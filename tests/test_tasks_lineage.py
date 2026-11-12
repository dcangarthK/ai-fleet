"""Task lineage, failure reasons and honest status (docs/SPEC-task-lineage.md section 6).
Run: uv run --with fastapi --with uvicorn --with httpx --with 'mcp<2' python -m unittest -v tests/test_tasks_lineage.py
"""
import asyncio
import datetime
import json
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from zoneinfo import ZoneInfo

# Reuse the controller test harness (fake runner, temp database, environment).
from tests.test_controller import (FakeRunner, TMP, W, reset_limits, settle_pipeline,  # noqa: E402
                                   unmetered_claude_rules, wait_for)

from fastapi.testclient import TestClient  # noqa: E402

import auth  # noqa: E402
import controller  # noqa: E402
import events  # noqa: E402
import store  # noqa: E402
import tasks  # noqa: E402
from store import db  # noqa: E402

def fresh_loop_state():
    """Each TestClient runs the app on its own event loop; a lock that was contended on an earlier loop stays
    bound to it, so start every client class from unbound module-level locks."""
    controller._tick_lock = asyncio.Lock()
    controller._pipeline_locks.clear()
    tasks._locks.clear()


SAST = ZoneInfo("Africa/Johannesburg")
LIMIT_TEXT = "You've hit your 5-hour limit · resets 2:30pm (Africa/Johannesburg)"


def limit_text_ahead(test) -> str:
    """A Claude limit message whose reset is later today (a passed time is no longer rolled to tomorrow)."""
    soon = datetime.datetime.now(SAST) + datetime.timedelta(hours=1)
    if soon.date() != datetime.datetime.now(SAST).date():
        test.skipTest("less than an hour left in the day")
    return f"You've hit your 5-hour limit · resets {soon.hour % 12 or 12}:{soon.minute:02d}{'am' if soon.hour < 12 else 'pm'} (Africa/Johannesburg)"


CODEX_LIMIT = "You've hit your usage limit. Upgrade to Pro, or try again at 1:00 PM."


def epoch(y, mo, d, h, mi, tz=SAST) -> float:
    return datetime.datetime(y, mo, d, h, mi, tzinfo=tz).timestamp()


def row(**kw) -> dict:
    base = {"id": "t-1", "parent_id": None, "link": None, "title": "T", "state": "failed", "failure_reason": None,
            "resets_at": None, "resolution": None, "result": None, "error": None, "gate": None,
            "gate_result": None, "exit_code": 1}
    return base | kw


class FailureReasonTests(unittest.TestCase):
    def fr(self, *args):
        return tasks.failure_reason(*args)

    def test_cancelled_and_interrupted_and_denied(self):
        self.assertEqual(self.fr(row(state="cancelled"), None), "cancelled")
        self.assertEqual(self.fr(row(state="interrupted"), {"state": "lost"}), "interrupted")
        self.assertEqual(self.fr(row(state="blocked", exit_code=0), {"denials": [{"tool_name": "Bash"}]}), "denied")

    def test_claude_limit_text(self):
        self.assertEqual(self.fr(row(), {"exit_code": 1, "result": LIMIT_TEXT}), "limit")
        self.assertEqual(self.fr(row(result=LIMIT_TEXT), None), "limit")

    def test_codex_limit_text(self):
        self.assertEqual(self.fr(row(), {"exit_code": 1, "result": CODEX_LIMIT}), "limit")

    def test_limit_beats_gate_and_error(self):
        t = row(gate="pytest")
        job = {"exit_code": 1, "result": LIMIT_TEXT, "gate": {"state": "failed"}}
        self.assertEqual(self.fr(t, job), "limit")

    def test_provider_limit_is_stricter_than_the_display_heuristic(self):
        pl = tasks.provider_limit
        self.assertTrue(pl(row(agent="claude", result=LIMIT_TEXT, exit_code=1)))
        self.assertTrue(pl(row(agent="codex", result="", error="error: " + CODEX_LIMIT, exit_code=1)))
        self.assertTrue(pl(row(agent="codex", result="", error=CODEX_LIMIT, exit_code=1)))
        discussing = "Notes: when the usage limit is hit it says it resets at 2:30 PM, so retry later."
        self.assertEqual(self.fr(row(), {"exit_code": 1, "result": discussing}), "limit")  # display unchanged
        self.assertFalse(pl(row(agent="claude", result=discussing, exit_code=1)))
        self.assertFalse(pl(row(agent="claude", result=LIMIT_TEXT, exit_code=0)))  # a clean exit is not a limit
        self.assertFalse(pl(row(agent="claude", result="Summary. " + LIMIT_TEXT, exit_code=1)))  # must start with it

    def test_limit_needs_reset_or_retry_wording(self):
        self.assertEqual(self.fr(row(), {"exit_code": 1, "result": "rate limit of the function exceeded"}), "error")

    def test_gate_failed(self):
        t = row(gate="pytest", exit_code=0)
        self.assertEqual(self.fr(t, {"exit_code": 0, "gate": {"state": "failed", "exit_code": 1}}), "gate_failed")
        # a stored task (gate_result as JSON text) says the same
        t = row(gate="pytest", exit_code=0, gate_result=json.dumps({"state": "failed"}))
        self.assertEqual(self.fr(t, None), "gate_failed")

    def test_exit_code_without_limit_text(self):
        self.assertEqual(self.fr(row(), {"exit_code": 1, "result": "it broke"}), "error")

    def test_start_error(self):
        self.assertEqual(self.fr(row(exit_code=None, error="could not start: runner: boom", gate="pytest"), None), "error")


