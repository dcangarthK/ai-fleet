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

    def run_hook(self, cmd, cwd=None):
        out = quiet_bash.hook_output({"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": cwd or self.wt})
        return out["hookSpecificOutput"] if out else None

    def new_cmd(self, cmd, cwd=None):
        h = self.run_hook(cmd, cwd)
        return h["updatedInput"]["command"] if h and "updatedInput" in h else None

    def asks(self, cmd, cwd=None):
        h = self.run_hook(cmd, cwd)
        self.assertIsNotNone(h, cmd)
        self.assertEqual(h.get("permissionDecision"), "ask", cmd)
        self.assertNotIn("updatedInput", h, cmd)

    def test_strip_root(self):
        self.assertEqual(self.new_cmd("cd %s && ls" % self.wt), "ls")
        self.assertEqual(self.new_cmd("cd %s; ls src | head" % self.wt), "ls src | head")

    def test_subdir_absolute_and_separator_kept(self):
        ui = os.path.join(self.wt, "master", "controller", "ui")
        os.makedirs(ui)
        self.assertEqual(self.new_cmd("cd %s; npx tsc -b" % ui), "cd %s ; npx tsc -b" % ui)
        sub = os.path.join(self.wt, "sub")
        os.makedirs(sub)
        self.assertEqual(self.new_cmd("cd sub && grep x f"), "cd %s && grep x f" % sub)
        self.assertEqual(self.new_cmd("cd sub && grep x f", cwd=self.wt), "cd %s && grep x f" % sub)

    def test_relative_dir_resolves_against_payload_cwd(self):
        sub = os.path.join(self.wt, "sub")
        os.makedirs(os.path.join(sub, "inner"))
        inner = os.path.join(sub, "inner")
        self.assertEqual(self.new_cmd("cd inner && ls", cwd=sub), "cd %s && ls" % inner)
        self.assertEqual(self.new_cmd("cd .. && ls", cwd=sub), "cd %s && ls" % self.wt)
        self.assertEqual(self.new_cmd("cd . && ls", cwd=sub), "ls")
        self.asks("cd ../.. && ls", cwd=sub)

    def test_separators_inside_unquoted_dir_do_not_corrupt(self):
        os.makedirs(os.path.join(self.wt, "a"))
        got = self.new_cmd("cd a;b;c")
        self.assertEqual(got, "cd %s ; b;c" % os.path.join(self.wt, "a"))
        os.makedirs(os.path.join(self.wt, "x"))
        self.assertEqual(self.new_cmd("cd x&&rm&&y"), "cd %s && rm&&y" % os.path.join(self.wt, "x"))

    def test_nonexistent_in_tree_dir_left_alone(self):
        self.assertIsNone(self.run_hook("cd nope && ls"))

    def test_outside_or_unresolvable_cd_asks(self):
        os.makedirs(os.path.join(self.wt, "a"))
        os.symlink("/etc", os.path.join(self.wt, "link"))
        for c in ("cd /etc && ls", "cd ~/.ssh && cat id_rsa", "cd ~ && ls", "cd - && ls", "cd .. && ls",
                  "cd ../.. && ls", "cd link && ls", "cd $(echo /etc) && ls", "cd `echo /etc` && ls",
                  "cd $HOME && ls", "cd /et* && ls", "cd -P /etc && ls", "cd /etc\nls", "cd a && cd b && ls",
                  "cd a; cd /etc; ls", "cd %s && ls; cd master" % self.wt, "ls; cd /etc; cat x",
                  "(cd /etc && ls)", "cd && ls", "cd 'a b/~' && ls"):
            self.asks(c.replace("\\n", "\n"))

    def test_heredoc_write_denied_with_hint(self):
        for c in ("cat > x <<EOF\nhi\nEOF", "cat >> x <<'EOF'\nhi\nEOF", "cd %s; cat > f <<'EOF'\nhi\nEOF" % self.wt,
                  "cat <<EOF > x\nhi\nEOF", "tee x <<EOF\nhi\nEOF"):
            h = self.run_hook(c)
            self.assertEqual(h["permissionDecision"], "deny", c)
            self.assertIn("Write/Edit", h["permissionDecisionReason"])

    def test_plain_cat_passes_and_sed_i_not_rewritten(self):
        self.assertIsNone(self.run_hook("cat x"))
        self.assertIsNone(self.run_hook("sed -i s/a/b/ f"))
        self.assertIsNone(self.run_hook("npx tsc -b"))
        self.assertIsNone(self.run_hook("npm run typecheck 2>&1 | tail -8"))

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
