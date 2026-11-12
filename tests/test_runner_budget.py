"""Runner side of the paid-model budget (docs/SPEC-paid-budget.md 2.1, 3.1, 3.5, 3.6, 3.7).
Usage parsing runs on recorded-shape fixtures; no real agent is ever called.
Run: python3 -m unittest -v tests/test_runner_budget.py
"""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("fleet_runner_budget", ROOT / "nodes" / "runner.py")
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)

FAKE = r'''#!{python}
import json, sys
sys.stdin.read()
print(json.dumps({{"type": "system", "session_id": "s1"}}))
print(json.dumps({{"type": "result", "session_id": "s1", "result": "ok", "permission_denials": []}}))
'''

# Shapes as documented for `claude -p --output-format stream-json --verbose` (result event) and
# `codex exec --json` (turn.completed events). Values are made up.
CLAUDE_MULTI_TURN = [
    {"type": "system", "subtype": "init", "session_id": "c-1", "model": "claude-sonnet-5-5"},
    {"type": "assistant", "message": {"content": [{"type": "text", "text": "looking"}]}},
    {"type": "user", "message": {"content": [{"type": "tool_result", "content": "file"}]}},
    {"type": "assistant", "message": {"content": [{"type": "text", "text": "done"}]}},
    {"type": "result", "subtype": "success", "session_id": "c-1", "result": "done", "num_turns": 4,
     "total_cost_usd": 0.0731, "permission_denials": [],
     "usage": {"input_tokens": 120, "output_tokens": 900, "cache_read_input_tokens": 40000,
               "cache_creation_input_tokens": 3000},
     "modelUsage": {"claude-haiku-5-5": {"inputTokens": 10, "outputTokens": 5, "costUSD": 0.0001},
                    "claude-sonnet-5-5": {"inputTokens": 110, "outputTokens": 895, "costUSD": 0.073}}},
]
CODEX_TURNS = [
    {"type": "thread.started", "thread_id": "th-1"},
    {"type": "turn.started"},
    {"type": "item.completed", "item": {"type": "agent_message", "text": "step one"}},
    {"type": "turn.completed", "usage": {"input_tokens": 5000, "cached_input_tokens": 4000, "output_tokens": 300}},
    {"type": "turn.started"},
    {"type": "item.completed", "item": {"type": "agent_message", "text": "step two"}},
    {"type": "turn.completed", "usage": {"input_tokens": 7000, "cached_input_tokens": 6500, "output_tokens": 200}},
]