class ParseResetTests(unittest.TestCase):
    def test_named_timezone_before_and_after_reset_time(self):
        before = epoch(2026, 11, 11, 13, 0)
        self.assertEqual(tasks.parse_reset(LIMIT_TEXT, before), epoch(2026, 11, 11, 14, 30))
        after = epoch(2026, 11, 11, 15, 0)
        self.assertIsNone(tasks.parse_reset(LIMIT_TEXT, after))  # already passed today: never rolled to tomorrow

    def test_try_again_uses_local_timezone(self):
        utc = ZoneInfo("UTC")
        got = tasks.parse_reset(CODEX_LIMIT, epoch(2026, 11, 11, 10, 0, utc), utc)
        self.assertEqual(got, epoch(2026, 11, 11, 13, 0, utc))
        self.assertIsNone(tasks.parse_reset(CODEX_LIMIT, epoch(2026, 11, 11, 14, 0, utc), utc))

    def test_a_reset_just_past_midnight_rolls_to_tomorrow_only_within_five_hours(self):
        utc = ZoneInfo("UTC")
        seen = epoch(2026, 11, 12, 22, 0, utc)
        self.assertEqual(tasks.parse_reset("limit reached, resets 1am", seen, utc), epoch(2026, 11, 13, 1, 0, utc))
        self.assertEqual(tasks.parse_reset("limit reached, resets 2:59am", seen, utc), epoch(2026, 11, 13, 2, 59, utc))
        self.assertIsNone(tasks.parse_reset("limit reached, resets 3:30am", seen, utc))  # 5.5 h ahead: not a 5-hour reset
        self.assertIsNone(tasks.parse_reset("try again at 1:47 PM", epoch(2026, 11, 12, 15, 0, utc), utc))

    def test_a_passed_time_never_limits_until_tomorrow(self):
        """t-54eebb94: "try again at 1:47 PM" seen at 15:00 gave a false 22-hour limit."""
        utc = ZoneInfo("UTC")
        text = "You've hit your usage limit. Try again at 1:47 PM."
        self.assertIsNone(tasks.parse_reset(text, epoch(2026, 11, 12, 15, 0, utc), utc))
        self.assertEqual(tasks.parse_reset(text, epoch(2026, 11, 12, 13, 0, utc), utc),
                         epoch(2026, 11, 12, 13, 47, utc))

    def test_twelve_oclock(self):
        utc = ZoneInfo("UTC")
        self.assertEqual(tasks.parse_reset("limit reached, resets 12:15am", epoch(2026, 1, 1, 0, 5, utc), utc),
                         epoch(2026, 1, 1, 0, 15, utc))

    def test_garbage_is_none(self):
        for text in ("", "nothing here", "resets soon", None, "resets 99:99pm"):
            self.assertIsNone(tasks.parse_reset(text, 1000.0), text)


class DisplayStatusTests(unittest.TestCase):
    NOW = epoch(2026, 11, 11, 13, 0)  # before the 14:30 reset used below

    def ds(self, now=None, **kw):
        return tasks.display_status(row(**kw), SAST, self.NOW if now is None else now)

    def test_running_and_queued(self):
        self.assertEqual(self.ds(state="queued"), {"tone": "running", "label": "Queued"})
        self.assertEqual(self.ds(state="starting"), {"tone": "running", "label": "Working"})
        self.assertEqual(self.ds(state="working"), {"tone": "running", "label": "Working"})

    def test_waiting_on_a_person(self):
        self.assertEqual(self.ds(state="awaiting_approval"), {"tone": "attention", "label": "Blocked: needs approval"})
        self.assertEqual(self.ds(state="blocked", failure_reason="denied"),
                         {"tone": "attention", "label": "Blocked: needs approval"})

    def test_limit_pause(self):
        got = self.ds(failure_reason="limit", resets_at=epoch(2026, 11, 11, 14, 30))
        self.assertEqual(got, {"tone": "paused", "label": "Paused: limit, resets 2:30 PM"})
        self.assertEqual(self.ds(failure_reason="limit"), {"tone": "paused", "label": "Paused: usage limit"})

    def test_limit_pause_expires_once_the_reset_time_passes(self):
        reset = epoch(2026, 11, 11, 14, 30)
        before = self.ds(failure_reason="limit", resets_at=reset, now=reset - 60)
        self.assertEqual(before, {"tone": "paused", "label": "Paused: limit, resets 2:30 PM"})
        for now in (reset, reset + 3600):
            self.assertEqual(self.ds(failure_reason="limit", resets_at=reset, now=now),
                             {"tone": "attention", "label": "Limit cleared: resume or hand off"})

    def test_limit_without_a_reset_time_is_likely_over_after_five_hours(self):
        ended = epoch(2026, 11, 11, 8, 0)
        young = self.ds(failure_reason="limit", ended_at=ended, now=ended + 4 * 3600)
        self.assertEqual(young, {"tone": "paused", "label": "Paused: usage limit"})
        old = self.ds(failure_reason="limit", ended_at=ended, now=ended + 5 * 3600 + 1)
        self.assertEqual(old, {"tone": "attention", "label": "Limit cleared: resume or hand off"})
        self.assertEqual(self.ds(failure_reason="limit", now=ended)["tone"], "paused")  # no ended_at: can't tell

    def test_other_failures(self):
        self.assertEqual(self.ds(failure_reason="gate_failed"), {"tone": "attention", "label": "Failed: gate"})
        self.assertEqual(self.ds(failure_reason="error"), {"tone": "attention", "label": "Failed: error"})
        self.assertEqual(self.ds(failure_reason=None), {"tone": "attention", "label": "Failed"})
        self.assertEqual(self.ds(state="interrupted", failure_reason="interrupted"),
                         {"tone": "attention", "label": "Interrupted"})

    def test_resolutions(self):
        resolved = {"kind": "resolved_by", "task_id": "t-2", "title": "The retry", "at": 1}
        self.assertEqual(self.ds(resolution=resolved), {"tone": "muted", "label": "Superseded by t-2"})
        elsewhere = {"kind": "done_elsewhere", "by": "Sonnet 5.5", "commit": "ec2ab20", "at": 1}
        self.assertEqual(self.ds(resolution=elsewhere), {"tone": "resolved", "label": "Done elsewhere · ec2ab20"})
        self.assertEqual(self.ds(resolution={"kind": "dismissed", "at": 1}), {"tone": "muted", "label": "Dismissed"})
        # dismissing a blocked task only mutes it; the state is still blocked
        self.assertEqual(self.ds(state="blocked", resolution={"kind": "dismissed", "at": 1}),
                         {"tone": "muted", "label": "Dismissed"})

    def test_a_running_task_ignores_a_stale_resolution(self):
        got = self.ds(state="working", resolution={"kind": "dismissed", "at": 1})
        self.assertEqual(got["tone"], "running")

    def test_ok_and_cancelled(self):
        self.assertEqual(self.ds(state="review"), {"tone": "ok", "label": "Ready for review"})
        self.assertEqual(self.ds(state="done"), {"tone": "ok", "label": "Done"})
        self.assertEqual(self.ds(state="cancelled"), {"tone": "muted", "label": "Cancelled"})


