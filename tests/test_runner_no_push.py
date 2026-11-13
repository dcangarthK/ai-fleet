"""Fix round 1 (review t-52c74425) H2, H3, L2: workers can't push below the agent; service restart commands.
Run: python3 -m unittest -v tests/test_runner_no_push.py
"""
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests.test_runner_budget import Base, runner  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}


def git(cwd, *args, env=None):
    return subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True)


class NoPushTests(Base):
    def setUp(self):
        super().setUp()
        self.saved_fleet = os.environ.get("FLEET_DIR")
        os.environ["FLEET_DIR"] = str(self.fleet)
        self.remote = self.base / "remote.git"
        git(self.base, "init", "--bare", "-q", str(self.remote))
        self.work = self.base / "work"
        git(self.base, "init", "-q", str(self.work))
        git(self.work, "remote", "add", "origin", str(self.remote))
        git(self.work, "commit", "--allow-empty", "-q", "-m", "x", env={**os.environ, **GIT_ENV})
        self.branch = git(self.work, "branch", "--show-current").stdout.strip()

    def tearDown(self):
        if self.saved_fleet is None:
            os.environ.pop("FLEET_DIR", None)
        else:
            os.environ["FLEET_DIR"] = self.saved_fleet
        super().tearDown()

    def push(self, env):
        return git(self.work, "push", "origin", self.branch, env=env)

    def test_control_push_works_without_the_fleet_config(self):
        self.assertEqual(self.push({**os.environ, **GIT_ENV}).returncode, 0)

    def test_codex_full_mode_cannot_push(self):
        job = self.job(agent="codex", mode="full")
        cmd = self.m._command(job, "p", [], False)
        self.assertIn("--dangerously-bypass-approvals-and-sandbox", cmd)  # nothing at the agent level stops git
        env = self.m.agent_env("codex", {**os.environ, **GIT_ENV, "SSH_AUTH_SOCK": "/tmp/agent",
                                          "GIT_ASKPASS": "/bin/true"}, None)
        done = self.push(env)
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("fleet-no-push", done.stderr)
        self.assertEqual(git(self.remote, "branch", "--list").stdout.strip(), "")  # nothing arrived

    def test_every_remote_style_and_agent_is_covered(self):
        env = self.m.agent_env("claude", {**os.environ, **GIT_ENV}, None)
        urls = ("https://example.invalid/r.git", "git@example.invalid:r.git", "ssh://h/r.git", str(self.remote),
                "git+ssh://h/r.git", "ssh+git://h/r.git", "git+https://h/r.git", "user@h.invalid:path/r.git",
                "h.invalid:r.git", "../remote.git", "r.git", "file://" + str(self.remote))
        for url in urls:
            git(self.work, "remote", "set-url", "origin", url)
            done = self.push(env)
            self.assertNotEqual(done.returncode, 0, url)
            self.assertIn("fleet-no-push", done.stderr, url)  # N4: refused by the rewrite, not by chance
        self.assertEqual(git(self.remote, "branch", "--list").stdout.strip(), "")

    def test_repo_local_config_cannot_reenable_push(self):
        env = self.m.agent_env("claude", {**os.environ, **GIT_ENV}, None)
        git(self.work, "config", "credential.helper", "store")
        git(self.work, "remote", "set-url", "origin", "https://h.invalid/r.git")
        self.assertIn("fleet-no-push", self.push(env).stderr)
        values = git(self.work, "config", "--get-all", "credential.helper", env=env).stdout.splitlines()
        self.assertEqual(values[-1], "")  # the last value is the reset, whatever .git/config added before it
        # Known residual (docs/SPEC-capacity-placement.md 3a): a repo-local longer pushInsteadOf prefix or a pushurl.

    def test_existing_git_config_count_is_kept(self):
        env = self.m.agent_env("claude", {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "a.b",
                                          "GIT_CONFIG_VALUE_0": "c"}, None)
        self.assertEqual(env["GIT_CONFIG_COUNT"], "3")
        self.assertEqual((env["GIT_CONFIG_KEY_0"], env["GIT_CONFIG_VALUE_0"]), ("a.b", "c"))

    def test_owner_global_config_is_included_first_and_fleet_rules_win(self):
        home = self.base / "home"
        home.mkdir()
        owner = home / ".gitconfig"
        owner.write_text("[user]\n\tname = Owner Name\n[alias]\n\tzz = status\n[credential]\n\thelper = store\n"
                         "[url \"git@host:\"]\n\tpushInsteadOf = https://host/\n")
        saved = {k: os.environ.get(k) for k in ("HOME", "GIT_CONFIG_GLOBAL", "XDG_CONFIG_HOME")}
        os.environ["HOME"] = str(home)
        os.environ.pop("GIT_CONFIG_GLOBAL", None)
        try:
            env = self.m.agent_env("codex", dict(os.environ), None)
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        text = Path(env["GIT_CONFIG_GLOBAL"]).read_text()
        self.assertLess(text.index("[include]"), text.index("[credential]"))  # owner first, fleet rules after
        self.assertLess(text.index("[include]"), text.index("pushInsteadOf"))
        copy = self.fleet / "worker.owner.gitconfig"
        self.assertIn(str(copy), text)  # the include points at a sanitized copy, not the owner's file itself
        kept = copy.read_text()
        self.assertNotIn("pushInsteadOf", kept)  # the owner's own rewrite can't beat the catch-all
        self.assertIn("Owner Name", kept)
        self.assertEqual(git(self.work, "config", "user.name", env=env).stdout.strip(), "Owner Name")
        self.assertEqual(git(self.work, "config", "alias.zz", env=env).stdout.strip(), "status")
        self.assertEqual(git(self.work, "config", "--get-all", "credential.helper", env=env).stdout.splitlines()[-1], "")
        git(self.work, "remote", "set-url", "origin", "https://host/r.git")  # the owner's own rewrite can't win
        done = self.push(env)
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("fleet-no-push", done.stderr)

    def test_old_or_missing_git_refuses_to_launch_the_worker(self):
        saved = list(runner._GIT_CHECK)
        try:
            for stdout, code in (("git version 2.31.1\n", 0), ("git version 1.8.3\n", 0), ("garbage", 0), ("", 1)):
                runner._GIT_CHECK[:] = []
                real = runner.subprocess.run
                runner.subprocess.run = lambda *a, _o=stdout, _c=code, **k: subprocess.CompletedProcess(a, _c, _o, "")
                try:
                    with self.assertRaises(RuntimeError, msg=stdout):
                        self.m.agent_env("claude", dict(os.environ), None)
                finally:
                    runner.subprocess.run = real
            runner._GIT_CHECK[:] = []
            runner.subprocess.run = lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError("git"))
            try:
                with self.assertRaises(RuntimeError):
                    self.m.agent_env("claude", dict(os.environ), None)
            finally:
                runner.subprocess.run = real
            runner._GIT_CHECK[:] = []
            ok, _detail = runner.git_version_check()
            self.assertTrue(ok)  # this machine's git is new enough
        finally:
            runner._GIT_CHECK[:] = saved

    def test_credentials_are_dropped_and_config_is_fleet_owned(self):
        env = self.m.agent_env("claude", {"SSH_AUTH_SOCK": "x", "GIT_ASKPASS": "y", "SSH_ASKPASS": "z",
                                          "GIT_SSH_COMMAND": "ssh -i k", "KEEP": "1"}, None)
        for key in ("SSH_AUTH_SOCK", "GIT_ASKPASS", "SSH_ASKPASS", "GIT_SSH_COMMAND"):
            self.assertNotIn(key, env)
        self.assertEqual(env["KEEP"], "1")
        self.assertEqual(env["GIT_CONFIG_GLOBAL"], str(self.fleet / "worker.gitconfig"))
        self.assertEqual(env["GIT_CONFIG_NOSYSTEM"], "1")
        self.assertIn("helper =", Path(env["GIT_CONFIG_GLOBAL"]).read_text())
        self.assertEqual(env["GIT_CONFIG_COUNT"], "2")

    def test_local_commits_still_work_for_a_worker(self):
        env = self.m.agent_env("codex", dict(os.environ), None)
        done = git(self.work, "commit", "--allow-empty", "-q", "-m", "worker", env=env)
        self.assertEqual(done.returncode, 0, done.stderr)


    def test_unwritable_config_refuses_to_launch_the_worker(self):
        blocker = self.base / "not-a-dir"
        blocker.write_text("x")
        os.environ["FLEET_DIR"] = str(blocker)  # worker.gitconfig can't be created under a file
        with self.assertRaises(RuntimeError):
            self.m.agent_env("claude", dict(os.environ), None)
        job = self.job(kind="build", cwd=str(self.code))
        with self.assertRaises(Exception):
            self.m._launch(job, "p", [], False)  # create() turns this into a failed job, never a running one

    def test_fleet_vars_survive_and_a_patched_subprocess_does_not_break_it(self):
        real = runner.subprocess.Popen
        runner.subprocess.Popen = lambda *a, **k: (_ for _ in ()).throw(OSError("patched"))
        try:
            env = self.m.agent_env("claude", {"FLEET_JOB_DIR": "/j", "FLEET_X": "1"}, None)
        finally:
            runner.subprocess.Popen = real
        self.assertEqual((env["FLEET_JOB_DIR"], env["FLEET_X"]), ("/j", "1"))


