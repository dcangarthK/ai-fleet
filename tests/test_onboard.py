"""Node onboarding (nodes/onboard.sh), the master's join helpers (master/setup.sh) and nodes/build.py.

Nothing here touches the machine: the scripts are sourced with *_SOURCE_ONLY=1 so single functions run against a
temporary FLEET_DIR, with fake tailscale/curl/launchctl binaries on PATH. Functions run under /bin/bash when it
exists, because macOS still ships bash 3.2 there and `bash onboard.sh` on a fresh Mac uses it.
"""

import hashlib
import json
import os
import plistlib
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ONBOARD = ROOT / "nodes" / "onboard.sh"
SETUP = ROOT / "master" / "setup.sh"
BASH = "/bin/bash" if Path("/bin/bash").exists() else shutil.which("bash")


def write_exec(path: Path, body: str) -> Path:
    path.write_text("#!/bin/sh\n" + textwrap.dedent(body))
    path.chmod(0o755)
    return path


class OnboardHarness(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="onboard-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.fleet = self.tmp / "fleet"
        self.bin = self.tmp / "bin"
        self.bin.mkdir()

    def env(self, **extra):
        (self.tmp / "home").mkdir(exist_ok=True)
        env = {"PATH": f"{self.bin}:/usr/bin:/bin:/usr/sbin:/sbin", "HOME": str(self.tmp / "home"),
               "FLEET_DIR": str(self.fleet), "ONBOARD_SOURCE_ONLY": "1", "SETUP_SOURCE_ONLY": "1",
               "ONBOARD_TTY": str(self.tmp / "no-tty"), "TMPDIR": str(self.tmp), "RELOAD_WAIT": "0"}
        env.update(extra)
        return env

    def run_fn(self, snippet: str, script: Path = ONBOARD, argv0: str | None = None, args=(), cwd=None, **env):
        """Source the script with `args`, then run `snippet` in the same shell. argv0 sets $0 (default: the script)."""
        cmd = [BASH, "-c", 'source "$ONBOARD_UNDER_TEST" "$@"\n' + snippet, argv0 or str(script), *args]
        return subprocess.run(cmd, capture_output=True, text=True, timeout=60, cwd=cwd,
                              env=self.env(ONBOARD_UNDER_TEST=str(script), **env))

    def fake_tailscale(self, hostname="Laptop2-MBP", tags=None, dns="laptop2-mbp.tail1234.ts.net."):
        status = {"Self": {"HostName": hostname, "DNSName": dns, "Tags": tags or []}}
        (self.tmp / "status.json").write_text(json.dumps(status))
        return write_exec(self.bin / "tailscale", f"""\
            case "$1" in
              status) cat "{self.tmp}/status.json" ;;
              ip) echo 100.64.0.7 ;;
              *) exit 1 ;;
            esac
            """)


class ScriptsParse(OnboardHarness):
    def test_bash_syntax(self):
        for script in (ONBOARD, SETUP):
            for shell in sorted({BASH, shutil.which("bash")}):
                with self.subTest(script=script.name, shell=shell):
                    result = subprocess.run([shell, "-n", str(script)], capture_output=True, text=True)
                    self.assertEqual(result.returncode, 0, result.stderr)

    def test_program_runs_from_main_at_the_end(self):
        # `curl | bash -s` must read the whole script before anything that might read stdin runs.
        last = [line for line in ONBOARD.read_text().splitlines() if line.strip()][-1]
        self.assertEqual(last, '[ "${ONBOARD_SOURCE_ONLY:-0}" = 1 ] || main')

    def test_piped_help_and_argument_errors_exit_before_changing_anything(self):
        def piped(*args):
            with ONBOARD.open() as script:
                return subprocess.run([BASH, "-s", "--", *args], stdin=script, capture_output=True,
                                      text=True, timeout=30, env=self.env(ONBOARD_SOURCE_ONLY="0"))
        result = piped("--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--enroll --master", result.stdout)
        self.assertIn("--enroll-file PATH --master", result.stdout)
        self.assertEqual(piped("--bogus").returncode, 1)
        for args in (("--enroll", "ABCDEFGHIJ"), ("--enroll",), ("--enroll-file", str(self.tmp / "code"))):
            with self.subTest(args=args):
                missing = piped(*args)
                self.assertEqual(missing.returncode, 1)
                self.assertIn("--master is required with --enroll", missing.stdout)
        no_tty = piped("--enroll", "--master", "https://master:8443")
        self.assertEqual(no_tty.returncode, 1)
        self.assertIn("needs a terminal", no_tty.stdout)
        plain_http = piped("--enroll", "ABCDEFGHIJ", "--master", "http://master:8701")
        self.assertEqual(plain_http.returncode, 1)
        self.assertIn("https://", plain_http.stdout)
        self.assertIn("needs a value", piped("--models").stdout)
        self.assertFalse(self.fleet.exists(), "argument errors must exit before creating FLEET_DIR")

    def test_no_prompt_reads_stdin(self):
        # Every prompt goes through tty_read; a bare `read` would eat the script under `curl | bash -s`.
        for number, line in enumerate(ONBOARD.read_text().splitlines(), 1):
            code = line.split("#", 1)[0]
            if "read -r" in code and "<&3" not in code:
                self.fail(f"onboard.sh:{number} reads stdin: {line.strip()}")

    def test_launch_agents_load_through_the_retrying_helper(self):
        for script in (ONBOARD, SETUP):
            direct = [line.strip() for line in script.read_text().splitlines()
                      if "launchctl bootstrap" in line and '"$domain"' not in line and "$domain " not in line]
            self.assertEqual(direct, [], script.name)

    def test_node_runner_uses_isolated_agent_profiles(self):
        text = ONBOARD.read_text()
        for needle in ('CLAUDE_CONFIG_DIR "$claude_dir"', 'CODEX_HOME "$codex_dir"',
                       "Environment=$runner_claude_env", "Environment=$runner_codex_env"):
            self.assertIn(needle, text)
        ps1 = (ROOT / "nodes" / "onboard.ps1").read_text()
        self.assertIn(r'set "CLAUDE_CONFIG_DIR=%USERPROFILE%\.claude-fleet"', ps1)
        self.assertIn(r'set "CODEX_HOME=%USERPROFILE%\.codex-fleet"', ps1)

    def test_windows_enrolls_before_advertising_worker_tag(self):
        ps1 = (ROOT / "nodes" / "onboard.ps1").read_text()
        enroll = ps1.index('Invoke-RestMethod -Method Post -Uri ($Master.TrimEnd(\'/\') + "/api/enroll/complete")')
        tag = ps1.rindex("Ensure-WorkerTag")
        self.assertLess(enroll, tag)
        main = ps1[ps1.index('if ($ScanOnly)'):]
        self.assertLess(main.index("Ensure-TailscaleConnection"), main.index("Invoke-RestMethod"))
        self.assertLess(main.index("Invoke-RestMethod"), main.index("Ensure-WorkerTag"))

    def test_dmr_smoke_check_is_read_only_and_rejects_non_loopback_listener(self):
        script = ROOT / "nodes" / "check_dmr.sh"
        text = script.read_text()
        self.assertIn("docker model ls", text)
        self.assertIn("127.0.0.1:12434/engines/v1/models", text)
        self.assertIn("lsof", text)
        self.assertNotRegex(text, r"(?m)^\s*(docker model (pull|rm)|tailscale|pfctl|iptables|curl .*-[A-Z].*POST)")
        fake = self.tmp / "fake-lsof"
        fake.write_text("#!/bin/sh\necho 'docker 1 user 9u IPv4 0 TCP *:12434 (LISTEN)'\n")
        fake.chmod(0o755)
        result = subprocess.run([BASH, str(script)], capture_output=True, text=True,
                                env=self.env(DMR_LSOF=str(fake), DMR_DOCKER="/usr/bin/false",
                                             DMR_CURL="/usr/bin/false", DMR_FIXTURE_DIR=str(self.tmp / "fixtures")))
        self.assertNotEqual(0, result.returncode)
        self.assertIn("FAIL", result.stdout)

    def test_dmr_smoke_check_passes_on_real_loopback_fixtures(self):
        fx = ROOT / "tests" / "fixtures" / "dmr"
        tool = {}
        for name, fixture in (("lsof", "lsof_12434.txt"), ("docker", "docker_model_ls.txt"), ("curl", "models.json")):
            path = self.tmp / ("fake-" + name)
            path.write_text(f"#!/bin/sh\ncat '{fx / fixture}'\n")
            path.chmod(0o755)
            tool[name] = str(path)
        result = subprocess.run([BASH, str(ROOT / "nodes" / "check_dmr.sh")], capture_output=True, text=True,
                                env=self.env(DMR_LSOF=tool["lsof"], DMR_DOCKER=tool["docker"],
                                             DMR_CURL=tool["curl"], DMR_FIXTURE_DIR=str(self.tmp / "fx2")))
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertNotIn("FAIL", result.stdout)


class RunnerPolicy(OnboardHarness):
    def configure(self, ram=16, pre=None, extra="", **env):
        self.fleet.mkdir(parents=True, exist_ok=True)
        if pre is not None:
            (self.fleet / "runner.json").write_text(json.dumps(pre))
        result = self.run_fn(f"ram_gb() {{ echo {ram}; }}\n{extra}\nconfigure_runner_policy", **env)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        return json.loads((self.fleet / "runner.json").read_text()), result.stdout

    def test_defaults_for_a_16gb_node_with_yes_and_no_roots(self):
        # Also the bash 3.2 regression: "${ALLOW_ROOTS[@]}" on an empty array aborted under `set -u`.
        config, out = self.configure(extra="YES=1")
        self.assertEqual(config["allowed_roots"], [])
        self.assertEqual(config["max_jobs"], 1)
        self.assertEqual(config["max_mode"], "auto-edit")
        self.assertEqual(config["power"], {"min_battery_pct": 30})
        self.assertEqual(config["models"], {"remote_management": False, "max_loaded": 1,
                                            "disk_reserve_gb": 8, "ram_reserve_gb": 4})
        self.assertEqual(config["gate_profiles"]["pytest"], ["python3", "-m", "pytest", "-q"])
        self.assertFalse(config["allow_raw_gates"])
        self.assertIn("allowed_roots is empty", out)
        self.assertEqual(stat.S_IMODE((self.fleet / "runner.json").stat().st_mode), 0o600)

    def test_bigger_machines_keep_two_jobs(self):
        config, _ = self.configure(ram=48, extra="YES=1")
        self.assertEqual(config["max_jobs"], 2)

    def test_rerun_keeps_owner_edits(self):
        pre = {"allowed_roots": ["/srv/repos"], "max_jobs": 4, "power": {"min_battery_pct": 0},
               "models": {"remote_management": True}, "gate_profiles": {"fleet-gate": ["bash", "scripts/gate.sh"]}}
        config, out = self.configure(pre=pre, extra="YES=1")
        self.assertEqual(config["allowed_roots"], ["/srv/repos"])
        self.assertEqual(config["max_jobs"], 4)
        self.assertEqual(config["power"]["min_battery_pct"], 0)
        self.assertTrue(config["models"]["remote_management"])
        self.assertEqual(config["gate_profiles"]["fleet-gate"], ["bash", "scripts/gate.sh"])
        self.assertIn("pytest", config["gate_profiles"])
        self.assertNotIn("allowed_roots is empty", out)

    def test_flags_override_roots_and_enable_remote_models(self):
        root = self.tmp / "code"
        root.mkdir()
        config, _ = self.configure(pre={"allowed_roots": ["/old"]},
                                   extra=f'YES=1; REMOTE_MODELS=1; ALLOW_ROOTS=("{root}"); ALLOW_ROOTS_SET=1')
        self.assertEqual(config["allowed_roots"], [os.path.realpath(root)])
        self.assertTrue(config["models"]["remote_management"])

    def test_interactive_roots_come_from_the_terminal(self):
        root = self.tmp / "repos"
        root.mkdir()
        tty = self.tmp / "tty"
        tty.write_text(f"{root}\n")  # each prompt reopens the "terminal"; the file stands in for it
        config, _ = self.configure(ONBOARD_TTY=str(tty), extra='tty_read() { if [ -z "${_once:-}" ]; then _once=1; printf -v "$1" "%s" "$(head -1 "$TTY")"; else return 1; fi; }')
        self.assertEqual(config["allowed_roots"], [os.path.realpath(root)])

    def test_recommended_models_fit_16gb(self):
        result = self.run_fn("ram_gb() { echo 16; }; gpu_gb() { echo 0; }; recommended_models")
        self.assertEqual(result.stdout.split(), ["qwen2.5-coder:14b", "phi4:14b"])
        self.assertNotIn("glm", result.stdout)


class Prompts(OnboardHarness):
    def ask(self, answer=None, yes=False):
        env = {}
        if answer is not None:
            (self.tmp / "tty").write_text(answer)
            env["ONBOARD_TTY"] = str(self.tmp / "tty")
        return self.run_fn(("YES=1; " if yes else "") + 'ask "Install it?"', **env)

    def test_answers(self):
        self.assertEqual(self.ask("y\n").returncode, 0)
        self.assertEqual(self.ask("\n").returncode, 0)  # default is yes
        self.assertEqual(self.ask("n\n").returncode, 1)
        self.assertEqual(self.ask(yes=True).returncode, 0)

    def test_no_terminal_means_no(self):
        result = self.ask()
        self.assertEqual(result.returncode, 1)
        self.assertIn("no terminal to ask", result.stdout)


class Enrollment(OnboardHarness):
    def fake_curl(self, status="200", exit_code=0):
        return write_exec(self.bin / "curl", f"""\
            printf '%s\\n' "$@" > "{self.tmp}/curl.argv"
            out=""
            while [ $# -gt 0 ]; do [ "$1" = -o ] && out="$2"; shift; done
            cat > "{self.tmp}/curl.stdin"
            [ {exit_code} = 0 ] || {{ echo "curl: (7) Failed to connect" >&2; exit {exit_code}; }}
            [ -n "$out" ] && echo '{{"detail":"x"}}' > "$out"
            printf '%s' {status}
            """)

    def enroll(self):
        self.fake_tailscale()
        snippet = (f'TS="{self.bin}/tailscale"; ENROLL=ABCDEFGHIJ; MASTER=https://master.tail1234.ts.net:8443\n'
                   'mkdir -p "$FLEET_DIR"; ensure_runner_token\n'
                   'if enroll_complete; then echo "RC=0 ENROLLED=$ENROLLED"; else echo "RC=1 ENROLLED=$ENROLLED"; fi')
        return self.run_fn(snippet)

    def test_posts_code_tailscale_hostname_and_token_on_stdin(self):
        self.fake_curl("200")
        result = self.enroll()
        self.assertIn("RC=0 ENROLLED=1", result.stdout, result.stderr)
        token = (self.fleet / "runner.token").read_text().strip()
        body = json.loads((self.tmp / "curl.stdin").read_text())
        self.assertEqual(body, {"code": "ABCDEFGHIJ", "hostname": "laptop2-mbp", "runner_token": token})
        argv = (self.tmp / "curl.argv").read_text()
        self.assertIn("https://master.tail1234.ts.net:8443/api/enroll/complete", argv)
        self.assertNotIn(token, argv)
        self.assertNotIn(token, result.stdout + result.stderr)
        self.assertEqual(list(self.tmp.glob("fleet-enroll.*")), [], "temporary files are removed")

    def test_refused_code_explains_what_to_do(self):
        self.fake_curl("403")
        result = self.enroll()
        self.assertIn("RC=1 ENROLLED=0", result.stdout)
        self.assertIn("expired after 30 minutes", result.stdout)

    def test_unreachable_master_names_the_usual_causes(self):
        self.fake_curl(exit_code=7)
        result = self.enroll()
        self.assertIn("RC=1", result.stdout)
        self.assertIn("Could not reach https://master.tail1234.ts.net:8443", result.stdout)
        self.assertIn("tailscale serve --bg --https=8443", result.stdout)

    def test_runner_token_is_private_and_never_replaced(self):
        result = self.run_fn('mkdir -p "$FLEET_DIR"; ensure_runner_token; cat "$FLEET_DIR/runner.token";'
                             'ensure_runner_token; cat "$FLEET_DIR/runner.token"')
        first, second = result.stdout.split()
        self.assertEqual(first, second)
        self.assertGreaterEqual(len(first), 40)
        self.assertEqual(stat.S_IMODE((self.fleet / "runner.token").stat().st_mode), 0o600)
        (self.fleet / "runner.token").write_text("")
        result = self.run_fn('ensure_runner_token; cat "$FLEET_DIR/runner.token"')
        self.assertTrue(result.stdout.strip(), "an empty token file is replaced (the runner refuses it)")

    def test_hostname_matches_what_the_controller_sees(self):
        self.fake_tailscale(hostname="Duncans-MacBook-Pro")
        result = self.run_fn(f'TS="{self.bin}/tailscale"; tailnet_hostname')
        self.assertEqual(result.stdout.strip(), "duncans-macbook-pro")
        fallback = self.run_fn('TS=""; tailnet_hostname').stdout.strip()
        self.assertTrue(fallback)
        self.assertEqual(fallback, fallback.lower())

    def test_piped_run_saves_a_copy_from_the_master(self):
        write_exec(self.bin / "curl", f"""\
            while [ $# -gt 0 ]; do [ "$1" = -o ] && out="$2"; shift; done
            cp "{ONBOARD}" "$out"
            """)
        result = self.run_fn('mkdir -p "$FLEET_DIR"; MASTER=https://m:8443; save_self_copy; save_self_copy',
                             argv0="bash")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.fleet / "onboard.sh").read_text(), ONBOARD.read_text())
        self.assertFalse((self.fleet / "onboard.sh.tmp").exists())

    def test_piped_run_without_master_warns(self):
        result = self.run_fn('mkdir -p "$FLEET_DIR"; save_self_copy', argv0="bash")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Could not save a copy of onboard.sh", result.stdout)

    def test_piped_run_ignores_a_file_named_bash_in_the_cwd(self):
        # Under `curl | bash -s` $0 is "bash"; a file of that name in the cwd is not this script.
        (self.tmp / "bash").write_text("echo not the onboarding script\n")
        result = self.run_fn('mkdir -p "$FLEET_DIR"; save_self_copy', argv0="bash", cwd=self.tmp)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.fleet / "onboard.sh").exists())
        self.assertIn("Could not save a copy of onboard.sh", result.stdout)

    def test_downloaded_copy_that_does_not_parse_is_not_saved(self):
        write_exec(self.bin / "curl", """\
            while [ $# -gt 0 ]; do [ "$1" = -o ] && out="$2"; shift; done
            printf 'if then fi\\n' > "$out"
            """)
        result = self.run_fn('mkdir -p "$FLEET_DIR"; MASTER=https://m:8443; save_self_copy', argv0="bash")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.fleet / "onboard.sh").exists())
        self.assertFalse((self.fleet / "onboard.sh.tmp").exists())

    def test_file_run_copies_itself(self):
        result = self.run_fn('mkdir -p "$FLEET_DIR"; save_self_copy')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.fleet / "onboard.sh").read_text(), ONBOARD.read_text())