class AutoResolutionTests(unittest.TestCase):
    def items(self, *rows):
        return {r["id"]: r for r in rows}

    def test_retry_child_resolves_parent(self):
        parent = row(id="a")
        child = row(id="b", parent_id="a", link="retry", state="review")
        self.assertEqual(tasks.resolution_targets(child, self.items(parent, child)), ["a"])

    def test_fix_child_resolves_parent(self):
        parent = row(id="a", state="interrupted")
        child = row(id="b", parent_id="a", link="fix", state="done")
        self.assertEqual(tasks.resolution_targets(child, self.items(parent, child)), ["a"])

    def test_failed_review_is_not_resolved_by_a_build_success(self):
        build = row(id="a", state="review")
        review = row(id="r", parent_id="a", link="review", state="failed")
        fix = row(id="f", parent_id="a", link="fix", state="review")
        self.assertEqual(tasks.resolution_targets(fix, self.items(build, review, fix)), [])
        # and a successful review does not resolve a failed build
        failed_build = row(id="a")
        ok_review = row(id="r2", parent_id="a", link="review", state="review")
        self.assertEqual(tasks.resolution_targets(ok_review, self.items(failed_build, ok_review)), [])

    def test_a_later_review_resolves_an_earlier_failed_review(self):
        build = row(id="a", state="review")
        first = row(id="r1", parent_id="a", link="review", state="failed")
        second = row(id="r2", parent_id="a", link="review", state="review")
        self.assertEqual(tasks.resolution_targets(second, self.items(build, first, second)), ["r1"])

    def test_already_resolved_is_untouched(self):
        parent = row(id="a", resolution={"kind": "dismissed", "at": 1})
        child = row(id="b", parent_id="a", link="retry", state="review")
        self.assertEqual(tasks.resolution_targets(child, self.items(parent, child)), [])

    def test_walks_to_the_root_across_two_levels(self):
        root = row(id="a")
        mid = row(id="b", parent_id="a", link="retry")
        leaf = row(id="c", parent_id="b", link="resume", state="review")
        self.assertEqual(sorted(tasks.resolution_targets(leaf, self.items(root, mid, leaf))), ["a", "b"])

    def test_chain_stops_at_a_non_retry_link(self):
        root = row(id="a")
        review = row(id="r", parent_id="a", link="review")
        leaf = row(id="c", parent_id="r", link="retry", state="review")
        self.assertEqual(tasks.resolution_targets(leaf, self.items(root, review, leaf)), ["r"])