class ServiceRestartCommandTests(Base):
    def test_linux_uses_sudo_and_the_units_onboarding_installs(self):
        self.assertEqual(runner.service_restart_command("runner", "linux", 1000),
                         ["sudo", "-n", "systemctl", "restart", "ai-fleet-runner.service"])
        self.assertEqual(runner.service_restart_command("ollama", "linux", 1000),
                         ["sudo", "-n", "systemctl", "restart", "ollama"])
        onboard = (ROOT / "nodes/onboard.sh").read_text()
        self.assertIn("/etc/systemd/system/ai-fleet-runner.service", onboard)
        self.assertIn("restart ai-fleet-runner.service, %s restart ollama", onboard)  # the sudoers rule: those two only
        self.assertIn("visudo -cf", onboard)

    def test_macos_uses_launchctl_kickstart(self):
        self.assertEqual(runner.service_restart_command("ollama", "darwin", 501),
                         ["launchctl", "kickstart", "-k", "gui/501/local.ai-fleet.ollama"])

    def test_windows_is_refused_with_a_reason(self):
        with self.assertRaises(runner.HttpError) as caught:
            runner.service_restart_command("runner", "win32", None)
        self.assertEqual(caught.exception.status, 501)
        self.assertIn("not supported", caught.exception.detail)

    def test_unknown_and_unmonitored_services_are_refused(self):
        for name in ("sshd", "claude-mem", None):
            with self.assertRaises(runner.HttpError):
                runner.restart_service(name, self.m)

    def test_restart_is_written_to_the_audit_log(self):
        seen = []
        self.m.audit = lambda event, detail: seen.append((event, detail))
        real_cmd, real_run = runner.service_restart_command, runner.subprocess.run
        runner.service_restart_command = lambda service: ["true"]
        runner.subprocess.run = lambda *a, **k: subprocess.CompletedProcess(a, 0, "", "")
        try:
            runner.restart_service("ollama", self.m)
        finally:
            runner.service_restart_command, runner.subprocess.run = real_cmd, real_run
        self.assertEqual(seen, [("service_restart", "ollama")])


if __name__ == "__main__":
    unittest.main()