class LaunchAgents(OnboardHarness):
    def fake_launchctl(self, print_ok_times=2, bootstrap_fail_times=1):
        state = self.tmp / "launchctl.state"
        state.write_text("0 0\n")
        (self.tmp / "launchctl.log").unlink(missing_ok=True)
        write_exec(self.bin / "launchctl", f"""\
            read p b < "{state}"
            echo "$1" >> "{self.tmp}/launchctl.log"
            case "$1" in
              print) p=$((p+1)); echo "$p $b" > "{state}"; [ "$p" -le {print_ok_times} ] && exit 0; exit 113 ;;
              bootstrap) b=$((b+1)); echo "$p $b" > "{state}"; [ "$b" -le {bootstrap_fail_times} ] && exit 5; exit 0 ;;
              *) exit 0 ;;
            esac
            """)

    def test_waits_for_bootout_and_retries_bootstrap(self):
        for script in (ONBOARD, SETUP):
            with self.subTest(script=script.name):
                self.fake_launchctl()
                result = self.run_fn('reload_launch_agent /x/local.ai-fleet.runner.plist', script=script)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                calls = (self.tmp / "launchctl.log").read_text().split()
                self.assertEqual(calls, ["bootout", "print", "print", "print", "bootstrap", "bootstrap"])

    def test_gives_up_with_a_message(self):
        self.fake_launchctl(print_ok_times=0, bootstrap_fail_times=99)
        result = self.run_fn('reload_launch_agent /x/local.ai-fleet.ollama.plist || echo FAILED')
        self.assertIn("Could not start local.ai-fleet.ollama", result.stdout)
        self.assertIn("FAILED", result.stdout)


