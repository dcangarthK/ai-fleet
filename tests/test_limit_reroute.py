"""Regression coverage for automatic rerouting after provider usage limits.

A limited task the existing machinery parks for its reset is correctly parked either in `failed` with a
resume_schedule row (Part C) or in `queued` with a not_before; these tests assert the time, never `blocked`.

Run:
uv run -q --with fastapi --with uvicorn --with httpx --with 'mcp<2' python -m unittest -v tests/test_limit_reroute.py
"""
import os
import time
import unittest

from tests.test_controller import FakeRunner, W
from tests.test_limit_handoff import CLAUDE_LIMIT, HandoffBase, OPUS

from store import db


class LimitRerouteTests(HandoffBase):
    def setUp(self):
        super().setUp()
        self.saved_mode = os.environ.get("FLEET_CAPACITY_MODE")

    def tearDown(self):
        FakeRunner.health_extra.pop("capacity", None)
        if self.saved_mode is None:
            os.environ.pop("FLEET_CAPACITY_MODE", None)
        else:
            os.environ["FLEET_CAPACITY_MODE"] = self.saved_mode
        super().tearDown()

    def parked_until(self, task_id):
        """When the task is parked for: a queued not_before, or a failed task's resume_schedule row."""
        task = self.get(task_id)
        self.assertNotEqual(task["state"], "blocked")
        if task["state"] == "queued":
            self.assertEqual(task["budget_wait"], "limit")
            return task["not_before"]
        self.assertEqual(task["state"], "failed")
        with db() as c:
            row = c.execute("SELECT at FROM resume_schedule WHERE task_id=?", (task_id,)).fetchone()
        self.assertIsNotNone(row, "a failed limit task must have a scheduled resume")
        return row["at"]

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

    def test_all_limited_accounts_leave_the_task_waiting_until_the_earliest_reset(self):
        source = self.build()
        earliest = time.time() + 600
        with db() as c:
            c.execute("INSERT INTO agent_usage (ts,task_id,run,agent,model,identity,outcome,resets_at,account) "
                      "VALUES (?,?,?,?,?,?,?,?,?)",
                      (time.time(), "t-spare-limit", 1, "claude", OPUS, "claude:" + OPUS, "limit", earliest,
                       "claude/spare"))
        self.limit(source["id"])

        # Part C waits for the reset the provider's limit message gave this task (plus its small margin).
        resets_at = self.get(source["id"])["resets_at"]
        parked = self.parked_until(source["id"])
        self.assertGreaterEqual(parked, resets_at)
        self.assertLess(parked, resets_at + 600)
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
        os.environ["FLEET_CAPACITY_MODE"] = "live"  # the near-limit hold applies in live mode only
        self.refresh()

        task = self.build()
        self.assertEqual((task["profile"], task["account"]), ("spare", "claude/spare"))
        self.assertEqual(self.sent(task["id"])["profile"], "spare")

    def test_explicit_profile_pin_waits_when_no_other_account_is_free(self):
        source = self.build(profile="fleet")
        with db() as c:  # the spare account is limited too, so there is nowhere to hand the task off to
            c.execute("INSERT INTO agent_usage (ts,task_id,run,agent,model,identity,outcome,resets_at,account) "
                      "VALUES (?,?,?,?,?,?,?,?,?)",
                      (time.time(), "t-spare-limit-pin", 1, "claude", OPUS, "claude:" + OPUS, "limit",
                       time.time() + 600, "claude/spare"))
        self.limit(source["id"], text=CLAUDE_LIMIT)

        self.assertGreater(self.parked_until(source["id"]), time.time())
        self.assertEqual(self.get(source["id"])["profile"], "fleet")
        self.assertEqual(self.children(source["id"]), [])

    def test_explicit_profile_pin_waits_even_when_another_account_is_free(self):
        source = self.build(profile="fleet")  # the spare account is free and eligible
        self.limit(source["id"], text=CLAUDE_LIMIT)

        self.assertGreater(self.parked_until(source["id"]), time.time())
        self.assertEqual(self.get(source["id"])["profile"], "fleet")
        self.assertEqual(self.children(source["id"]), [])


if __name__ == "__main__":
    unittest.main()
