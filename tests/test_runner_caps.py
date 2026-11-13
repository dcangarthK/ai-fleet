"""Part H batch 2 (docs/SPEC-token-budget.md 3.1, 3.2): runner lean launch, caps, `capped` state.
Run: python3 -m unittest -v tests/test_runner_caps.py
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests.test_runner_budget import Base, runner  # noqa: E402

# A claude that never stops by itself: one new assistant message id per step, each followed by a tool result.
ENDLESS = r'''#!{python}
import json, sys, time
sys.stdin.read()
if "--resume" in sys.argv:  # the one stop run: commit and write the handoff
    open(sys.argv[0] + ".resumed", "w").write(" ".join(sys.argv))
    print(json.dumps({{"type": "assistant", "message": {{"id": "h1", "content": [
        {{"type": "text", "text": "handoff: committed"}}]}}}}))
    sys.exit(0)
print(json.dumps({{"type": "system", "session_id": "s1"}}), flush=True)
print(json.dumps({{"type": "rate_limit_event", "rate_limit_info": {{"status": "allowed", "unifiedWindows": {{
    "five_hour": {{"utilization": 0.3, "resetsAt": 1763000000}}}}}}}}), flush=True)
i = 0
while True:
    i += 1
    msg = {{"id": "m%d" % (i // 2 + 1), "content": [{{"type": "text", "text": "step %d" % i}}],
           "usage": {{"input_tokens": 10, "cache_read_input_tokens": 100}}}}
    print(json.dumps({{"type": "assistant", "message": msg}}), flush=True)  # a repeated id is one turn
    if i % 2 == 0:
        print(json.dumps({{"type": "user", "message": {{"content": [{{"type": "tool_result"}}]}}}}), flush=True)
    time.sleep(0.05)
'''

MAX_TURNS = r'''#!{python}
import json, sys
sys.stdin.read()
print(json.dumps({{"type": "assistant", "message": {{"id": "a", "content": [{{"type": "text", "text": "partly done"}}]}}}}))
print(json.dumps({{"type": "result", "subtype": "error_max_turns", "session_id": "s1", "num_turns": 3,
                   "permission_denials": []}}))
sys.exit(1)
'''


class Caps(Base):
    def setUp(self):
        super().setUp()
        self.saved = {k: os.environ.get(k) for k in ("FLEET_TOKEN_CAPS", "FLEET_LEAN_PROFILE")}

    def tearDown(self):
        for key, value in self.saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        super().tearDown()

    def fake(self, template):
        path = self.bin / "claude"
        path.write_text(template.format(python=sys.executable))
        path.chmod(0o755)

    def start(self, body):
        self.m.create(body)  # returns a public copy; the live job is in m.jobs
        return self.m.jobs[body["id"]]

    def wait(self, job, seconds=15):
        end = time.time() + seconds
        while time.time() < end and job["state"] == "running":
            self.m.refresh(job)
            time.sleep(0.05)

    def body(self, **kw):
        return {"id": "cap1", "agent": "claude", "prompt": "x", "mode": "auto-edit", "cwd": str(self.code),
                "kind": "build"} | kw

    def test_job_at_its_turn_cap_settles_capped_with_a_summary(self):
        self.fake(ENDLESS)
        os.environ["FLEET_TOKEN_CAPS"] = json.dumps({"build": {"turns": 2}})
        job = self.start(self.body())
        self.wait(job)
        self.assertEqual(job["state"], "capped")
        self.assertEqual(job["cap"]["reason"], "turns")
        self.assertEqual(job["cap"]["limit"], 2)
        self.assertEqual(job["cap"]["stop"], "resumed")  # the stop run committed and wrote the handoff
        self.assertEqual(job["turns_seen"], 2)  # repeated message ids count once
        self.assertEqual(job["result"], "handoff: committed")
        args = (self.bin / "claude.resumed").read_text().split()
        self.assertEqual(args[args.index("--resume") + 1], "s1")
        self.assertEqual((self.fleet / "jobs" / "cap1" / "handoff.md").read_text().strip(), job["result"])
        self.assertIsNone(job["exit_code"])
        self.assertIn("capped", self.m.public(job)["state"])

    def test_context_cap_also_stops_the_job(self):
        self.fake(ENDLESS)
        os.environ["FLEET_TOKEN_CAPS"] = json.dumps({"build": {"turns": 1000, "context": 100}})
        job = self.start(self.body(id="cap2"))
        self.wait(job)
        self.assertEqual((job["state"], job["cap"]["reason"]), ("capped", "context"))

    def test_cli_max_turns_result_settles_capped_not_failed(self):
        self.fake(MAX_TURNS)
        job = self.start(self.body(id="cap3"))
        self.wait(job)
        self.assertEqual(job["state"], "capped")
        self.assertEqual(job["cap"]["reason"], "max_turns")
        self.assertEqual(job["result"], "partly done")

    def test_rate_limit_observed_at_is_the_live_read_time_not_the_file_mtime(self):
        self.fake(ENDLESS)
        os.environ["FLEET_TOKEN_CAPS"] = json.dumps({"build": {"turns": 2}})
        before = time.time()
        job = self.start(self.body(id="cap4"))
        self.wait(job)
        old = before - 86400
        os.utime(self.fleet / "jobs" / "cap4" / "events.jsonl", (old, old))  # an old mtime changes nothing
        seen = self.m.claude_usage()["fleet"]
        self.assertGreaterEqual(seen["observed_at"], before)
        self.assertAlmostEqual(seen["windows"]["5h"]["used_percent"], 30.0)

    def test_old_job_without_a_live_stamp_gives_no_usage(self):
        folder = self.m.jobs_dir / "old1"
        folder.mkdir(parents=True)
        (folder / "state.json").write_text(json.dumps({"id": "old1", "agent": "claude", "state": "exited"}))
        (folder / "events.jsonl").write_text(json.dumps({"type": "rate_limit_event", "rate_limit_info": {
            "status": "allowed", "unifiedWindows": {"five_hour": {"utilization": 0.9, "resetsAt": 1}}}}) + "\n")
        self.assertEqual(self.m.claude_usage(), {})


class Watch(Base):
    """_watch_stream on a hand-written log."""

    def state(self, job, **kw):
        return {"pos": 0, "buf": b"", "ids": set(), "cap": None, "dirty": False, "pending": set(), "stamp_from": 0,
                "caps": {"turns": 1}} | kw

    def write(self, job, events):
        path = self.fleet / "jobs" / job["id"] / "events.jsonl"
        with open(path, "ab") as handle:
            for event in events:
                handle.write((json.dumps(event) + "\n").encode())

    def test_waits_for_every_tool_result_of_the_turn(self):
        job, st = self.job(), None
        st = self.state(job)
        use = {"type": "assistant", "message": {"id": "m1", "content": [
            {"type": "tool_use", "id": "a"}, {"type": "tool_use", "id": "b"}]}}
        result = lambda i: {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": i}]}}
        self.write(job, [use, result("a")])
        self.assertIsNone(self.m._watch_stream(job, st))  # "b" has no result yet
        self.write(job, [result("b")])
        self.assertEqual(self.m._watch_stream(job, st)["reason"], "turns")

    def test_restored_monitor_does_not_stamp_old_rate_limit_events(self):
        job = self.job()
        event = {"type": "rate_limit_event", "rate_limit_info": {
            "status": "allowed", "unifiedWindows": {"five_hour": {"utilization": 0.5, "resetsAt": 1763000000}}}}
        self.write(job, [event])
        size = (self.fleet / "jobs" / job["id"] / "events.jsonl").stat().st_size
        st = self.state(job, stamp_from=size, caps={})
        self.m._watch_stream(job, st)
        self.assertNotIn("rate_limit", job)  # caught up on old lines only
        self.write(job, [event])
        self.m._watch_stream(job, st)
        self.assertIn("observed_at", job["rate_limit"])  # a line written after the restore is live


class Launch(Base):
    def setUp(self):
        super().setUp()
        self.saved = os.environ.get("FLEET_LEAN_PROFILE")

    def tearDown(self):
        if self.saved is None:
            os.environ.pop("FLEET_LEAN_PROFILE", None)
        else:
            os.environ["FLEET_LEAN_PROFILE"] = self.saved
        super().tearDown()

    def test_lean_flags_per_kind(self):
        os.environ.pop("FLEET_LEAN_PROFILE", None)
        expect = {"build": "build", "fix": "fix", "review": "review", "re_review": "review",
                  "final_review": "review", "security_review": "review", "test": "test", "merge": "build",
                  "draft": "build"}
        for kind, profile in expect.items():
            job = self.job(kind=kind)
            cmd = self.m._command(job, "p", [], False)
            self.assertEqual(cmd[cmd.index("--setting-sources") + 1], "project", kind)
            self.assertIn("--strict-mcp-config", cmd)
            self.assertIn("--disable-slash-commands", cmd)
            settings = Path(cmd[cmd.index("--settings") + 1])
            self.assertTrue(settings.is_absolute())
            self.assertEqual(settings.parent, self.m._job_dir(job))
            text = settings.read_text()
            self.assertNotIn("{{", text)
            self.assertNotIn("allow", json.loads(text).get("permissions", {}))  # tool control: runner flags only
            if profile != "review":
                hook = json.loads(text)["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
                import shlex
                self.assertEqual(shlex.split(hook), [sys.executable, str(runner.WORKER_DIR / "quiet_bash.py")])
            mcp = Path(cmd[cmd.index("--mcp-config") + 1])
            self.assertTrue(mcp.is_absolute() and mcp.name == "mcp-empty.json")

    def test_flag_off_or_no_kind_keeps_the_old_launch(self):
        os.environ["FLEET_LEAN_PROFILE"] = "off"
        self.assertNotIn("--settings", self.m._command(self.job(kind="build"), "p", [], False))
        os.environ.pop("FLEET_LEAN_PROFILE")
        self.assertNotIn("--settings", self.m._command(self.job(), "p", [], False))

    def test_job_dir_env_is_set_for_a_kind(self):
        seen = {}
        real = runner.subprocess.Popen

        class Spy:
            def __init__(self, *args, **kw):
                seen.update(kw.get("env") or {})
                self.pid = 0
        runner.subprocess.Popen = Spy
        try:
            job = self.job(kind="build", state="running", exit_file="exit_code.1", cwd=str(self.code))
            self.m.jobs[job["id"]] = job
            try:
                self.m._launch(job, "p", [], False)
            except Exception:
                pass
        finally:
            runner.subprocess.Popen = real
        self.assertEqual(seen.get("FLEET_JOB_DIR"), str(self.m._job_dir(job)))

    def test_default_and_overridden_caps(self):
        saved = os.environ.pop("FLEET_TOKEN_CAPS", None)
        try:
            self.assertEqual(runner.token_caps("build"), {"turns": 120, "context": 150_000})
            self.assertEqual(runner.token_caps("security_review"), {"turns": 30, "context": 100_000})
            self.assertEqual(runner.token_caps("re_review"), {"turns": 15})
            self.assertEqual(runner.token_caps("test"), {"turns": 20})
            self.assertEqual(runner.token_caps(None), {})
            os.environ["FLEET_TOKEN_CAPS"] = json.dumps({"review": {"turns": 7}})
            self.assertEqual(runner.token_caps("review"), {"turns": 7})
            os.environ["FLEET_TOKEN_CAPS"] = "not json"
            self.assertEqual(runner.token_caps("fix"), {"turns": 120, "context": 150_000})
        finally:
            if saved is None:
                os.environ.pop("FLEET_TOKEN_CAPS", None)
            else:
                os.environ["FLEET_TOKEN_CAPS"] = saved


def flag(cmd, name):
    return cmd[cmd.index(name) + 1] if name in cmd else None


class LeanTools(Base):
    """Part H 3.1/3.7: the per-kind tool list is the real tool set (--tools), not only a pre-approval."""

    def setUp(self):
        super().setUp()
        self.m.cli_help = lambda agent: "--tools --effort --max-turns"

    def cmd(self, kind, **kw):
        return self.m._command(self.job(kind=kind, **kw), "p", [], False)

    def test_build_and_fix_get_only_the_six_tools(self):
        for kind in ("build", "fix"):
            cmd = self.cmd(kind)
            self.assertEqual(flag(cmd, "--tools"), "Read,Edit,Write,Bash,Grep,Glob", kind)
            self.assertEqual(flag(cmd, "--allowedTools"), "Read,Edit,Write,Bash,Grep,Glob", kind)

    def test_test_kind_gets_bash_so_a_haiku_test_can_run(self):
        cmd = self.cmd("test", model="claude-haiku-5-5")
        self.assertEqual(flag(cmd, "--tools"), "Bash")
        self.assertEqual(flag(cmd, "--allowedTools"), "Bash")

    def test_policy_still_narrows_the_lean_list(self):
        cmd = self.cmd("build", policy={"tools": {"claude_allow": ["Read", "Grep"]}})
        self.assertEqual(flag(cmd, "--tools"), "Read,Grep")
        self.assertEqual(flag(cmd, "--allowedTools"), "Read,Grep")

    def test_an_explicit_review_tool_list_is_unchanged(self):
        cmd = self.cmd("re_review", tools=["Read", "Grep", "Glob"])
        self.assertEqual(flag(cmd, "--tools"), "Read,Grep,Glob")

    def test_no_kind_or_lean_off_keeps_the_old_tools(self):
        self.assertNotIn("--tools", self.m._command(self.job(), "p", [], False))
        os.environ["FLEET_LEAN_PROFILE"] = "off"
        try:
            self.assertNotIn("--tools", self.cmd("build"))
        finally:
            os.environ.pop("FLEET_LEAN_PROFILE")

    def test_cli_without_tools_flag_is_not_given_one(self):
        self.m.cli_help = lambda agent: ""
        self.assertNotIn("--tools", self.cmd("build"))


class LeadCaps(Base):
    def body(self, **kw):
        return {"id": "lead1", "agent": "claude", "prompt": "x", "mode": "auto-edit", "cwd": str(self.code),
                "kind": "lead"} | kw

    def test_lead_without_a_turn_cap_is_refused_with_a_reason(self):
        for kind in ("lead", "orchestrator"):
            with self.assertRaises(runner.HttpError) as caught:
                self.m.create(self.body(kind=kind))
            self.assertEqual(caught.exception.status, 400)
            self.assertIn("max_turns", str(caught.exception.detail))

    def test_lead_with_caps_starts(self):
        job = self.m.create(self.body(max_turns=30))
        self.assertEqual(job["id"], "lead1")


class QuietBashRepeat(unittest.TestCase):
    """Part H 3.6: the third identical test command in a job is refused: "the gate will run this"."""
    HOOK = str(Path(runner.WORKER_DIR) / "quiet_bash.py")

    def run_hook(self, command, job_dir):
        payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})
        done = subprocess.run([sys.executable, self.HOOK], input=payload, capture_output=True, text=True,
                              env=dict(os.environ, FLEET_JOB_DIR=job_dir))
        return json.loads(done.stdout) if done.stdout.strip() else None

    def test_third_identical_run_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            cmd = "python3 -m unittest tests/test_a.py"
            for _ in range(2):
                out = self.run_hook(cmd, d)
                self.assertIn("updatedInput", out["hookSpecificOutput"])
            out = self.run_hook(cmd, d)["hookSpecificOutput"]
            self.assertEqual(out["permissionDecision"], "deny")
            self.assertIn("the gate will run this", out["permissionDecisionReason"])

    def test_different_commands_and_other_jobs_count_apart(self):
        with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as e:
            for _ in range(2):
                self.run_hook("python3 -m unittest tests/test_a.py", d)
            out = self.run_hook("python3 -m unittest tests/test_b.py", d)
            self.assertNotIn("permissionDecision", out["hookSpecificOutput"])
            out = self.run_hook("python3 -m unittest tests/test_a.py", e)
            self.assertNotIn("permissionDecision", out["hookSpecificOutput"])

    def test_whitespace_does_not_hide_a_repeat(self):
        with tempfile.TemporaryDirectory() as d:
            self.run_hook("python3 -m unittest  tests/test_a.py", d)
            self.run_hook("python3 -m unittest tests/test_a.py ", d)
            out = self.run_hook("python3   -m unittest tests/test_a.py", d)["hookSpecificOutput"]
            self.assertEqual(out["permissionDecision"], "deny")

    def test_non_test_commands_are_never_counted(self):
        with tempfile.TemporaryDirectory() as d:
            for _ in range(4):
                self.assertIsNone(self.run_hook("ls -la", d))


def git(*args, cwd):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd, check=True,
                   capture_output=True)


class Continuation(Caps):
    """Part H 3.3: a continuation job reuses the capped job's worktree and branch."""

    def setUp(self):
        super().setUp()
        git("init", "-q", "-b", "main", cwd=self.code)
        (self.code / "a.txt").write_text("a")
        git("add", "-A", cwd=self.code)
        git("commit", "-qm", "init", cwd=self.code)

    def capped(self):
        self.fake(ENDLESS)
        os.environ["FLEET_TOKEN_CAPS"] = json.dumps({"build": {"turns": 2}})
        job = self.start(self.body(id="cap1", cwd=None, repo=str(self.code), worktree=True))
        self.wait(job)
        self.assertEqual(job["state"], "capped")
        return job

    def test_continuation_keeps_worktree_and_branch(self):
        old = self.capped()
        new = self.start(self.body(id="cap2", cwd=None, repo=str(self.code), worktree=True, continue_job="cap1"))
        self.assertEqual((new["cwd"], new["branch"], new["base"]), (old["cwd"], old["branch"], old["base"]))
        self.assertEqual(old["worktree_moved_to"], "cap2")
        listing = subprocess.run(["git", "worktree", "list"], cwd=self.code, capture_output=True, text=True)
        self.assertEqual(len(listing.stdout.strip().splitlines()), 2)  # main + the one shared worktree

    def test_cleaning_up_the_old_job_leaves_the_shared_worktree(self):
        old = self.capped()
        self.start(self.body(id="cap2", cwd=None, repo=str(self.code), worktree=True, continue_job="cap1"))
        self.m.cleanup(old)
        self.assertTrue(Path(old["cwd"]).exists())

    def test_only_a_capped_job_can_be_continued(self):
        self.fake(ENDLESS)
        with self.assertRaises(runner.HttpError):
            self.m.create(self.body(id="cap3", cwd=None, repo=str(self.code), worktree=True, continue_job="nope"))

    def test_commits_lists_the_branch_log(self):
        old = self.capped()
        (Path(old["cwd"]) / "b.txt").write_text("b")
        git("add", "-A", cwd=old["cwd"])
        git("commit", "-qm", "add b", cwd=old["cwd"])
        self.assertIn("add b", self.m.commits(old)["log"])


if __name__ == "__main__":
    unittest.main()