class MasterPublicHost(OnboardHarness):
    def test_public_host_is_the_serve_address_with_port(self):
        ts = self.fake_tailscale(dns="Studio.Tail1234.ts.net.")
        result = self.run_fn(f'TS="{ts}"; public_host', script=SETUP)
        self.assertEqual(result.stdout.strip(), "studio.tail1234.ts.net:8443")

    def test_public_host_empty_without_tailscale(self):
        result = self.run_fn(f'TS="{self.tmp}/missing"; public_host; echo "rc=$?"', script=SETUP)
        self.assertEqual(result.stdout.strip(), "rc=0")


class EnrollmentSecrets(OnboardHarness):
    """main() with --enroll: the code and the runner token stay out of every argv and all output, and a failed
    enrollment stops before tagging (a tag:llm-node machine can no longer reach the master to retry)."""

    CODE = "QWERTYUIOP"
    MASTER = "https://master.tail1234.ts.net:8443"

    def setUp(self):
        super().setUp()
        self.argv_log = self.tmp / "argv.log"
        real_python = shutil.which("python3", path="/usr/bin:/bin:/usr/sbin:/sbin:/usr/local/bin:/opt/homebrew/bin")
        # Every fake records its full argv; python3 is a recording wrapper around the real interpreter.
        write_exec(self.bin / "python3", f"""\
            echo "python3 $*" >> "{self.argv_log}"
            exec "{real_python}" "$@"
            """)
        self.status = {"Self": {"HostName": "Laptop2-MBP", "DNSName": "laptop2-mbp.tail1234.ts.net.", "Tags": []}}
        write_exec(self.bin / "tailscale", f"""\
            echo "tailscale $*" >> "{self.argv_log}"
            case "$1" in
              status) cat "{self.tmp}/status.json" ;;
              ip) echo 100.64.0.7 ;;
              up|set) exit 0 ;;
              *) exit 1 ;;
            esac
            """)

    def fake_curl(self, status):
        write_exec(self.bin / "curl", f"""\
            echo "curl $*" >> "{self.argv_log}"
            out=""
            for a in "$@"; do [ "$prev" = -o ] && out="$a"; prev="$a"; done
            cat > "{self.tmp}/curl.stdin"
            [ -n "$out" ] && echo '{{"detail":"x"}}' > "$out"
            printf '%s' {status}
            """)

    def main(self, *args, curl_status="200", tags=(), **env):
        self.status["Self"]["Tags"] = list(tags)
        (self.tmp / "status.json").write_text(json.dumps(self.status))
        self.fake_curl(curl_status)
        stubs = (f'ensure_basics() {{ :; }}; install_tailscale() {{ TS="{self.bin}/tailscale"; }}\n'
                 'save_self_copy() { :; }; ram_gb() { echo 16; }; configure_ollama() { :; }; scan() { :; }\n'
                 'install_services() { :; }; install_runner() { :; }; have() { return 1; }\n'
                 'main; echo "MAIN_RC=$?"')
        return self.run_fn(stubs, args=args, **env)

    def code_file(self, mode=0o600):
        path = self.tmp / "enroll-code"
        path.unlink(missing_ok=True)  # an earlier subtest may have left it read-only
        path.write_text(self.CODE + "\n")
        path.chmod(mode)
        return path

    def calls(self, program):
        if not self.argv_log.exists():
            return []
        return [line for line in self.argv_log.read_text().splitlines() if line.startswith(program + " ")]

    def assert_secrets_hidden(self, result):
        argv = self.argv_log.read_text() if self.argv_log.exists() else ""
        output = result.stdout + result.stderr
        self.assertNotIn(self.CODE, argv)
        self.assertNotIn(self.CODE, output)
        token_file = self.fleet / "runner.token"
        if token_file.exists():
            token = token_file.read_text().strip()
            self.assertNotIn(token, argv)
            self.assertNotIn(token, output)

    def test_enroll_file_enrolls_then_tags(self):
        result = self.main("--enroll-file", str(self.code_file()), "--master", self.MASTER)
        self.assertIn("MAIN_RC=0", result.stdout, result.stdout + result.stderr)
        body = json.loads((self.tmp / "curl.stdin").read_text())
        self.assertEqual(body["code"], self.CODE)
        self.assertEqual(body["runner_token"], (self.fleet / "runner.token").read_text().strip())
        self.assertIn("tailscale up --advertise-tags=tag:llm-node", self.calls("tailscale"))
        self.assertTrue(self.calls("python3"), "the JSON encoder ran through the recording python3")
        self.assert_secrets_hidden(result)

    def test_failed_enrollment_never_tags(self):
        result = self.main("--enroll-file", str(self.code_file()), "--master", self.MASTER, curl_status="403")
        self.assertNotIn("MAIN_RC=0", result.stdout)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual([c for c in self.calls("tailscale") if c.startswith("tailscale up")], [])
        self.assertIn("refused the enrollment code", result.stdout)
        self.assertIn("not tagged and no services were", result.stdout)
        self.assertNotIn("nothing else was", result.stdout)
        self.assertIn("--enroll --master " + self.MASTER, result.stdout)
        self.assertIn("runner-token set", result.stdout)
        self.assert_secrets_hidden(result)

    def test_unreachable_or_already_tagged_never_tags(self):
        for tags in ((), ("tag:llm-node",)):
            with self.subTest(tags=tags):
                self.argv_log.unlink(missing_ok=True)
                result = self.main("--enroll-file", str(self.code_file()), "--master", self.MASTER,
                                   curl_status="000", tags=tags)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("MAIN_RC=0", result.stdout)
                self.assertEqual([c for c in self.calls("tailscale") if c.startswith("tailscale up")], [])
                self.assert_secrets_hidden(result)
                if tags:  # already tagged: the "this machine is not tagged" preamble would be false
                    self.assertIn("already tagged", result.stdout)
                    self.assertNotIn("not tagged", result.stdout)
                else:
                    self.assertIn("not tagged and no services were", result.stdout)

    def test_hidden_prompt_reads_the_terminal(self):
        tty = self.tmp / "tty"
        tty.write_text(self.CODE + "\n")
        result = self.main("--enroll", "--master", self.MASTER, ONBOARD_TTY=str(tty))
        self.assertIn("MAIN_RC=0", result.stdout, result.stdout + result.stderr)
        self.assertEqual(json.loads((self.tmp / "curl.stdin").read_text())["code"], self.CODE)
        self.assert_secrets_hidden(result)
        self.assertIn("read -rsp", ONBOARD.read_text(), "the prompt hides the code as it is typed")

    def test_read_only_enroll_file_is_accepted(self):
        result = self.main("--enroll-file", str(self.code_file(0o400)), "--master", self.MASTER)
        self.assertIn("MAIN_RC=0", result.stdout, result.stdout + result.stderr)
        self.assertEqual(json.loads((self.tmp / "curl.stdin").read_text())["code"], self.CODE)
        self.assert_secrets_hidden(result)

    def test_enroll_file_with_wrong_mode_is_refused(self):
        for mode, message in ((0o644, "group or others"), (0o640, "group or others"), (0o604, "group or others"),
                              (0o440, "group or others"), (0o700, "must be 600 or 400")):
            with self.subTest(mode=oct(mode)):
                self.argv_log.unlink(missing_ok=True)
                path = self.code_file(mode)
                result = self.main("--enroll-file", str(path), "--master", self.MASTER)
                self.assertEqual(result.returncode, 1)
                self.assertIn("chmod 600", result.stdout)
                self.assertIn(message, result.stdout)
                self.assertEqual(self.calls("curl"), [])
                self.assertEqual(self.calls("tailscale"), [])
                self.assert_secrets_hidden(result)

    def test_missing_or_empty_enroll_file_is_refused(self):
        empty = self.code_file()
        empty.write_text("\n")
        for path in (self.tmp / "nope", empty):
            with self.subTest(path=path.name):
                result = self.main("--enroll-file", str(path), "--master", self.MASTER)
                self.assertEqual(result.returncode, 1)
                self.assertEqual(self.calls("curl"), [])

    def test_code_in_argv_still_works_but_warns(self):
        result = self.main("--enroll", self.CODE, "--master", self.MASTER)
        self.assertIn("MAIN_RC=0", result.stdout, result.stdout + result.stderr)
        self.assertIn("shell history", result.stdout)
        self.assertEqual(json.loads((self.tmp / "curl.stdin").read_text())["code"], self.CODE)
        argv = self.argv_log.read_text()
        self.assertNotIn(self.CODE, argv, "the code never reaches a child process's argv")


