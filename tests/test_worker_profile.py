"""Part H batch 1: worker settings files and the quiet_bash filter. Run: python3 -m unittest tests.test_worker_profile"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
W = os.path.join(ROOT, "nodes", "worker")
sys.path.insert(0, W)
import quiet_bash  # noqa: E402
import profile as wprofile  # noqa: E402

TOOLS = {"build": ["Read", "Edit", "Write", "Bash", "Grep", "Glob"],
         "fix": ["Read", "Edit", "Write", "Bash", "Grep", "Glob"],
         "review": ["Read", "Grep", "Glob"], "test": ["Bash"]}


class Settings(unittest.TestCase):
    def test_files(self):
        for kind, tools in TOOLS.items():
            with open(os.path.join(W, "settings-%s.json" % kind)) as f:
                s = json.load(f)
            self.assertEqual(s["permissions"]["allow"], tools)
            self.assertEqual(s.get("enabledPlugins", {}), {})
            hooks = s.get("hooks", {})
            if "Bash" in tools:
                self.assertEqual(list(hooks), ["PreToolUse"])
                self.assertEqual(hooks["PreToolUse"][0]["matcher"], "Bash")
            else:
                self.assertEqual(hooks, {})

    def test_no_relative_script_path(self):
        for kind in TOOLS:
            with open(os.path.join(W, "settings-%s.json" % kind)) as f:
                text = f.read()
            self.assertNotIn("nodes/worker", text)
            if kind in ("build", "fix", "test"):
                self.assertIn("{{QUIET_BASH}}", text)
                out = wprofile.render_settings(text, "/abs/quiet_bash.py")
                self.assertNotIn("{{", out)
                self.assertIn("python3 /abs/quiet_bash.py", out)

    def test_launch_args(self):
        self.assertEqual(wprofile.LAUNCH_ARGS, ("--setting-sources", "project", "--strict-mcp-config",
                                                "--disable-slash-commands"))
        with open(os.path.join(W, "README.md")) as f:
            self.assertIn(" ".join(wprofile.LAUNCH_ARGS), f.read())

    def test_mcp_empty(self):
        with open(os.path.join(W, "mcp-empty.json")) as f:
            self.assertEqual(json.load(f), {"mcpServers": {}})


def run_cmd(cmd, job):
    """Drive the real hook, then run the rewritten command."""
    env = dict(os.environ, FLEET_JOB_DIR=job)
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": cmd}})
    h = subprocess.run([sys.executable, os.path.join(W, "quiet_bash.py")], input=payload,
                       capture_output=True, text=True, env=env)
    if not h.stdout.strip():
        return None, h
    new = json.loads(h.stdout)["hookSpecificOutput"]["updatedInput"]["command"]
    return subprocess.run(["bash", "-c", new], capture_output=True, text=True, env=env), h


class Filter(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        d = self.tmp.name
        with open(os.path.join(d, "t_fail.py"), "w") as f:
            f.write("import unittest\n"
                    "class T(unittest.TestCase):\n"
                    "    def test_a(self): self.assertEqual(1, 2)\n"
                    "    def test_b(self): raise ValueError('boom')\n"
                    + "".join("    def test_p%d(self): pass\n" % i for i in range(300)))
        with open(os.path.join(d, "t_ok.py"), "w") as f:
            f.write("import unittest\nclass T(unittest.TestCase):\n    def test_a(self): pass\n")
        self.job = os.path.join(d, "job")

    def cmd(self, mod):
        return "cd %s && python3 -m unittest %s" % (self.tmp.name, mod)

    def test_failing(self):
        r, _ = run_cmd(self.cmd("t_fail"), self.job)
        self.assertEqual(r.returncode, 1)
        lines = r.stdout.splitlines()
        self.assertLessEqual(len(lines), 100)
        self.assertTrue(lines[0].startswith("exit=1 FAILED"))
        self.assertIn("failures=1", lines[0])
        self.assertIn("errors=1", lines[0])
        self.assertTrue(any(l.startswith("FAIL: test_a") for l in lines))
        self.assertTrue(any("AssertionError: 1 != 2" in l for l in lines))
        self.assertTrue(any(l.startswith("ERROR: test_b") for l in lines))
        self.assertTrue(any("ValueError: boom" in l for l in lines))
        self.assertTrue(lines[-1].startswith("full output: "))

    def test_full_log_written(self):
        r, _ = run_cmd(self.cmd("t_fail"), self.job)
        path = r.stdout.splitlines()[-1].split("full output: ", 1)[1]
        self.assertTrue(path.startswith(self.job))
        with open(path) as f:
            log = f.read()
        self.assertIn("Traceback", log)
        self.assertIn("Ran 302 tests", log)

    def test_passing_one_line(self):
        r, _ = run_cmd(self.cmd("t_ok"), self.job)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(len(r.stdout.splitlines()), 1)
        self.assertIn("exit=0 ok", r.stdout)
        self.assertIn("Ran 1 test", r.stdout)

    def test_cap_100(self):
        text = "\n".join("FAIL: test_%d (m.T)\nAssertionError: x" % i for i in range(200))
        out = quiet_bash.summarize(text, 1, "/x.log")
        self.assertLessEqual(len(out.splitlines()), 100)
        self.assertIn("exit=1", out)

    def test_nonzero_exit_never_hidden_when_unparsed(self):
        out = quiet_bash.summarize("weird output\nno summary", 3, "/x.log")
        self.assertTrue(out.startswith("exit=3"))

    def test_only_at_segment_start(self):
        for c in ["grep vitest src/", 'git commit -m "run python3 -m unittest later"', "echo scripts/gate.sh",
                  "cat x | grep -n 'python3 -m unittest'"]:
            self.assertFalse(quiet_bash.is_test_command(c), c)
        for c in ["python3 -m unittest t", "cd x && python3 -m unittest t", "make; scripts/gate.sh",
                  "FOO=1 npx vitest run", "a || vitest"]:
            self.assertTrue(quiet_bash.is_test_command(c), c)
        self.assertIsNone(quiet_bash.hook_output({"tool_name": "Bash", "tool_input": {"command": "grep vitest ."}}))

    def test_no_permission_decision(self):
        out = quiet_bash.hook_output({"tool_name": "Bash", "tool_input": {"command": "python3 -m unittest t"}})
        self.assertNotIn("permissionDecision", out["hookSpecificOutput"])
        self.assertIn("updatedInput", out["hookSpecificOutput"])

    def test_git_defaults(self):
        o = quiet_bash.hook_output({"tool_name": "Bash", "tool_input": {"command": "git log"}})
        self.assertEqual(o["hookSpecificOutput"]["updatedInput"]["command"], "git log -n 30")
        self.assertIsNone(quiet_bash.hook_output({"tool_name": "Bash", "tool_input": {"command": "git log -n 5"}}))
        self.assertIsNone(quiet_bash.hook_output({"tool_name": "Bash", "tool_input": {"command": "git diff -- a.py"}}))

    def test_git_diff_truncates(self):
        d = self.tmp.name
        env = dict(os.environ, FLEET_JOB_DIR=self.job)
        subprocess.run("cd %s && git init -q && git config user.email a@b && git config user.name a && "
                       "printf 'x\\n' > f && git add f && git commit -qm i && "
                       "seq 1 1000 > f" % d, shell=True, check=True, env=env)
        r, _ = run_cmd("cd %s && git diff" % d, self.job)
        self.assertIsNone(r)  # compound command is left alone
        o = quiet_bash.rewrite_command("git diff", os.path.join(self.job, "x.log"), "--head")
        os.makedirs(self.job, exist_ok=True)
        r = subprocess.run(["bash", "-c", o], cwd=d, capture_output=True, text=True, env=env)
        lines = r.stdout.splitlines()
        self.assertEqual(len(lines), 301)
        self.assertTrue(lines[-1].startswith("truncated, full: "))

    def test_line_cap_and_fallback_dir(self):
        out = quiet_bash.summarize("FAIL: t (m)\n" + "E" * 2000, 1, "/x.log")
        self.assertTrue(all(len(l) <= 520 for l in out.splitlines()))
        old = os.environ.pop("FLEET_JOB_DIR", None)
        try:
            d = quiet_bash.job_dir()
            self.assertTrue(os.path.isdir(d))
            self.assertNotEqual(d, os.path.join(tempfile.gettempdir(), "fleet-quiet-bash"))
        finally:
            if old is not None:
                os.environ["FLEET_JOB_DIR"] = old

    def test_log_write_failure_passes_output(self):
        r = subprocess.run([sys.executable, os.path.join(W, "quiet_bash.py"), "--wrap",
                            "/nonexistent-dir/x.log", "--", "echo hi; exit 4"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 4)
        self.assertIn("hi", r.stdout)

    def test_non_test_command_unchanged(self):
        r, h = run_cmd("echo hello", self.job)
        self.assertIsNone(r)
        self.assertEqual(h.stdout.strip(), "")
        self.assertEqual(h.returncode, 0)
        self.assertIsNone(quiet_bash.hook_output({"tool_name": "Read", "tool_input": {"command": "unittest"}}))


if __name__ == "__main__":
    unittest.main()
