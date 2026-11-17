"""Regression coverage for automatic rerouting after provider usage limits.

Run:
uv run -q --with fastapi --with uvicorn --with httpx --with 'mcp<2' python -m unittest -v tests/test_limit_reroute.py
"""
import json
import time
import unittest

from tests.test_controller import FakeRunner, W
from tests.test_limit_handoff import CLAUDE_LIMIT, FABLE, HandoffBase, OPUS

import tasks
from store import db


class LimitRerouteTests(HandoffBase):
    def tearDown(self):
        FakeRunner.health_extra.pop("capacity", None)
        super().tearDown()

    def test_limit_reroutes_to_spare_and_records_the_account_limit(self):
        source = self.build()
        self.limit(source["id"])

        [replacement] = self.children(source["id"])
        rerouted = self.get(replacement)
        self.assertEqual((rerouted["profile"], rerouted["account"]), ("spare", "claude/spare"))
        with db() as c:
            row = c.execute("SELECT resets_at FROM agent_usage WHERE task_id=? AND outcome='limit'",
                            (source["id"],)).fetchone()
        self.assertIsNotNone(row["resets_at"])
        events = self.c.get("/api/events/list?limit=1000", headers=W).json()
        self.assertTrue(any(e["type"] == "task.rerouted" and e["task_id"] == source["id"] for e in events))

    def test_all_limited_accounts_leave_the_task_queued_until_the_earliest_reset(self):
        source = self.build()
        earliest = time.time() + 600
        with db() as c:
            c.execute("INSERT INTO agent_usage (ts,task_id,run,agent,model,identity,outcome,resets_at,account) "
                      "VALUES (?,?,?,?,?,?,?,?,?)",
                      (time.time(), "t-spare-limit", 1, "claude", OPUS, "claude:" + OPUS, "limit", earliest,
                       "claude/spare"))
        self.limit(source["id"])

        queued = self.get(source["id"])
        self.assertEqual(queued["state"], "queued")
        self.assertEqual(queued["budget_wait"], "limit")
        self.assertAlmostEqual(queued["not_before"], earliest, delta=2)
        self.assertEqual(self.children(source["id"]), [])

    def test_near_limit_account_is_skipped_before_dispatch(self):
        reset = time.time() + 1800
        FakeRunner.health_extra["capacity"] = [{
            "agent": "claude", "profile": "fleet", "slots_free": 1,
            "usage": [{"window": "5h", "used_percent": 90, "resets_at": reset, "age_s": 0}],
        }, {
            "agent": "claude", "profile": "spare", "slots_free": 1,
            "usage": [{"window": "5h", "used_percent": 10, "resets_at": reset, "age_s": 0}],
        }]
        self.refresh()

        task = self.build()
        self.assertEqual((task["profile"], task["account"]), ("spare", "claude/spare"))
        self.assertEqual(self.sent(task["id"])["profile"], "spare")

    def test_explicit_profile_pin_waits_instead_of_rerouting(self):
        source = self.build(profile="fleet")
        self.limit(source["id"], text=CLAUDE_LIMIT)

        queued = self.get(source["id"])
        self.assertEqual(queued["state"], "queued")
        self.assertEqual(queued["profile"], "fleet")
        self.assertEqual(self.children(source["id"]), [])

    def test_auto_approve_held_by_a_limit_queues_until_reset_not_blocked(self):
        t = self.ok(agent="claude", model=FABLE, kind="review", mode="read-only", worktree=False)
        with db() as c:
            c.execute("UPDATE tasks SET state='blocked', exit_code=0, denials=? WHERE id=?",
                      (json.dumps([{"tool_name": "Read"}]), t["id"]))
            c.execute("INSERT INTO agent_usage (ts,task_id,run,agent,model,identity,outcome,resets_at,account) "
                      "VALUES (?,?,?,?,?,?,?,?,?)", (time.time(), "t-other", 1, "claude", FABLE, "claude:" + FABLE,
                                                     "limit", time.time() + 3600, "claude/owner-max"))
        FakeRunner.jobs[t["id"]].update(state="exited", exit_code=0)
        task = tasks.get_task(t["id"])
        task["policy"] = dict(task["policy"] or {}, auto_approve={"denied_tools": True, "agents": ["claude"]})
        self.c.portal.call(tasks.auto_approve_denials, task)
        got = self.get(t["id"])
        self.assertEqual((got["state"], got["budget_wait"]), ("queued", "limit"))
        self.assertGreater(got["not_before"], time.time())
        self.assertIsNone(got["ended_at"])


if __name__ == "__main__":
    unittest.main()