class PipedEndToEnd(OnboardHarness):
    """`curl ... | bash -s -- --enroll-file F --master URL` on a 16 GB Mac, with fake brew, tailscale, curl,
    ollama, launchctl, osascript, uname and sysctl. save_self_copy, enrollment, tagging, configure_ollama,
    install_services and install_runner are the real ones; only the inventory scan is stubbed (it would probe
    this machine's real ports and agent CLIs)."""

    MASTER = "https://x:8443"
    CODE = "ZXCVBNMASD"
    IP = "100.64.0.7"

    def setUp(self):
        super().setUp()
        log = self.tmp / "calls.log"
        self.log = log
        status = {"Self": {"HostName": "Laptop2-MBP", "DNSName": "laptop2-mbp.tail1234.ts.net.", "Tags": []}}
        (self.tmp / "status.json").write_text(json.dumps(status))
        write_exec(self.bin / "tailscale", f"""\
            echo "tailscale $*" >> "{log}"
            case "$1" in
              status) cat "{self.tmp}/status.json" ;;
              ip) echo {self.IP} ;;
              up|set) exit 0 ;;
              *) exit 1 ;;
            esac
            """)
        write_exec(self.bin / "curl", f"""\
            echo "curl $*" >> "{log}"
            out=""; url=""; prev=""
            for a in "$@"; do
              [ "$prev" = -o ] && out="$a"
              case "$a" in http*) url="$a" ;; esac
              prev="$a"
            done
            case "$url" in
              */api/enroll/complete) cat > "{self.tmp}/curl.stdin"; echo '{{}}' > "$out"; printf 200 ;;
              */enroll/onboard.sh) cp "{ONBOARD}" "$out" ;;
              */api/tags) echo '{{"models":[{{"name":"qwen2.5-coder:14b"}}]}}' ;;
              *) exit 7 ;;
            esac
            """)
        for name, body in (("brew", ""), ("osascript", ""), ("ollama", ""), ("launchctl", '[ "$1" = print ] && exit 113\nexit 0\n'),
                           ("uname", "echo Darwin\n"), ("sysctl", "echo 17179869184\n")):
            write_exec(self.bin / name, f'echo "{name} $*" >> "{log}"\n' + body)

    def test_piped_enroll_file_install(self):
        code = self.tmp / "code"
        code.write_text(self.CODE + "\n")
        code.chmod(0o600)
        (self.tmp / "bash").write_text("echo not the onboarding script\n")  # $0 is "bash" when piped
        lines = ONBOARD.read_text().rstrip("\n").splitlines()
        self.assertEqual(lines[-1], '[ "${ONBOARD_SOURCE_ONLY:-0}" = 1 ] || main')
        piped = "\n".join(lines[:-1] + ["scan() { :; }", lines[-1]]) + "\n"
        result = subprocess.run(
            [BASH, "-s", "--", "--enroll-file", str(code), "--master", self.MASTER], input=piped,
            capture_output=True, text=True, timeout=120, cwd=self.tmp,
            env=self.env(ONBOARD_SOURCE_ONLY="0", TAILSCALE_APP=str(self.tmp / "no-tailscale-app")))
        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 0, output)
        self.assertIn("Enrolled as 'laptop2-mbp'", result.stdout)
        calls = self.log.read_text()
        self.assertIn("tailscale up --advertise-tags=tag:llm-node", calls)
        # Enrollment and the self-copy (both need the master) happen before tagging.
        self.assertLess(calls.index("/enroll/onboard.sh"), calls.index("tailscale up"))
        self.assertLess(calls.index("/api/enroll/complete"), calls.index("tailscale up"))
        self.assertEqual((self.fleet / "onboard.sh").read_text(), ONBOARD.read_text(), "saved from the master")
        token = (self.fleet / "runner.token").read_text().strip()
        body = json.loads((self.tmp / "curl.stdin").read_text())
        self.assertEqual(body, {"code": self.CODE, "hostname": "laptop2-mbp", "runner_token": token})

        la = Path(self.env()["HOME"]) / "Library" / "LaunchAgents"
        plists = {p.name: plistlib.loads(p.read_bytes()) for p in la.glob("*.plist")}
        self.assertEqual(sorted(plists), [f"local.ai-fleet.{j}.plist" for j in ("info", "ollama", "runner", "scan")])
        for name, data in plists.items():
            self.assertEqual(data["Label"], name[:-len(".plist")])
            raw = (la / name).read_text()
            self.assertNotIn(token, raw)
            self.assertNotIn(self.CODE, raw)
            self.assertNotIn("TOKEN", raw.upper().replace("RUNNER.TOKEN", ""))
        fleet = str(self.fleet)
        ollama = plists["local.ai-fleet.ollama.plist"]
        self.assertEqual(ollama["ProgramArguments"], [str(self.bin / "ollama"), "serve"])
        self.assertEqual(ollama["EnvironmentVariables"]["OLLAMA_HOST"], f"{self.IP}:11434")
        self.assertEqual(ollama["EnvironmentVariables"]["OLLAMA_MAX_LOADED_MODELS"], "1")
        self.assertEqual(ollama["EnvironmentVariables"]["OLLAMA_NUM_PARALLEL"], "1")
        self.assertEqual(ollama["EnvironmentVariables"]["OLLAMA_CONTEXT_LENGTH"], "16384")
        info = plists["local.ai-fleet.info.plist"]
        self.assertEqual(info["ProgramArguments"][1:],
                         ["-m", "http.server", "11435", "--bind", self.IP, "--directory", fleet + "/public"])
        self.assertTrue(info["RunAtLoad"] and info["KeepAlive"])
        scan = plists["local.ai-fleet.scan.plist"]
        self.assertEqual(scan["ProgramArguments"], ["/bin/bash", fleet + "/onboard.sh", "--scan-only"])
        self.assertEqual(scan["StartInterval"], 3600)
        self.assertEqual(scan["EnvironmentVariables"]["FLEET_DIR"], fleet)
        runner = plists["local.ai-fleet.runner.plist"]
        self.assertEqual(runner["ProgramArguments"][1:], [fleet + "/runner.py"])
        env = runner["EnvironmentVariables"]
        home = self.env()["HOME"]
        self.assertEqual(env["RUNNER_BIND"], f"{self.IP}:11436")
        self.assertEqual(env["FLEET_DIR"], fleet)
        self.assertEqual(env["OLLAMA_URL"], f"http://{self.IP}:11434")
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], home + "/.claude-fleet")
        self.assertEqual(env["CODEX_HOME"], home + "/.codex-fleet")
        self.assertTrue(runner["RunAtLoad"] and runner["KeepAlive"])
        config = json.loads((self.fleet / "runner.json").read_text())
        self.assertEqual((config["max_jobs"], config["allowed_roots"]), (1, []))
        self.assertNotIn(token, output)
        self.assertNotIn(self.CODE, output)
        self.assertNotIn(self.CODE, calls)
        self.assertNotIn(token, calls)


