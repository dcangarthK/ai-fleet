"""G2: leading-cd strip, heredoc deny, new Bash rules. Run: python3 -m unittest nodes.worker.test_quiet_bash_g2"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import quiet_bash  # noqa: E402
import profile as wprofile  # noqa: E402


class G2(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.wt = os.path.realpath(self.tmp.name)
        for key, val in (("CLAUDE_PROJECT_DIR", self.wt), ("FLEET_JOB_DIR", self.wt)):
            old = os.environ.get(key)
            os.environ[key] = val
            self.addCleanup(lambda k=key, o=old: os.environ.pop(k, None) if o is None
                            else os.environ.__setitem__(k, o))

    def run_hook(self, cmd):
        out = quiet_bash.hook_output({"tool_name": "Bash", "tool_input": {"command": cmd}})
        return out["hookSpecificOutput"] if out else None

    def new_cmd(self, cmd):
        h = self.run_hook(cmd)
        return h["updatedInput"]["command"] if h and "updatedInput" in h else None

    def test_strip_root(self):
        self.assertEqual(self.new_cmd("cd %s && ls" % self.wt), "ls")
        self.assertEqual(self.new_cmd("cd %s; ls src | head" % self.wt), "ls src | head")

    def test_subdir_normalised(self):
        self.assertEqual(self.new_cmd("cd %s/master/controller/ui; npx tsc -b" % self.wt),
                         "cd master/controller/ui && npx tsc -b")
        self.assertEqual(self.new_cmd("cd sub && grep x f"), "cd sub && grep x f")

    def test_left_alone(self):
        for c in ("cd /etc && ls", "cd a && cd b && ls", "cd ../.. && ls", "cd %s && ls; cd master" % self.wt):
            self.assertIsNone(self.run_hook(c), c)

    def test_heredoc_denied_and_cat_passes(self):
        for c in ("cat > x <<'EOF'\nhi\nEOF", "cat >> x <<EOF\nhi\nEOF", "cd sub && cat > f <<'EOF'\nx\nEOF"):
            h = self.run_hook(c)
            self.assertEqual(h["permissionDecision"], "deny", c)
            self.assertIn("Write/Edit", h["permissionDecisionReason"])
        self.assertIsNone(self.run_hook("cat x"))

    def test_deny_does_not_end_the_run(self):
        # The hook exits 0 with a deny reply (a refused tool call with a reason), never a non-zero exit.
        import io
        import json
        import contextlib
        payload = {"tool_name": "Bash", "tool_input": {"command": "cat > x <<EOF\nhi\nEOF"}}
        old = sys.stdin
        sys.stdin = io.StringIO(json.dumps(payload))
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                rc = quiet_bash.main(["quiet_bash.py"])
        finally:
            sys.stdin = old
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(buf.getvalue())["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_rules(self):
        rules = set(wprofile.BASH_RULES)
        for r in ("cd:*", "npx tsc:*", "npm run typecheck", "npm run lint"):
            self.assertIn("Bash(%s)" % r, rules)
        for r in ("npm run:*", "sed:*", "sed -i:*", "echo", "find:*"):
            self.assertNotIn("Bash(%s)" % r, rules)

    def test_test_command_after_cd_still_wrapped(self):
        c = self.new_cmd("cd %s && python3 -m unittest x" % self.wt)
        self.assertIn("--wrap", c)


if __name__ == "__main__":
    unittest.main()