class Base(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(os.path.realpath(self.temp.name))
        self.fleet = self.base / "fleet"
        self.bin = self.base / "bin"
        self.bin.mkdir()
        for name in ("claude", "codex"):
            p = self.bin / name
            p.write_text(FAKE.format(python=sys.executable))
            p.chmod(0o755)
        self.code = self.base / "code"
        self.code.mkdir()
        self.fleet.mkdir()
        (self.fleet / "runner.json").write_text(json.dumps({"max_mode": "auto-edit", "allowed_roots": [str(self.code)]}))
        self.saved_find = runner.find_agents
        runner.find_agents = lambda _url: {"claude": str(self.bin / "claude"), "codex": str(self.bin / "codex"),
                                           "ollama": None}
        self.saved_env = os.environ.get("FLEET_CODEX_PROFILE")
        os.environ["FLEET_CODEX_PROFILE"] = str(self.base / "codex-fleet")
        self.m = runner.JobManager(self.fleet, "http://127.0.0.1:1")
        self.m.cli_help = lambda agent: ""  # by default the CLI advertises nothing extra

    def tearDown(self):
        runner.find_agents = self.saved_find
        if self.saved_env is None:
            os.environ.pop("FLEET_CODEX_PROFILE", None)
        else:
            os.environ["FLEET_CODEX_PROFILE"] = self.saved_env
        end = time.time() + 5  # let short fake runs finish so their monitor threads don't write into a removed dir
        while time.time() < end and any(job.get("state") == "running" for job in self.m.jobs.values()):
            for job in list(self.m.jobs.values()):
                self.m.refresh(job)
            time.sleep(0.05)
        time.sleep(0.1)
        for job in self.m.jobs.values():
            if job.get("state") == "running" and job.get("pid"):
                try:
                    os.killpg(job["pid"], 9)
                except OSError:
                    pass
        self.temp.cleanup()

    def job(self, agent="claude", **kw):
        job = {"id": "j-" + str(time.time_ns()), "agent": agent, "mode": "auto-edit", "runs": 1, "session_id": "s",
               "model": None, "policy": {}} | kw
        (self.fleet / "jobs" / job["id"]).mkdir(parents=True, exist_ok=True)
        return job

    def write_events(self, job, events, append=False):
        path = self.fleet / "jobs" / job["id"] / "events.jsonl"
        with open(path, "a" if append else "w", encoding="utf-8") as handle:
            for event in events:
                handle.write(json.dumps(event) + "\n")
        return path


class UsageParsing(Base):
    def test_claude_result_usage_cost_model_and_turns(self):
        job = self.job()
        self.write_events(job, CLAUDE_MULTI_TURN)
        self.m._parse_events(job)
        self.assertEqual(job["usage"], {"input": 3120, "output": 900, "cached": 40000, "cost_usd": 0.0731,
                                        "model": "claude-sonnet-5-5"})
        self.assertEqual(job["num_turns"], 4)

    def test_codex_turns_are_summed(self):
        job = self.job("codex", model="gpt-x")
        self.write_events(job, CODEX_TURNS)
        self.m._parse_events(job)
        # input counts uncached tokens only, the same meaning as Claude's
        self.assertEqual(job["usage"], {"input": 1500, "output": 500, "cached": 10500, "cost_usd": None,
                                        "model": "gpt-x"})
        self.assertIsNone(job["num_turns"])

    def test_resumed_run_counts_only_its_own_events(self):
        job = self.job("codex")
        path = self.write_events(job, CODEX_TURNS[:4])
        job["events_offset"] = path.stat().st_size
        self.write_events(job, [CODEX_TURNS[4], CODEX_TURNS[6]], append=True)
        self.m._parse_events(job)
        self.assertEqual((job["usage"]["input"], job["usage"]["output"], job["usage"]["cached"]), (500, 200, 6500))
        self.assertEqual(job["session_id"], "th-1")  # the session still comes from the whole log

    def test_no_usage_stays_null(self):
        job = self.job()
        self.write_events(job, [{"type": "system", "session_id": "x"},
                                {"type": "result", "session_id": "x", "result": "hi"}])
        self.m._parse_events(job)
        self.assertEqual(job["usage"], {"input": None, "output": None, "cached": None, "cost_usd": None, "model": None})
        self.assertIsNone(job["num_turns"])
        self.assertIn("usage", runner.PUBLIC_JOB_KEYS)
        self.assertIn("num_turns", runner.PUBLIC_JOB_KEYS)


CODEX_LIMIT_LINE = "error: You've hit your usage limit. Upgrade to Pro, or try again at 1:00 PM."


class CodexErrors(Base):
    def write_raw(self, job, lines):
        path = self.fleet / "jobs" / job["id"] / "events.jsonl"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def test_the_last_stderr_error_line_becomes_the_job_error(self):
        job = self.job("codex", exit_code=1)
        self.write_raw(job, [json.dumps(CODEX_TURNS[0]), json.dumps(CODEX_TURNS[1]),
                             "ERROR: something earlier", CODEX_LIMIT_LINE])
        self.m._parse_events(job)
        self.assertEqual(job["error"], CODEX_LIMIT_LINE)
        self.assertIn("error", runner.PUBLIC_JOB_KEYS)
        self.assertIn("error", self.m.public(job))

    def test_json_error_and_turn_failed_events_are_captured(self):
        job = self.job("codex", exit_code=1)
        self.write_raw(job, [json.dumps({"type": "error", "message": "stream broke"})])
        self.m._parse_events(job)
        self.assertEqual(job["error"], "stream broke")
        self.write_raw(job, [json.dumps({"type": "turn.failed", "error": {"message": "You've hit your usage limit, "
                                                                                       "try again at 1:00 PM"}})])
        self.m._parse_events(job)
        self.assertEqual(job["error"], "You've hit your usage limit, try again at 1:00 PM")

    def test_no_error_for_a_clean_run_and_only_this_runs_lines_count(self):
        job = self.job("codex", exit_code=0)
        self.write_raw(job, [CODEX_LIMIT_LINE])
        self.m._parse_events(job)
        self.assertIsNone(job["error"])  # exit 0: a stray error: line is not a failure
        job = self.job("codex", exit_code=1)
        path = self.write_raw(job, [CODEX_LIMIT_LINE])
        job["events_offset"] = path.stat().st_size  # a resumed run that printed no error of its own
        self.m._parse_events(job)
        self.assertIsNone(job["error"])

    def test_the_line_is_redacted_and_trimmed(self):
        job = self.job("codex", exit_code=1)
        self.write_raw(job, ["error: bad key sk-abcdefghijklmnopqrstuvwx and Bearer abc.def.ghi " + "x" * 3000])
        self.m._parse_events(job)
        self.assertNotIn("sk-abcdefghijklmnopqrstuvwx", job["error"])
        self.assertNotIn("abc.def.ghi", job["error"])
        self.assertLessEqual(len(job["error"]), 600)

    def test_claude_runs_never_take_error_lines(self):
        job = self.job("claude", exit_code=1)
        self.write_raw(job, ["error: not for claude"])
        self.m._parse_events(job)
        self.assertIsNone(job.get("error"))


class Commands(Base):
    def test_codex_effort_is_a_per_invocation_setting(self):
        job = self.job("codex", effort="medium")
        cmd = self.m._command(job, "p", [], False)
        self.assertIn('model_reasoning_effort="medium"', cmd)
        self.assertEqual(cmd[cmd.index('model_reasoning_effort="medium"') - 1], "-c")
        resumed = self.m._command(job, "p", [], True)
        self.assertIn('model_reasoning_effort="medium"', resumed)
        self.assertNotIn("model_reasoning_effort", " ".join(self.m._command(self.job("codex"), "p", [], False)))

    def test_claude_effort_only_when_the_cli_has_the_flag(self):
        job = self.job(effort="low")
        self.assertNotIn("--effort", self.m._command(job, "p", [], False))
        self.m.cli_help = lambda agent: "  --effort <level>  Effort level\n  --tools <tools...>\n"
        cmd = self.m._command(job, "p", [], False)
        self.assertEqual(cmd[cmd.index("--effort") + 1], "low")
        self.assertEqual(self.m.limits_applied(job), {"effort": "low", "max_turns": None, "tools": None})

    def test_review_tools_and_turn_cap(self):
        self.m.cli_help = lambda agent: "--tools <tools...>\n--allowedTools\n"
        job = self.job(mode="read-only", tools=["Read", "Grep", "Glob"], max_turns=8)
        cmd = self.m._command(job, "p", [], False)
        self.assertEqual(cmd[cmd.index("--tools") + 1], "Read,Grep,Glob")
        self.assertEqual(cmd[cmd.index("--allowedTools") + 1], "Read,Grep,Glob")
        self.assertNotIn("--max-turns", cmd)  # this CLI doesn't advertise one: nothing faked
        self.assertIsNone(self.m.limits_applied(job)["max_turns"])
        self.m.cli_help = lambda agent: "--tools <tools...>\n--max-turns <n>\n"
        cmd = self.m._command(job, "p", [], False)
        self.assertEqual(cmd[cmd.index("--max-turns") + 1], "8")
        # Policy narrowing still applies to the review list.
        job = self.job(mode="read-only", tools=["Read", "Grep"], policy={"tools": {"claude_allow": ["Read"]}})
        cmd = self.m._command(job, "p", [], False)
        self.assertEqual(cmd[cmd.index("--tools") + 1], "Read")

    def test_an_explicitly_empty_review_tool_list_means_no_tools(self):
        self.m.cli_help = lambda agent: "--tools <tools...>\n--allowedTools\n"
        job = self.job(mode="read-only", tools=[])
        cmd = self.m._command(job, "p", [], False)
        self.assertEqual(cmd[cmd.index("--tools") + 1], "")
        self.assertNotIn("--allowedTools", cmd)
        self.assertEqual(self.m.limits_applied(job)["tools"], [])
        # Policy narrowing down to nothing is the same thing.
        job = self.job(mode="read-only", tools=["Read"], policy={"tools": {"claude_allow": ["Grep"]}})
        cmd = self.m._command(job, "p", [], False)
        self.assertEqual(cmd[cmd.index("--tools") + 1], "")

    def test_an_empty_tool_list_is_refused_when_the_cli_cannot_express_it(self):
        body = {"id": "e1", "agent": "claude", "prompt": "x", "mode": "read-only", "cwd": str(self.code), "tools": []}
        with self.assertRaises(runner.HttpError) as caught:
            self.m.create(body)  # setUp's CLI help advertises no --tools flag
        self.assertEqual(caught.exception.status, 400)
        self.m.cli_help = lambda agent: "--tools <tools...>\n"
        self.m.create(body)

    def test_a_policy_narrowed_empty_tool_list_is_refused_when_the_cli_cannot_express_it(self):
        body = {"id": "n1", "agent": "claude", "prompt": "x", "mode": "read-only", "cwd": str(self.code),
                "tools": ["Read"], "policy": {"tools": {"claude_allow": ["Grep"]}}}
        # The effective list (Read narrowed by claude_allow Grep) is empty; the raw list is not.
        with self.assertRaises(runner.HttpError) as caught:
            self.m.create(body)  # setUp's CLI help advertises no --tools flag
        self.assertEqual(caught.exception.status, 400)
        self.assertNotIn("n1", self.m.jobs)
        self.m.cli_help = lambda agent: "--tools <tools...>\n"
        job = self.m.create(body | {"id": "n2"})
        cmd = self.m._command(self.m.jobs[job["id"]], "p", [], False)
        self.assertEqual(cmd[cmd.index("--tools") + 1], "")
        self.assertNotIn("--allowedTools", cmd)

    def test_jobs_report_the_profile_they_ran_under(self):
        job = self.m.create({"id": "p1", "agent": "codex", "prompt": "x", "mode": "read-only", "cwd": str(self.code)})
        self.assertEqual(job["profile"], "shared")  # nothing signed in on the fleet profile
        self.assertIsNone(job["account_label"])
        self.assertIn("profile", runner.PUBLIC_JOB_KEYS)
        self.assertIn("account_label", runner.PUBLIC_JOB_KEYS)

    def test_new_job_fields_are_validated(self):
        body = {"id": "v1", "agent": "claude", "prompt": "x", "mode": "read-only", "cwd": str(self.code)}
        for bad in ({"effort": "extreme"}, {"max_turns": 0}, {"tools": ["rm -rf /"]}, {"max_turns": True}):
            with self.assertRaises(runner.HttpError, msg=bad):
                self.m.create(body | bad)

    def test_health_reports_budget_support_and_profiles(self):
        self.assertTrue(self.m.budget_support())
        self.assertEqual(self.m.profiles()["codex_profile"], "shared")


class CodexProfile(Base):
    def test_env_uses_the_fleet_profile_only_once_signed_in(self):
        home = self.base / "codex-fleet"
        self.assertNotIn("CODEX_HOME", self.m.agent_env("codex", {}))
        home.mkdir()
        self.assertNotIn("CODEX_HOME", self.m.agent_env("codex", {}))  # not signed in yet: shared
        self.assertEqual(self.m.profiles()["codex_profile"], "shared")
        (home / "auth.json").write_text("{}")  # stands in for the owner's `codex login`; never read
        self.assertEqual(self.m.agent_env("codex", {})["CODEX_HOME"], str(home))
        self.assertEqual(self.m.profiles()["codex_profile"], "fleet")
        self.assertNotIn("CODEX_HOME", self.m.agent_env("claude", {}))

    def test_claude_profile(self):
        self.assertEqual(self.m.profiles({"CLAUDE_CONFIG_DIR": str(self.base)})["claude_profile"], "fleet")
        self.assertEqual(self.m.profiles({})["claude_profile"], "shared")


class CodexRateLimits(Base):
    """Part F 11: the newest `rate_limits` record in $CODEX_HOME/sessions/**/rollout-*.jsonl, reported on /health."""

    def setUp(self):
        super().setUp()
        self.home = self.base / "codex-home"
        (self.home / "sessions" / "2026" / "11" / "12").mkdir(parents=True)
        self.saved_home = os.environ.get("CODEX_HOME")
        os.environ["CODEX_HOME"] = str(self.home)
        runner.find_agents = lambda _url, **_kw: {"claude": None, "codex": None, "ollama": None}  # restored by Base

    def tearDown(self):
        if self.saved_home is None:
            os.environ.pop("CODEX_HOME", None)
        else:
            os.environ["CODEX_HOME"] = self.saved_home
        super().tearDown()

    def rollout(self, name, records, mtime):
        path = self.home / "sessions" / "2026" / "11" / "12" / f"rollout-{name}.jsonl"
        path.write_text("\n".join(json.dumps(r) if isinstance(r, dict) else r for r in records) + "\n")
        os.utime(path, (mtime, mtime))
        return path

    @staticmethod
    def record(primary, secondary, ts="2026-11-12T10:00:00Z"):
        return {"timestamp": ts, "type": "event_msg", "payload": {"type": "token_count", "info": None, "rate_limits": {
            "primary": {"used_percent": primary[0], "window_minutes": 300, "resets_at": primary[1]},
            "secondary": {"used_percent": secondary[0], "window_minutes": 10080, "resets_at": secondary[1]}}}}

    def test_reports_both_windows_from_the_newest_record(self):
        now = time.time()
        self.rollout("old", [self.record((90.0, 1), (80.0, 2))], now - 1000)
        self.rollout("new", [self.record((10.0, 100), (20.0, 200)), "not json",
                             {"type": "event_msg", "payload": {"type": "agent_message"}},
                             self.record((3.0, 1762999999), (56.0, 1763500000))], now - 10)
        got = self.m.codex_rate_limits()
        self.assertEqual(got["primary"], {"used_percent": 3.0, "resets_at": 1762999999, "window_minutes": 300})
        self.assertEqual(got["secondary"], {"used_percent": 56.0, "resets_at": 1763500000, "window_minutes": 10080})

    def test_health_carries_only_the_rate_limits(self):
        self.rollout("a", [self.record((3.0, 5), (56.0, 6))], time.time())
        report = runner.health_report(self.m)
        self.assertEqual(report["codex_rate_limits"]["primary"]["used_percent"], 3.0)
        self.assertEqual(set(report["codex_rate_limits"]), {"primary", "secondary", "seen_at"})

    def test_none_when_there_is_no_record(self):
        self.assertIsNone(self.m.codex_rate_limits())
        self.rollout("a", ["garbage", {"type": "session_meta"}], time.time())
        self.assertIsNone(self.m.codex_rate_limits())
        self.assertIsNone(runner.health_report(self.m)["codex_rate_limits"])

    def test_a_malformed_window_is_dropped(self):
        record = self.record((3.0, 5), (56.0, 6))
        record["payload"]["rate_limits"]["secondary"] = {"used_percent": "lots", "resets_at": "soon"}
        self.rollout("a", [record], time.time())
        got = self.m.codex_rate_limits()
        self.assertEqual(got["primary"]["used_percent"], 3.0)
        self.assertIsNone(got["secondary"])


class TreesAndReviewCwd(Base):
    def git(self, *args, cwd=None):
        return subprocess.run(["git", *args], cwd=cwd or self.repo, check=True, capture_output=True, text=True).stdout

    def setUp(self):
        super().setUp()
        self.repo = self.code / "repo"
        self.repo.mkdir()
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "t@example.test")
        self.git("config", "user.name", "T")
        (self.repo / "a.txt").write_text("one\n")
        (self.repo / "dist.js").write_text("built\n")  # tracked although .gitignore names it (like a committed dist)
        (self.repo / ".gitignore").write_text("dist.js\n")
        self.git("add", "-f", "a.txt", "dist.js", ".gitignore")
        self.git("commit", "-qm", "init")
        status, self.build = 200, self.m.create({"id": "build1", "agent": "claude", "prompt": "x", "mode": "auto-edit",
                                                  "repo": str(self.repo), "worktree": True})
        for _ in range(100):
            if self.m.jobs["build1"]["state"] != "running":
                break
            time.sleep(0.05)
            self.m.refresh(self.m.jobs["build1"])

    def index_bytes(self, cwd):
        git_dir = self.git("rev-parse", "--git-dir", cwd=cwd).strip()
        path = Path(git_dir if os.path.isabs(git_dir) else Path(cwd) / git_dir) / "index"
        return path.read_bytes() if path.exists() else b""

    def test_tree_hash_and_delta_leave_the_builder_index_alone(self):
        job = self.m.jobs["build1"]
        cwd = job["cwd"]
        (Path(cwd) / "a.txt").write_text("two\n")
        (Path(cwd) / "new.txt").write_text("fresh\n")
        before = self.index_bytes(cwd)
        first = self.m.tree(job)["tree"]
        self.assertRegex(first, r"^[0-9a-f]{40}$")
        self.assertEqual(self.index_bytes(cwd), before)
        self.assertEqual(self.m.tree(job)["tree"], first)  # nothing changed: same tree
        self.assertEqual(self.m.diff(job, since=first)["diff"].strip(), "")
        (Path(cwd) / "a.txt").write_text("three\n")
        delta = self.m.diff(job, since=first)["diff"]
        self.assertIn("-two", delta)
        self.assertIn("+three", delta)
        self.assertNotIn("new.txt", delta)
        (Path(cwd) / "dist.js").write_text("rebuilt\n")  # a tracked file that .gitignore names still counts
        self.assertIn("+rebuilt", self.m.diff(job, since=first)["diff"])
        self.assertEqual(self.index_bytes(cwd), before)
        with self.assertRaises(runner.HttpError):
            self.m.diff(job, since="not-a-tree; rm -rf /")

    def test_read_only_review_may_use_a_build_worktree_but_nothing_else_may(self):
        cwd = self.m.jobs["build1"]["cwd"]
        review = self.m.create({"id": "rev1", "agent": "claude", "prompt": "review", "mode": "read-only", "cwd": cwd})
        self.assertEqual(review["cwd"], cwd)
        with self.assertRaises(runner.HttpError) as caught:
            self.m.create({"id": "rev2", "agent": "claude", "prompt": "edit", "mode": "auto-edit", "cwd": cwd})
        self.assertEqual(caught.exception.status, 403)
        with self.assertRaises(runner.HttpError):  # the runner's own directory stays off limits
            self.m.create({"id": "rev3", "agent": "claude", "prompt": "x", "mode": "read-only", "cwd": str(self.fleet)})


if __name__ == "__main__":
    unittest.main()