class MigrationTests(unittest.TestCase):
    def test_existing_database_migrates_additively(self):
        path = Path(tempfile.mkdtemp(prefix="fleet-mig-")) / "old.db"
        conn = sqlite3.connect(path)
        # the tasks table as it was before lineage: none of the new columns
        conn.execute("""CREATE TABLE tasks (
            id TEXT PRIMARY KEY, parent_id TEXT, title TEXT, agent TEXT, model TEXT, prompt TEXT,
            mode TEXT, repo TEXT, worktree INTEGER DEFAULT 1, gate TEXT, node_id TEXT,
            state TEXT, created_at REAL, updated_at REAL, started_at REAL, ended_at REAL,
            session_id TEXT, result TEXT, denials TEXT DEFAULT '[]', gate_result TEXT,
            exit_code INTEGER, branch TEXT, cwd TEXT, runs INTEGER DEFAULT 0, error TEXT,
            pending_message TEXT, agent_seconds REAL DEFAULT 0, run_started_at REAL)""")
        conn.execute("INSERT INTO tasks (id,title,agent,state,created_at,updated_at) VALUES ('old','Old','claude','failed',1,2)")
        conn.commit()
        conn.close()
        previous = store.DB_PATH
        store.DB_PATH = path
        try:
            store.init_db()
            store.init_db()  # idempotent
            with db() as c:
                got = c.execute("SELECT * FROM tasks WHERE id='old'").fetchone()
            view = tasks.task_view(got)
            got = dict(got)
        finally:
            store.DB_PATH = previous
        for column in ("link", "failure_reason", "resets_at", "resolution", "outcome"):
            self.assertIsNone(got[column], column)
        self.assertEqual(got["hidden"], 0)
        # limit-handoff prerequisites and the budget wait reason: additive, nothing rewritten
        self.assertEqual((got["profile"], got["account_label"], got["budget_wait"]), ("fleet", None, None))
        self.assertEqual((got["title"], got["state"]), ("Old", "failed"))
        self.assertEqual(view["failure_reason"], "error")  # derived on read, not stored
        self.assertEqual(view["display"], {"tone": "attention", "label": "Failed: error"})
        self.assertIsNone(view["resolution"])
        self.assertFalse(view["hidden"])


class LegacyRowTests(unittest.TestCase):
    """Failed rows stored before lineage have no failure_reason; the read path derives it."""

    @classmethod
    def setUpClass(cls):
        store.init_db()  # self-contained: the read path queries the tasks table, which another class may not have made

    def view(self, **kw):
        base = {"id": "old", "parent_id": None, "title": "Old", "agent": "claude", "state": "failed", "denials": "[]",
                "gate_result": None, "policy": "{}", "skills": "[]", "approved_tools": "[]", "exit_code": 1,
                "ended_at": epoch(2026, 11, 1, 10, 0), "updated_at": epoch(2026, 11, 1, 10, 0)}
        return tasks.task_view(base | kw)

    def test_old_limit_failure_is_a_limit_with_a_reset_time_not_a_red_failure(self):
        v = self.view(result=LIMIT_TEXT)
        self.assertEqual(v["failure_reason"], "limit")
        self.assertEqual(v["resets_at"], epoch(2026, 11, 1, 14, 30))
        # that reset was days ago, so it asks for a decision instead of claiming to be paused
        self.assertEqual(v["display"]["tone"], "attention")
        self.assertEqual(v["display"]["label"], "Limit cleared: resume or hand off")

    def test_old_limit_failure_with_unreadable_reset_time(self):
        v = self.view(result="You've hit your limit. Please try again later.")
        self.assertEqual((v["failure_reason"], v["resets_at"]), ("limit", None))
        self.assertEqual(v["display"]["label"], "Limit cleared: resume or hand off")

    def test_other_old_states(self):
        self.assertEqual(self.view(result="boom")["failure_reason"], "error")
        self.assertEqual(self.view(state="interrupted")["failure_reason"], "interrupted")
        self.assertEqual(self.view(state="blocked", exit_code=0)["failure_reason"], "denied")
        self.assertIsNone(self.view(state="review", exit_code=0).get("failure_reason"))

    def test_stored_reason_wins_and_nothing_is_written(self):
        v = self.view(failure_reason="gate_failed", result=LIMIT_TEXT)
        self.assertEqual(v["failure_reason"], "gate_failed")


