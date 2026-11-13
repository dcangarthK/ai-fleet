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
            self.assertEqual(queued["account"], "claude/spare")
            self.assertIn("waiting for", queued["error"])
            shown = next(row for row in live.queue(usage.now()) if row["task_id"] == task["id"])
            self.assertEqual(shown["until"], resets["claude/spare"])
            self.assertIn("waiting for", shown["reason"])

        with patch.object(capacity.budget, "usage_records", return_value=near):
            self.c.portal.call(capacity.tick)
        before_reset = tasks.get_task(task["id"])
        self.assertEqual((before_reset["state"], before_reset["account"], before_reset["not_before"]),
                         ("queued", "claude/spare", resets["claude/spare"]))

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

    def test_migration_does_not_move_a_slot_waiter_onto_a_near_limit_account(self):
        self.mode("live")
        a, b, queued = self.queued_behind_busy_accounts()
        free = b if queued["account"] != b["account"] else a
        self.free(free)
        now = time.time()
        records = self.records({queued["account"]: 10, free["account"]: 95},
                               {queued["account"]: now + 300, free["account"]: now + 120})
        with patch.object(capacity.budget, "usage_records", return_value=records):
            self.c.portal.call(capacity.tick)
        after = tasks.get_task(queued["id"])
        self.assertEqual((after["state"], after["account"]), ("queued", queued["account"]))

    def test_timed_queue_drains_in_creation_order_when_one_slot_returns(self):
        self.mode("live")
        now = time.time()
        resets = {"claude/owner-max": now + 300, "claude/spare": now + 120}
        near = self.records({"claude/owner-max": 95, "claude/spare": 95}, resets)
        with patch.object(capacity.budget, "usage_records", return_value=near):
            first = self.build()
            second = self.build()
        self.assertEqual((first["account"], second["account"]), ("claude/spare", "claude/spare"))

        usage.clock = lambda: resets["claude/spare"] + 1
        headroom = self.records({"claude/owner-max": 95, "claude/spare": 10}, resets)
        with patch.object(capacity.budget, "usage_records", return_value=headroom):
            self.c.portal.call(tasks.recover)
        self.assertEqual(tasks.get_task(first["id"])["state"], "working")
        self.assertEqual(tasks.get_task(second["id"])["state"], "queued")

    def test_multiple_near_windows_wait_for_the_last_reset_on_the_target_account(self):
        self.mode("live")
        now = time.time()
        records = {
            "claude/owner-max": {"agent": "claude", "windows": {
                "5h": {"used_percent": 95, "resets_at": now + 300, "observed_at": now, "source": "test"},
                "weekly": {"used_percent": 95, "resets_at": now + 2000, "observed_at": now, "source": "test"}}},
            "claude/spare": {"agent": "claude", "windows": {
                "5h": {"used_percent": 95, "resets_at": now + 100, "observed_at": now, "source": "test"},
                "weekly": {"used_percent": 95, "resets_at": now + 900, "observed_at": now, "source": "test"}}},
        }
        with patch.object(capacity.budget, "usage_records", return_value=records):
            task = self.build()
        queued = tasks.get_task(task["id"])
        self.assertEqual((queued["account"], queued["not_before"]), ("claude/spare", now + 900))


if __name__ == "__main__":
    unittest.main()