class TailnetPolicy(unittest.TestCase):
    """tailscale/policy.hujson: minimal §14 N0 reachability."""

    def load(self):
        text = (ROOT / "tailscale" / "policy.hujson").read_text()
        text = re.sub(r"(?m)^\s*//.*$|\s//[^\"\n]*$", "", text)  # comments (none inside strings here)
        text = re.sub(r",(\s*[}\]])", r"\1", text)  # trailing commas
        return json.loads(text)

    def test_worker_can_reach_master_https_but_not_another_worker(self):
        policy = self.load()
        node_tests = [t for t in policy["tests"] if t["src"] == "tag:llm-node"]
        self.assertTrue(any("tag:master:8443" in t.get("accept", []) for t in node_tests))
        self.assertTrue(any("tag:llm-node:11436" in t.get("deny", []) for t in node_tests))
        grants = [g for g in policy["grants"] if "tag:llm-node" in g["src"] and "tag:master" in g["dst"]]
        self.assertEqual([["tcp:8443"]], [g["ip"] for g in grants])


class MasterTailscaleTag(OnboardHarness):
    """master/setup.sh tags the Mac tag:master without resetting its other Tailscale preferences."""

    def fake(self, has_set=True, up_ok=True, set_ok=True):
        log = self.tmp / "ts.argv"
        log.unlink(missing_ok=True)
        help_text = "USAGE\n  tailscale set [flags]\n  --advertise-tags string" if has_set else ""
        return write_exec(self.bin / "tailscale", f"""\
            echo "$*" >> "{log}"
            case "$1" in
              set) if [ "$2" = --help ]; then
                     [ -n "{help_text}" ] || {{ echo "tailscale: unknown subcommand: set" >&2; exit 1; }}
                     printf '{help_text}\\n' >&2; exit 0
                   fi
                   [ {int(set_ok)} = 1 ] ;;
              up) [ {int(up_ok)} = 1 ] || {{ echo "Error: changing settings via 'tailscale up' requires mentioning all non-default flags" >&2; exit 1; }} ;;
              *) exit 1 ;;
            esac
            """), log

    def tag(self, *args, **fake):
        ts, log = self.fake(**fake)
        result = self.run_fn(f'TS="{ts}"; if tag_master; then echo RC=0; else echo RC=1; fi', script=SETUP, args=args)
        calls = log.read_text().splitlines() if log.exists() else []
        return result, calls

    def test_uses_tailscale_set_when_available(self):
        result, calls = self.tag()
        self.assertIn("RC=0", result.stdout, result.stdout + result.stderr)
        self.assertIn("set --advertise-tags=tag:master", calls)
        self.assertFalse([c for c in calls if c.startswith("up")])
        self.assertFalse([c for c in calls if "--reset" in c])

    def test_falls_back_to_up_without_reset(self):
        result, calls = self.tag(has_set=False)
        self.assertIn("RC=0", result.stdout, result.stdout + result.stderr)
        self.assertIn("up --advertise-tags=tag:master", calls)
        self.assertFalse([c for c in calls if "--reset" in c])

    def test_refusal_explains_and_never_resets_by_default(self):
        for kw in ({"has_set": False, "up_ok": False}, {"set_ok": False}):
            with self.subTest(**kw):
                result, calls = self.tag(**kw)
                self.assertIn("RC=1", result.stdout)
                self.assertFalse([c for c in calls if "--reset" in c], calls)
                self.assertIn("--reset-tailscale", result.stdout)
                self.assertIn("tailscale/policy.hujson", result.stdout)

    def test_reset_only_with_the_opt_in_flag(self):
        result, calls = self.tag("--reset-tailscale", has_set=False, up_ok=False)
        self.assertIn("up --advertise-tags=tag:master --reset", calls)
        self.assertIn("resets", result.stdout)
        # The opt-in still tries the non-destructive way first.
        result, calls = self.tag("--reset-tailscale")
        self.assertIn("RC=0", result.stdout)
        self.assertFalse([c for c in calls if "--reset" in c])

    def test_only_the_opt_in_branch_mentions_reset(self):
        lines = [line.strip() for line in SETUP.read_text().splitlines()
                 if "--reset" in line and "--reset-tailscale" not in line and not line.strip().startswith("#")]
        self.assertEqual(len(lines), 1, lines)
        self.assertIn('"$TS" up --advertise-tags=tag:master --reset', lines[0])

    def test_flags_parse_together(self):
        result = self.run_fn('echo "$SERVICES_ONLY $RESET_TAILSCALE"', script=SETUP,
                             args=("--reset-tailscale", "--services-only"))
        self.assertEqual(result.stdout.strip(), "1 1")
        result = self.run_fn('echo "$SERVICES_ONLY $RESET_TAILSCALE"', script=SETUP)
        self.assertEqual(result.stdout.strip(), "0 0")