class LineageApiTests(unittest.TestCase):
    def legacy(self, **kw):
        """A failed task as stored before lineage: no failure_reason, resets_at, display."""
        tid = "t-leg" + str(time.time_ns())[-8:]
        base = {"id": tid, "title": "Legacy", "agent": "claude", "model": None, "prompt": "p", "mode": "auto-edit",
                "node_id": "master", "state": "failed", "created_at": time.time(), "updated_at": time.time(),
                "ended_at": time.time() - 100, "exit_code": 1}
        base |= kw
        with db() as c:
            c.execute(f"INSERT INTO tasks ({','.join(base)}) VALUES ({','.join('?' * len(base))})", tuple(base.values()))
        return tid

    def test_work_and_task_agree_on_a_limit_message_after_a_long_result(self):
        long_output = ("x" * 6000) + "\n" + limit_text_ahead(self)
        tid = self.legacy(result=long_output)
        one = self.get(tid)
        item = self.work_item(tid)
        self.assertEqual(one["failure_reason"], "limit")
        self.assertEqual(item["attempts"][0]["failure_reason"], "limit")
        self.assertEqual(item["attempts"][0]["display"], one["display"])
        self.assertEqual(item["display"], one["display"])
        self.assertIsNotNone(one["resets_at"])

    def test_work_limit_returns_the_newest_items_with_their_attempts(self):
        old_root = self.legacy(title="Old work", updated_at=1000.0, created_at=1000.0)
        for n in range(5):
            self.legacy(title=f"Filler {n}", updated_at=2000.0 + n, created_at=2000.0 + n)
        busy = self.legacy(title="Busy work", updated_at=1001.0, created_at=1001.0)
        # an old root whose child is the newest activity of all: the work item counts as newest
        child = self.legacy(title="Late child", parent_id=busy, link="retry", state="review", exit_code=0,
                            updated_at=time.time() + 50, created_at=1002.0)
        top = self.c.get("/api/work?limit=1").json()
        self.assertEqual(len(top), 1)
        self.assertEqual(top[0]["root_id"], busy)
        self.assertEqual([a["id"] for a in top[0]["attempts"]], [busy, child])
        two = self.c.get("/api/work?limit=2").json()
        self.assertEqual(two[0]["root_id"], busy)
        self.assertNotIn(old_root, [w["root_id"] for w in two])

    @classmethod
    def setUpClass(cls):
        fresh_loop_state()
        cls.ctx = TestClient(controller.app, base_url="http://127.0.0.1")
        cls.c = cls.ctx.__enter__()
        unmetered_claude_rules()
        wait_for(lambda: any(n["id"] == "master" and n["runner_ok"] for n in cls.c.get("/api/fleet").json()["nodes"]))

    @classmethod
    def tearDownClass(cls):
        cls.ctx.__exit__(None, None, None)

    def setUp(self):
        reset_limits()  # a limit recorded by an earlier test is over: tasks here start straight away

    def tearDown(self):
        reset_limits()

    def task(self, **kw):
        r = self.c.post("/api/tasks", json={"agent": "claude", "prompt": "do the thing"} | kw, headers=W)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def get(self, tid):
        return self.c.get(f"/api/tasks/{tid}").json()

    def fail(self, tid, result="it broke", **job):
        FakeRunner.jobs[tid].update(state="exited", exit_code=1, result=result, **job)
        wait_for(lambda: self.get(tid)["state"] == "failed")
        return self.get(tid)

    def succeed(self, tid, **job):
        FakeRunner.jobs[tid].update(state="exited", exit_code=0, result="done", **job)
        wait_for(lambda: self.get(tid)["state"] == "review")
        return self.get(tid)

    def work_item(self, root_id, **query):
        qs = "&".join(f"{k}={v}" for k, v in query.items())
        items = self.c.get(f"/api/work?limit=500&{qs}").json()
        return next((i for i in items if i["root_id"] == root_id), None)

    # --- failure reasons end to end ---

    def test_limit_failure_is_paused_with_reset_time(self):
        t = self.task(title="Limit hit")
        text = limit_text_ahead(self)
        failed = self.fail(t["id"], text)
        self.assertEqual(failed["failure_reason"], "limit")
        resets = datetime.datetime.fromtimestamp(failed["resets_at"], SAST)
        self.assertEqual(f"{resets.hour % 12 or 12}:{resets.minute:02d}", text.split("resets ")[1].split("pm")[0].split("am")[0])
        self.assertGreater(failed["resets_at"], failed["ended_at"])
        self.assertLessEqual(failed["resets_at"] - failed["ended_at"], 86400)
        self.assertEqual(failed["display"]["tone"], "paused")
        self.assertTrue(failed["display"]["label"].startswith("Paused: limit, resets"))
        self.assertEqual(failed["root_id"], t["id"])

    def test_gate_denied_and_lost_job_reasons(self):
        gated = self.task(gate="pytest")
        FakeRunner.jobs[gated["id"]].update(state="exited", exit_code=0, gate={"state": "failed", "exit_code": 1})
        wait_for(lambda: self.get(gated["id"])["state"] == "failed")
        self.assertEqual(self.get(gated["id"])["failure_reason"], "gate_failed")
        self.assertEqual(self.get(gated["id"])["display"]["label"], "Failed: gate")

        denied = self.task()
        FakeRunner.jobs[denied["id"]].update(state="exited", exit_code=0, denials=[{"tool_name": "Bash"}])
        wait_for(lambda: self.get(denied["id"])["state"] == "blocked")
        self.assertEqual(self.get(denied["id"])["failure_reason"], "denied")

        lost = self.task()
        FakeRunner.jobs[lost["id"]].update(state="lost")
        wait_for(lambda: self.get(lost["id"])["state"] == "interrupted")
        self.assertEqual(self.get(lost["id"])["failure_reason"], "interrupted")

        plain = self.task()
        self.assertEqual(self.fail(plain["id"])["failure_reason"], "error")
        self.assertEqual(self.c.post(f"/api/tasks/{plain['id']}/cancel", headers=W).json()["failure_reason"], "cancelled")

    # --- creation, links ---

    def test_create_with_parent_and_link(self):
        parent = self.task()
        child = self.task(parent_id=parent["id"], link="retry")
        self.assertEqual((child["parent_id"], child["link"], child["root_id"]), (parent["id"], "retry", parent["id"]))
        grandchild = self.task(parent_id=child["id"], link="fix")
        self.assertEqual(grandchild["root_id"], parent["id"])
        plain = self.task()
        self.assertIsNone(plain["link"])
        self.assertFalse(plain["hidden"])

    def test_create_validation(self):
        parent = self.task()
        post = lambda **kw: self.c.post("/api/tasks", json={"agent": "claude", "prompt": "x"} | kw, headers=W)  # noqa: E731
        self.assertEqual(post(parent_id="t-nope", link="retry").status_code, 400)
        self.assertEqual(post(parent_id=parent["id"], link="sideways").status_code, 400)
        self.assertEqual(post(link="retry").status_code, 400)

    def test_check_tasks_are_hidden_and_need_no_parent(self):
        check = self.task(link="check", title="Availability check")
        self.assertTrue(check["hidden"])
        self.assertIsNone(check["parent_id"])
        self.assertIsNone(self.work_item(check["id"]))
        self.assertIsNotNone(self.work_item(check["id"], include_hidden=1))

    # --- automatic resolution ---

    def test_retry_that_succeeds_resolves_the_failed_attempt(self):
        first = self.task(title="First go")
        self.fail(first["id"])
        retry = self.task(title="Second go", parent_id=first["id"], link="retry")
        self.succeed(retry["id"])
        resolved = self.get(first["id"])
        self.assertEqual(resolved["resolution"]["kind"], "resolved_by")
        self.assertEqual(resolved["resolution"]["task_id"], retry["id"])
        self.assertEqual(resolved["display"], {"tone": "muted", "label": f"Superseded by {retry['id']}"})
        self.assertEqual(resolved["state"], "failed")  # the state machine is untouched

    def test_failed_review_is_not_resolved_by_a_later_build_step(self):
        build = self.task(title="Build")
        self.succeed(build["id"])
        review = self.task(title="Review", parent_id=build["id"], link="review")
        self.fail(review["id"])
        fix = self.task(title="Fix", parent_id=build["id"], link="fix")
        self.succeed(fix["id"])
        self.assertIsNone(self.get(review["id"])["resolution"])

    # --- resumed runs ---

    def test_resumed_build_is_one_work_item_with_both_runs(self):
        t = self.task(title="Resumable build")
        self.fail(t["id"], LIMIT_TEXT)
        with db() as c:  # the account's limit has reset (a resume passes admission, docs/SPEC-limit-handoff.md 4.5)
            c.execute("UPDATE agent_usage SET resets_at=? WHERE task_id=?", (time.time() - 1, t["id"]))
        r = self.c.post(f"/api/tasks/{t['id']}/message", json={"text": "carry on"}, headers=W)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["state"], "working")
        self.assertIsNone(r.json()["failure_reason"])
        resumed = [e for e in self.c.get("/api/events/list?limit=1000").json()
                   if e["task_id"] == t["id"] and e["type"] == "task.resumed"]
        self.assertEqual(len(resumed), 1)
        self.assertEqual(resumed[0]["data"]["failure_reason"], "limit")
        self.succeed(t["id"])
        item = self.work_item(t["id"])
        self.assertEqual(item["display"], {"tone": "ok", "label": "Ready for review"})
        self.assertEqual(len(item["attempts"]), 2)
        first, last = item["attempts"]
        self.assertEqual((first["id"], last["id"]), (t["id"], t["id"]))
        self.assertEqual(first["display"]["tone"], "paused")
        self.assertEqual(first["failure_reason"], "limit")
        self.assertEqual(last["display"]["label"], "Ready for review")

    # --- manual resolution ---

    def test_resolve_done_elsewhere(self):
        t = self.task(title="Finished by hand")
        self.fail(t["id"])
        post = lambda body: self.c.post(f"/api/tasks/{t['id']}/resolve", json=body, headers=W)  # noqa: E731
        self.assertEqual(post({"kind": "done_elsewhere", "by": "Sonnet", "commit": "not-hex"}).status_code, 400)
        self.assertEqual(post({"kind": "done_elsewhere", "commit": "ec2ab20"}).status_code, 400)  # needs by
        self.assertEqual(post({"kind": "done_elsewhere", "by": "Sonnet", "commit": "abc12"}).status_code, 400)
        self.assertEqual(post({"kind": "wat"}).status_code, 400)
        ok = post({"kind": "done_elsewhere", "by": "Sonnet 5.5 (orchestrator session)", "commit": "ec2ab20"})
        self.assertEqual(ok.status_code, 200, ok.text)
        body = ok.json()
        self.assertEqual(body["resolution"]["kind"], "done_elsewhere")
        self.assertEqual(body["display"]["tone"], "resolved")
        self.assertIn("Done elsewhere", body["display"]["label"])
        self.assertEqual(body["state"], "failed")
        types = [e["type"] for e in self.c.get("/api/events/list?limit=1000").json() if e["task_id"] == t["id"]]
        self.assertIn("task.resolved", types)

    def test_resolve_dismissed_and_wrong_state(self):
        working = self.task()
        self.assertEqual(self.c.post(f"/api/tasks/{working['id']}/resolve", json={"kind": "dismissed"}, headers=W).status_code, 409)
        failed = self.task()
        self.fail(failed["id"])
        r = self.c.post(f"/api/tasks/{failed['id']}/resolve", json={"kind": "dismissed", "note": "check"}, headers=W)
        self.assertEqual(r.json()["display"], {"tone": "muted", "label": "Dismissed"})
        self.assertEqual(self.c.post("/api/tasks/t-nope/resolve", json={"kind": "dismissed"}, headers=W).status_code, 404)

    def test_resolve_and_outcome_never_unblock_or_approve(self):
        blocked = self.task()
        FakeRunner.jobs[blocked["id"]].update(state="exited", exit_code=0, denials=[{"tool_name": "Bash"}])
        wait_for(lambda: self.get(blocked["id"])["state"] == "blocked")
        calls_before = len(FakeRunner.calls)
        r = self.c.post(f"/api/tasks/{blocked['id']}/resolve", json={"kind": "dismissed"}, headers=W)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["state"], "blocked")
        self.assertEqual(r.json()["display"]["tone"], "muted")
        r = self.c.post(f"/api/tasks/{blocked['id']}/outcome", json={"kind": "done"}, headers=W)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.get(blocked["id"])["state"], "blocked")
        self.assertEqual(self.get(blocked["id"])["approved_tools"], [])
        self.assertEqual([p for p, _ in FakeRunner.calls[calls_before:] if p.endswith("/message")], [])

        waiting = self.task(mode="supervised")
        self.assertEqual(waiting["state"], "awaiting_approval")
        self.assertEqual(self.c.post(f"/api/tasks/{waiting['id']}/resolve", json={"kind": "dismissed"}, headers=W).status_code, 409)
        self.c.post(f"/api/tasks/{waiting['id']}/outcome", json={"kind": "done"}, headers=W)
        self.assertEqual(self.get(waiting["id"])["state"], "awaiting_approval")
        self.assertNotIn(waiting["id"], FakeRunner.jobs)

    def test_resolve_and_outcome_need_admin(self):
        t = self.task()
        self.fail(t["id"])
        remote = TestClient(controller.app, base_url="https://master.tail.ts.net:8701")
        operate, admin = auth.create_token("operate", "op"), auth.create_token("admin", "ad")
        h = lambda tok: {"Authorization": f"Bearer {tok}", **W}  # noqa: E731
        for path, body in ((f"/api/tasks/{t['id']}/resolve", {"kind": "dismissed"}),
                           (f"/api/tasks/{t['id']}/outcome", {"kind": "abandoned"})):
            self.assertEqual(remote.post(path, json=body, headers=h(operate)).status_code, 403, path)
        self.assertEqual(remote.post(f"/api/tasks/{t['id']}/resolve", json={"kind": "dismissed"}, headers=h(admin)).status_code, 200)

    # --- outcomes ---

    def test_outcome_merged_needs_commit_and_marks_root_done(self):
        t = self.task(title="Merged work")
        self.succeed(t["id"])
        post = lambda body, tid=t["id"]: self.c.post(f"/api/tasks/{tid}/outcome", json=body, headers=W)  # noqa: E731
        self.assertEqual(post({"kind": "merged"}).status_code, 400)
        self.assertEqual(post({"kind": "merged", "commit": "zzzzzzz"}).status_code, 400)
        self.assertEqual(post({"kind": "bogus"}).status_code, 400)
        r = post({"kind": "merged", "commit": "ec2ab20"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["state"], "done")
        self.assertEqual(r.json()["outcome"]["kind"], "merged")
        item = self.work_item(t["id"])
        self.assertEqual(item["outcome"]["commit"], "ec2ab20")
        self.assertEqual(item["display"], {"tone": "ok", "label": "Merged · ec2ab20"})
        types = [e["type"] for e in self.c.get("/api/events/list?limit=1000").json() if e["task_id"] == t["id"]]
        self.assertIn("task.outcome", types)

    def test_outcome_only_on_a_root(self):
        parent = self.task()
        child = self.task(parent_id=parent["id"], link="retry")
        r = self.c.post(f"/api/tasks/{child['id']}/outcome", json={"kind": "done"}, headers=W)
        self.assertEqual(r.status_code, 400)

    def test_abandoned_leaves_the_task_alone(self):
        t = self.task()
        self.fail(t["id"])
        r = self.c.post(f"/api/tasks/{t['id']}/outcome", json={"kind": "abandoned"}, headers=W)
        self.assertEqual((r.json()["state"], r.json()["outcome"]["kind"]), ("failed", "abandoned"))
        self.assertEqual(self.work_item(t["id"])["display"]["label"], "Abandoned")

    # --- /api/work ---

    def test_work_groups_orders_and_lists_attempts(self):
        root = self.task(title="Grouped work", agent="claude")
        self.fail(root["id"], LIMIT_TEXT)
        reset_limits()  # the retry starts after the limit has reset
        time.sleep(0.02)
        retry = self.task(title="Grouped retry", parent_id=root["id"], link="retry")
        self.succeed(retry["id"])
        time.sleep(0.02)
        review = self.task(title="Grouped review", parent_id=retry["id"], link="review")
        # Starts run in the background and bump updated_at: let the review's start land before the other item's
        # activity, then settle the other item so it is unambiguously the newest (was flaky 1 in 4).
        wait_for(lambda: self.get(review["id"])["state"] == "working")
        self.succeed(review["id"])  # nothing in this item stays active (a running task's polls bump updated_at)
        time.sleep(0.02)
        other = self.task(title="Separate item")
        self.succeed(other["id"])
        items = self.c.get("/api/work?limit=500").json()
        ids = [i["root_id"] for i in items]
        self.assertIn(root["id"], ids)
        self.assertNotIn(retry["id"], ids)
        self.assertNotIn(review["id"], ids)
        self.assertLess(ids.index(other["id"]), ids.index(root["id"]))  # newest activity first
        item = self.work_item(root["id"])
        self.assertEqual(item["title"], "Grouped work")
        self.assertEqual([a["id"] for a in item["attempts"]], [root["id"], retry["id"], review["id"]])
        self.assertEqual([a["link"] for a in item["attempts"]], [None, "retry", "review"])
        self.assertEqual(item["attempts"][0]["display"]["label"], f"Superseded by {retry['id']}")
        self.assertEqual(item["display"], item["attempts"][-1]["display"])
        for key in ("id", "title", "agent", "model", "link", "state", "display", "failure_reason", "started_at", "ended_at"):
            self.assertIn(key, item["attempts"][0])
        self.assertEqual(len(self.c.get("/api/work?limit=1").json()), 1)

    def test_work_hides_check_attempts_unless_asked(self):
        root = self.task(title="With a check")
        check = self.task(title="probe", parent_id=root["id"], link="check")
        self.assertNotIn(check["id"], [a["id"] for a in self.work_item(root["id"])["attempts"]])
        self.assertIn(check["id"], [a["id"] for a in self.work_item(root["id"], include_hidden=1)["attempts"]])

    # --- pipelines ---

    def test_pipeline_review_tasks_link_to_the_build(self):
        FakeRunner.agents["codex"] = "/x/codex"
        self.c.post("/api/refresh", headers=W)
        try:
            body = {"title": "Lineage pipeline", "spec": "do it", "repo": "/r/project", "gate": "pytest",
                    "mode": "auto-edit", "builder": {"agent": "codex"},
                    "reviewers": [{"kind": "agent", "agent": "claude"}], "max_rounds": 1}
            p = self.c.post("/api/pipelines", json=body, headers=W).json()
            build = p["build_task"]
            def ready(x):
                if x["state"] == "building":  # re-apply until the pipeline moves on, so nothing can undo it
                    FakeRunner.jobs[build].update(state="exited", exit_code=0, gate={"state": "passed", "exit_code": 0})
                return x if x["reviews"].get("0", {}).get("task_id") else None
            reviewing = settle_pipeline(self.c, p["id"], ready)
            review = self.get(reviewing["reviews"]["0"]["task_id"])
            self.assertEqual((review["parent_id"], review["link"]), (build, "review"))
            self.assertIsNone(self.get(build)["link"])
            self.assertEqual(review["root_id"], build)
            self.c.post(f"/api/pipelines/{p['id']}/cancel", headers=W)
        finally:
            FakeRunner.agents["codex"] = None
            self.c.post("/api/refresh", headers=W)


class NotificationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fresh_loop_state()
        cls.ctx = TestClient(controller.app, base_url="http://127.0.0.1")
        cls.c = cls.ctx.__enter__()
        unmetered_claude_rules()
        wait_for(lambda: any(n["id"] == "master" and n["runner_ok"] for n in cls.c.get("/api/fleet").json()["nodes"]))

    @classmethod
    def tearDownClass(cls):
        cls.ctx.__exit__(None, None, None)

    def setUp(self):
        reset_limits()

    def tearDown(self):
        reset_limits()

    def failed(self, parent=None, link=None, text=LIMIT_TEXT):
        body = {"agent": "claude", "prompt": "x", "title": "Notified work"}
        if parent:
            body |= {"parent_id": parent, "link": link}
        tid = self.c.post("/api/tasks", json=body, headers=W).json()["id"]
        FakeRunner.jobs[tid].update(state="exited", exit_code=1, result=text)
        wait_for(lambda: self.c.get(f"/api/tasks/{tid}").json()["state"] == "failed")
        return tid

    def test_limit_failures_say_paused_not_failed(self):
        tid = self.failed()
        note = events.describe({"type": "task.failed", "task_id": tid, "data": {"title": "Notified work"}})
        self.assertEqual(note["title"], "Paused: usage limit")
        self.assertIn("resets", note["message"])
        reset_limits()  # a later, unrelated failure happens after the limit has reset
        other = self.failed(text="boom")
        note = events.describe({"type": "task.failed", "task_id": other, "data": {"title": "Notified work"}})
        self.assertEqual(note["title"], "Task failed")

    def test_resolution_and_outcome_events_do_not_notify(self):
        for kind in ("task.resolved", "task.outcome", "task.resumed"):
            self.assertIsNone(events.describe({"type": kind, "task_id": None, "data": {"title": "x"}}), kind)

    def test_one_paused_notice_per_work_item(self):
        out = TMP / "lineage-plugin.out"
        script = TMP / "lineage_plug.py"
        script.write_text(f"import sys; open({str(out)!r}, 'a').write(sys.stdin.read() + '\\n')")
        events.PLUGINS.append({"name": "lineage", "events": {"task.failed"}, "command": [sys.executable, str(script)]})
        try:
            root = self.failed()
            reset_limits()  # the retry starts after the limit has reset
            child = self.failed(parent=root, link="retry")
            for tid in (root, child):
                events.run_plugins({"id": 1, "ts": 0, "task_id": tid, "type": "task.failed", "data": {"title": "Notified work"}})
            time.sleep(1)
            lines = out.read_text().splitlines() if out.exists() else []
            self.assertEqual(len(lines), 1, lines)
            self.assertEqual(json.loads(lines[0])["notification"]["title"], "Paused: usage limit")
        finally:
            events.PLUGINS.pop()


if __name__ == "__main__":
    unittest.main()
