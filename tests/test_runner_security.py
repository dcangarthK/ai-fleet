"""Phase 4 runner boundary tests (docs/SPEC-phase4-runner-boundary.md). Real runner subprocess, fake agents.
Run: python3 -m unittest -v tests/test_runner_security.py
"""
import json
import os
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_runner as base  # noqa: E402  (fake agents, free_port)

RUNNER = base.RUNNER
CASE_INSENSITIVE = sys.platform in ("darwin", "win32")


@unittest.skipUnless(base.loopback_bind_available(), "needs loopback sockets")
class RunnerBoundary(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(os.path.realpath(self.temp.name))
        self.fleet = self.base / "fleet"
        self.fleet.mkdir()
        self.bin = self.base / "bin"
        self.bin.mkdir()
        fake = base.FAKE_AGENT.format(python=sys.executable)
        for name in ("claude", "codex"):
            p = self.bin / name
            p.write_text(fake, encoding="utf-8")
            p.chmod(0o755)
        self.root = self.base / "code"          # the allowed root
        self.outside = self.base / "elsewhere"  # not allowed
        self.root.mkdir()
        self.outside.mkdir()
        self.repo = self.make_repo(self.root / "proj")
        self.proc = None
        self.token = "tok-" + "x" * 40

    def tearDown(self):
        for state_path in self.fleet.glob("jobs/*/state.json"):
            try:
                st = json.loads(state_path.read_text())
                if st.get("state") == "running" and st.get("pid") and os.name != "nt":
                    os.killpg(st["pid"], signal.SIGKILL)
            except (OSError, ValueError):
                pass
        self.stop()
        self.temp.cleanup()

    # ---------- helpers ----------

    def make_repo(self, path, *extra):
        path.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init", "-q", "-b", "main", *extra, str(path)], check=True, capture_output=True)
        for k, v in (("user.email", "t@example.test"), ("user.name", "T")):
            subprocess.run(["git", "-C", str(path), "config", k, v], check=True)
        (path / "tracked.txt").write_text("original\n")
        subprocess.run(["git", "-C", str(path), "add", "."], check=True)
        subprocess.run(["git", "-C", str(path), "commit", "-qm", "init"], check=True)
        return path

    def start(self, config=None, token=True):
        cfg = {"max_mode": "auto-edit", "allowed_roots": [str(self.root)],
               "gate_profiles": {"pass": [sys.executable, "-c", "import sys; print('gate ok', sys.argv[1:])"],
                                 "fail": [sys.executable, "-c", "import sys; print('gate bad'); sys.exit(7)"]}}
        if config is not None:
            cfg.update(config)
        (self.fleet / "runner.json").write_text(json.dumps(cfg))
        self.port = base.free_port()
        env = os.environ.copy()
        env.pop("RUNNER_TOKEN", None)
        env.update({"FLEET_DIR": str(self.fleet), "RUNNER_BIND": "127.0.0.1:%d" % self.port,
                    "OLLAMA_URL": "http://127.0.0.1:1", "PATH": str(self.bin) + os.pathsep + env.get("PATH", "")})
        if token:
            env["RUNNER_TOKEN"] = self.token
        self.proc = subprocess.Popen([sys.executable, str(RUNNER)], env=env, stdin=subprocess.DEVNULL,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        deadline = time.time() + 10
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError("runner exited: " + self.proc.stderr.read().decode()[-500:])
            try:
                urllib.request.urlopen("http://127.0.0.1:%d/health" % self.port, timeout=1)
                return
            except urllib.error.HTTPError:
                return  # up (401 is expected without a token)
            except OSError:
                time.sleep(0.05)
        raise RuntimeError("runner did not start")

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)
        if self.proc and self.proc.stderr:
            self.proc.stderr.close()
        self.proc = None

    def request(self, method, path, body=None, token=None):
        token = self.token if token is None else token
        headers = {"Content-Type": "application/json"}
        if token:
            headers["X-Runner-Token"] = token
        req = urllib.request.Request("http://127.0.0.1:%d%s" % (self.port, path),
                                     data=None if body is None else json.dumps(body).encode(),
                                     headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.load(r)
        except urllib.error.HTTPError as e:
            return e.code, json.load(e)

    def no_token_status(self):
        try:
            urllib.request.urlopen("http://127.0.0.1:%d/health" % self.port, timeout=5)
            return 200
        except urllib.error.HTTPError as e:
            return e.code

    def create(self, job_id, **kw):
        body = {"id": job_id, "agent": "claude", "prompt": "hello", "mode": "auto-edit", "repo": None,
                "worktree": False, "cwd": None, "model": None, "gate": None, "allowed_tools": [],
                "resume_session": None}
        body.update(kw)
        return self.request("POST", "/jobs", body)

    def wait_job(self, job_id, pred, timeout=10):
        end = time.time() + timeout
        job = None
        while time.time() < end:
            _, job = self.request("GET", "/jobs/" + job_id)
            if pred(job):
                return job
            time.sleep(0.05)
        self.fail("job %s stuck: %r" % (job_id, job))

    def audit(self):
        path = self.fleet / "audit.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    def assertDenied(self, status, body):
        self.assertEqual(403, status, body)
        self.assertIn("not allowed", body["detail"])
        self.assertNotIn(str(self.root), body["detail"])  # doesn't echo the config

    # ---------- credential ----------

    def test_generates_token_when_none_and_requires_it_everywhere(self):
        self.start(token=False)
        tok_file = self.fleet / "runner.token"
        self.assertTrue(tok_file.exists())
        self.assertEqual(0o600, stat.S_IMODE(tok_file.stat().st_mode))
        self.token = tok_file.read_text().strip()
        self.assertGreaterEqual(len(self.token), 32)
        self.assertEqual(200, self.request("GET", "/health")[0])
        self.assertEqual(401, self.no_token_status())
        self.assertEqual(401, self.request("GET", "/health", token="wrong-token")[0])
        self.assertEqual(401, self.request("GET", "/jobs", token="wrong-token")[0])
        self.assertIn("auth_failed", [e["event"] for e in self.audit()])
        self.stop()  # a restart keeps the same token
        self.start(token=False)
        self.assertEqual(self.token, tok_file.read_text().strip())

    def test_health_v2_reports_local_policy(self):
        self.start({"max_jobs": 3})
        status, h = self.request("GET", "/health")
        self.assertEqual(200, status)
        self.assertEqual("2", h["version"])
        self.assertEqual(["fail", "pass"], sorted(h["gate_profiles"]))
        self.assertFalse(h["allow_raw_gates"])
        self.assertEqual([str(self.root)], h["allowed_roots"])
        self.assertEqual(3, h["max_jobs"])

    # ---------- paths ----------

    def test_allowed_root_repo_and_cwd_work(self):
        self.start()
        status, job = self.create("ok-wt", repo=str(self.repo), worktree=True)
        self.assertEqual(200, status, job)
        self.wait_job("ok-wt", lambda j: j["state"] != "running")
        status, job = self.create("ok-cwd", cwd=str(self.repo))
        self.assertEqual(200, status, job)

    def test_outside_root_is_refused_and_audited(self):
        self.start()
        other = self.make_repo(self.outside / "proj")
        self.assertDenied(*self.create("out-repo", repo=str(other), worktree=True))
        self.assertDenied(*self.create("out-cwd", cwd=str(other)))
        self.assertIn("path_denied", [e["event"] for e in self.audit()])
        self.assertFalse((self.fleet / "jobs" / "out-repo").exists())

    def test_symlink_and_dotdot_escapes_are_refused(self):
        self.start()
        self.make_repo(self.outside / "proj")
        link = self.root / "sneaky"
        link.symlink_to(self.outside / "proj")
        self.assertDenied(*self.create("via-link", cwd=str(link)))
        self.assertDenied(*self.create("via-link-repo", repo=str(link), worktree=True))
        self.assertDenied(*self.create("via-dotdot", cwd=str(self.root) + "/../elsewhere/proj"))

    def test_relative_and_missing_paths_are_rejected(self):
        self.start()
        self.assertEqual(400, self.create("rel", cwd="code/proj")[0])
        self.assertIn(self.create("missing", cwd=str(self.root / "nope"))[0], (400, 403))

    def test_fleet_dir_is_always_denied(self):
        self.start({"allowed_roots": [str(self.base)]})  # the allowed root contains the runner's own dir
        self.assertDenied(*self.create("fleet-cwd", cwd=str(self.fleet)))
        (self.fleet / "jobs").mkdir(exist_ok=True)
        self.assertDenied(*self.create("fleet-sub", cwd=str(self.fleet / "jobs")))
        self.assertDenied(*self.create("fleet-parent", cwd=str(self.base)))  # contains the fleet dir

    def test_root_that_contains_a_denied_path_is_refused(self):
        secret = self.root / "Secret"
        secret.mkdir()
        self.start({"denied_paths": [str(secret)]})
        self.assertDenied(*self.create("parent-of-denied", cwd=str(self.root)))
        self.assertDenied(*self.create("in-denied", cwd=str(secret)))
        self.assertEqual(200, self.create("sibling", cwd=str(self.repo))[0])

    @unittest.skipUnless(CASE_INSENSITIVE, "case-insensitive file systems only")
    def test_case_variant_of_denied_path_is_refused(self):
        secret = self.root / "Secret"
        (secret / "inner").mkdir(parents=True)
        self.start({"denied_paths": [str(secret)]})
        self.assertDenied(*self.create("case", cwd=str(self.root / "SECRET" / "inner")))

    def test_builtin_denied_paths_cant_be_removed(self):
        home_ssh = Path.home() / ".ssh"
        if not home_ssh.is_dir():
            self.skipTest("no ~/.ssh on this machine")
        self.start({"allowed_roots": [str(Path.home())], "denied_paths": []})
        self.assertDenied(*self.create("ssh", cwd=str(home_ssh)))
        self.assertDenied(*self.create("home", cwd=str(Path.home())))  # contains ~/.ssh

    def test_no_allowed_roots_means_no_paths(self):
        self.start({"allowed_roots": []})
        self.assertDenied(*self.create("any-path", cwd=str(self.repo)))
        status, job = self.create("scratch")  # no repo/cwd: a scratch dir is fine
        self.assertEqual(200, status, job)

    def test_git_dir_pointing_outside_is_refused(self):
        hidden = self.outside / "gitdir"
        repo = self.make_repo(self.root / "split", "--separate-git-dir", str(hidden))
        self.start()
        self.assertDenied(*self.create("split", repo=str(repo), worktree=True))

    def test_cwd_with_git_dir_outside_is_refused(self):
        hidden = self.outside / "gitdir2"
        repo = self.make_repo(self.root / "split2", "--separate-git-dir", str(hidden))
        self.start()
        self.assertDenied(*self.create("split-cwd", cwd=str(repo)))

    # ---------- gates ----------

    def test_gate_profile_runs_without_a_shell(self):
        self.start()
        status, job = self.create("g-pass", cwd=str(self.repo), gate="pass", gate_args=["a.py", "-k=x"])
        self.assertEqual(200, status, job)
        job = self.wait_job("g-pass", lambda j: (j.get("gate") or {}).get("state") in ("passed", "failed"))
        self.assertEqual("passed", job["gate"]["state"])
        self.assertIn("gate ok ['a.py', '-k=x']", job["gate"]["output_tail"])
        self.assertEqual(200, self.create("g-fail", cwd=str(self.repo), gate="fail")[0])
        job = self.wait_job("g-fail", lambda j: (j.get("gate") or {}).get("state") in ("passed", "failed"))
        self.assertEqual(7, job["gate"]["exit_code"])

    def test_raw_gate_and_shell_injection_are_refused(self):
        self.start()
        pwned = self.base / "PWNED"
        cases = [("touch %s" % pwned, None), ("pass; touch %s" % pwned, None),
                 ("pass", ["x; touch %s" % pwned]), ("pass", ["$(touch %s)" % pwned]),
                 ("pass", ["../../etc/passwd"]), ("pass", ["/etc/passwd"]), ("pass", ["a"] * 21),
                 ("pass", "not-a-list")]
        for i, (gate, args) in enumerate(cases):
            body = {"gate": gate} | ({"gate_args": args} if args is not None else {})
            status, resp = self.create("inj-%d" % i, cwd=str(self.repo), **body)
            self.assertEqual(400, status, (gate, args, resp))
        time.sleep(0.5)
        self.assertFalse(pwned.exists())
        self.assertIn("gate_denied", [e["event"] for e in self.audit()])

    def test_raw_gates_only_when_node_allows(self):
        self.start({"allow_raw_gates": True})
        status, job = self.create("raw", cwd=str(self.repo), gate="%s -c \"print('raw ok')\"" % sys.executable)
        self.assertEqual(200, status, job)
        job = self.wait_job("raw", lambda j: (j.get("gate") or {}).get("state") in ("passed", "failed"))
        self.assertEqual("passed", job["gate"]["state"])
        self.assertIn("raw ok", job["gate"]["output_tail"])

    def test_saved_raw_gate_is_not_rerun_when_raw_gates_are_off(self):
        job_dir = self.fleet / "jobs" / "legacy"
        job_dir.mkdir(parents=True)
        pwned = self.base / "LEGACY_RAN"
        (job_dir / "exit_code.1").write_text("0")
        state = {"id": "legacy", "agent": "claude", "mode": "auto-edit", "state": "exited", "exit_code": 0,
                 "session_id": None, "cwd": str(self.repo), "worktree": False, "branch": None,
                 "started_at": "2026-01-01T00:00:00+00:00", "ended_at": "2026-01-01T00:01:00+00:00",
                 "result": "", "denials": [], "runs": 1, "repo": None, "base": None, "model": None,
                 "allowed_tools": [], "pid": None, "exit_file": "exit_code.1",
                 "gate": {"cmd": "touch %s" % pwned, "state": "pending", "exit_code": None, "output_tail": ""}}
        (job_dir / "state.json").write_text(json.dumps(state))
        self.start()
        job = self.wait_job("legacy", lambda j: (j.get("gate") or {}).get("state") == "failed")
        self.assertIn("raw gate", job["gate"]["output_tail"])
        self.assertFalse(pwned.exists())

    # ---------- request hygiene and limits ----------

    def test_unknown_fields_are_refused(self):
        self.start()
        status, body = self.create("extra", allowed_roots=["/"])
        self.assertEqual(400, status)
        self.assertIn("allowed_roots", body["detail"])
        self.assertIn("bad_field", [e["event"] for e in self.audit()])

    def test_mode_refusal_is_audited(self):
        self.start({"max_mode": "supervised"})
        self.assertEqual(403, self.create("too-much", mode="auto-edit")[0])
        self.assertIn("mode_denied", [e["event"] for e in self.audit()])

    def test_max_jobs(self):
        self.start({"max_jobs": 1})
        self.assertEqual(200, self.create("busy-1", prompt="sleep")[0])
        status, body = self.create("busy-2")
        self.assertEqual(429, status, body)
        self.assertIn("busy", [e["event"] for e in self.audit()])
        self.request("POST", "/jobs/busy-1/cancel", {})

    def test_max_job_minutes_kills_the_run(self):
        self.start({"max_job_minutes": 0.03})
        self.assertEqual(200, self.create("slow", prompt="sleep")[0])
        job = self.wait_job("slow", lambda j: j["state"] != "running", timeout=15)
        self.assertEqual(124, job["exit_code"])
        self.assertIn("time", (job.get("result") or "").lower())

    def test_bad_config_values_fall_back_to_safe_defaults(self):
        self.start({"max_jobs": "lots", "allowed_roots": "/", "allow_raw_gates": "yes", "max_mode": "god"})
        _, h = self.request("GET", "/health")
        self.assertEqual("auto-edit", h["max_mode"])
        self.assertEqual([], h["allowed_roots"])
        self.assertFalse(h["allow_raw_gates"])
        self.assertEqual(2, h["max_jobs"])

    def test_audit_log_is_private(self):
        self.start()
        self.request("GET", "/health", token="wrong-token")
        self.assertEqual(0o600, stat.S_IMODE((self.fleet / "audit.jsonl").stat().st_mode))


if __name__ == "__main__":
    unittest.main()