class Build(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="build-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        for name in ("build.py", "runner.py", "onboard.sh", "onboard.ps1"):
            shutil.copy(ROOT / "nodes" / name, self.tmp / name)

    def build(self, *args, where=None):
        return subprocess.run([sys.executable, str((where or self.tmp) / "build.py"), *args],
                              capture_output=True, text=True, timeout=60)

    def test_repo_scripts_embed_the_current_runner(self):
        result = self.build("--check", where=ROOT / "nodes")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_stale_scripts_are_detected_then_rebuilt(self):
        runner = self.tmp / "runner.py"
        runner.write_text(runner.read_text() + "# changed\n")
        stale = self.build("--check")
        self.assertEqual(stale.returncode, 1)
        self.assertIn("onboard.sh", stale.stdout)
        self.assertIn("onboard.ps1", stale.stdout)
        self.assertEqual(self.build().returncode, 0)
        self.assertEqual(self.build("--check").returncode, 0)
        self.assertIn("# changed\n# <<< runner.py", (self.tmp / "onboard.sh").read_text())
        result = subprocess.run([BASH, "-n", str(self.tmp / "onboard.sh")], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_lines_that_would_end_the_here_doc_are_refused(self):
        for bad in ("RUNNER_PY", "'@"):
            with self.subTest(bad=bad):
                (self.tmp / "runner.py").write_text((ROOT / "nodes" / "runner.py").read_text() + bad + "\n")
                result = self.build()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("would break the here-doc", result.stderr)

    def test_installed_runner_matches_the_masters_digest(self):
        # The controller flags update_available when a node's runner.py digest differs from nodes/runner.py,
        # so the installed copy must not carry build.py's marker lines.
        with tempfile.TemporaryDirectory() as fleet:
            result = subprocess.run([BASH, "-c", 'source "$SCRIPT"; write_runner', str(ONBOARD)],
                                    capture_output=True, text=True, timeout=60,
                                    env={"PATH": "/usr/bin:/bin", "HOME": fleet, "FLEET_DIR": fleet,
                                         "ONBOARD_SOURCE_ONLY": "1", "SCRIPT": str(ONBOARD)})
            self.assertEqual(result.returncode, 0, result.stderr)
            installed = Path(fleet, "runner.py").read_bytes()
        self.assertEqual(hashlib.sha256(installed).hexdigest(),
                         hashlib.sha256((ROOT / "nodes" / "runner.py").read_bytes()).hexdigest())

if __name__ == "__main__":
    unittest.main()
