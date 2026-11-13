"""Part G requirement 10: near-limit admission, timed queue, and automatic drain."""
import time
import unittest
from unittest.mock import patch

from tests.test_capacity import CapacityBase

import capacity
import live
import tasks
import usage


class UsageWatcherTests(CapacityBase):
    def records(self, used_by_account, resets):
        now = usage.now()
        return {account: {"agent": "claude", "windows": {
            "5h": {"used_percent": used, "resets_at": resets[account], "observed_at": now,
                       "source": "test"}}}
            for account, used in used_by_account.items()}

    def test_all_near_limit_queues_until_the_earliest_reset_then_drains(self):
        self.mode("live")
        now = time.time()
        resets = {"claude/owner-max": now + 300, "claude/spare": now + 120}
        near = self.records({"claude/owner-max": 95, "claude/spare": 95}, resets)
        with patch.object(capacity.budget, "usage_records", return_value=near):
            task = self.build()
        queued = tasks.get_task(task["id"])
        self.assertEqual(queued["state"], "queued")
        self.assertEqual(queued["not_before"], resets["claude/spare"])
        self.assertIn("waiting for", queued["error"])
        shown = next(row for row in live.queue(usage.now()) if row["task_id"] == task["id"])
        self.assertEqual(shown["until"], resets["claude/spare"])
        self.assertIn("waiting for", shown["reason"])

        usage.clock = lambda: resets["claude/spare"] + 1
        clear = self.records({"claude/owner-max": 10, "claude/spare": 10}, resets)
        with patch.object(capacity.budget, "usage_records", return_value=clear):
            self.c.portal.call(tasks.recover)
        self.assertEqual(tasks.get_task(task["id"])["state"], "working")

    def test_scheduled_task_moves_to_new_headroom_before_its_reset(self):
        self.mode("live")
        now = time.time()
        resets = {"claude/owner-max": now + 300, "claude/spare": now + 120}
        all_near = self.records({"claude/owner-max": 95, "claude/spare": 95}, resets)
        with patch.object(capacity.budget, "usage_records", return_value=all_near):
            task = self.build()

        headroom = self.records({"claude/owner-max": 95, "claude/spare": 10}, resets)
        with patch.object(capacity.budget, "usage_records", return_value=headroom):
            self.c.portal.call(capacity.tick)
        moved = tasks.get_task(task["id"])
        self.assertEqual((moved["state"], moved["profile"]), ("working", "spare"))
        self.assertIsNone(moved["not_before"])


if __name__ == "__main__":
    unittest.main()
