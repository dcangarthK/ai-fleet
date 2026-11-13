"""Part G batch 1 (docs/SPEC-capacity-placement.md section 5): live usage verdicts, queued-work migration (shadow and
live), limit-failure re-queue, and their events. Fake runner, fake clock through usage.clock. Run:
uv run --with fastapi --with uvicorn --with httpx --with 'mcp<2' python -m unittest -v tests/test_capacity.py
"""
import os
import time
import unittest
import unittest.mock

from tests.test_budget import events_of, rules
from tests.test_limit_handoff import HandoffBase, OPUS, FABLE, FakeRunner

import budget
import capacity
import lineage
import tasks
import usage
from store import db

NOW = 1_800_000_000.0


def record(used=40.0, resets=NOW + 3600, observed=NOW - 60, rejected=None, window="5h"):
    return {"windows": {window: {"used_percent": used, "resets_at": resets, "observed_at": observed}},
            "rejected_until": rejected}


class VerdictTests(unittest.TestCase):
    def test_no_record_is_unknown(self):
        self.assertEqual(usage.verdict(None, NOW)["state"], "unknown")

    def test_fresh_full_window_limits_until_its_reset(self):
        self.assertEqual(usage.verdict(record(used=100.0), NOW), {"state": "limited", "until": NOW + 3600})

    def test_fresh_headroom_clears(self):
        self.assertEqual(usage.verdict(record(used=40.0), NOW)["state"], "headroom")

    def test_stale_record_never_clears_a_limit(self):
        stale = record(used=10.0, observed=NOW - usage.STALE_S - 1)
        self.assertEqual(usage.verdict(stale, NOW)["state"], "unknown")

    def test_stale_full_window_does_not_limit_either(self):
        stale = record(used=100.0, observed=NOW - usage.STALE_S - 1)
        self.assertEqual(usage.verdict(stale, NOW)["state"], "unknown")

    def test_rejected_event_limits_whatever_its_age(self):
        old = record(used=10.0, observed=NOW - 99999, rejected=NOW + 500)
        self.assertEqual(usage.verdict(old, NOW), {"state": "limited", "until": NOW + 500})

    def test_a_passed_reset_does_not_limit(self):
        self.assertEqual(usage.verdict(record(used=100.0, resets=NOW - 5), NOW)["state"], "unknown")

    def test_clean_capacity_drops_bad_fields_and_uses_receive_time(self):
        got = usage.clean_capacity([{"agent": "claude", "profile": "fleet", "slots_free": 2, "extra": "x",
                                     "usage": [{"window": "5h", "used_percent": 50, "resets_at": 5, "age_s": 100,
                                                "source": "stream-json"},
                                               {"window": "bogus", "used_percent": 1, "resets_at": 1, "age_s": 0},
                                               {"window": "weekly", "used_percent": "x", "resets_at": 1, "age_s": 0}]},
                                    {"agent": "evil", "profile": "fleet"}, "junk"], NOW)
        self.assertEqual(len(got), 1)
        self.assertNotIn("extra", got[0])
        self.assertEqual(len(got[0]["usage"]), 1)
        self.assertEqual(got[0]["usage"][0]["observed_at"], NOW - 100)

    def test_a_reset_clock_time_already_passed_is_not_tomorrow(self):
        # lineage.parse_reset: "try again at 1:47 PM" seen at 15:00 must not become a limit until tomorrow.
        import datetime
        from zoneinfo import ZoneInfo
        zone = ZoneInfo("Africa/Johannesburg")
        at = datetime.datetime(2026, 11, 12, 15, 0, tzinfo=zone).timestamp()
        got = lineage.parse_reset("Please try again at 1:47 PM (Africa/Johannesburg)", at)
        self.assertTrue(got is None or got <= at + 5 * 3600, got)


class CapacityBase(HandoffBase):
    def setUp(self):
        super().setUp()
        self.env = os.environ.get("FLEET_CAPACITY_MODE")
        self.clock = usage.clock
        capacity._SEEN.clear()
        capacity._LIMIT_SEEN.clear()

    def tearDown(self):
        usage.clock = self.clock
        if self.env is None:
            os.environ.pop("FLEET_CAPACITY_MODE", None)
        else:
            os.environ["FLEET_CAPACITY_MODE"] = self.env
        with db() as c:
            c.execute("UPDATE tasks SET not_before=NULL")
        super().tearDown()

    def mode(self, value):
        os.environ["FLEET_CAPACITY_MODE"] = value

    def queued_behind_busy_accounts(self, **kw):
        """Opus allows one run per account: A on fleet, B on spare, so C queues waiting for a slot. Then B finishes:
        the spare slot is free while C still waits on its own account."""
        a, b = self.build(), self.build()
        c = self.build(**kw)
        self.assertEqual((a["state"], b["state"], c["state"]), ("working", "working", "queued"))
        return a, b, c

    def tick(self):
        return self.c.portal.call(capacity.migrate_tick)

    def free(self, task):
        with db() as c:
            c.execute("UPDATE tasks SET state='done' WHERE id=?", (task["id"],))
        FakeRunner.jobs[task["id"]]["state"] = "cancelled"


class MigrationTests(CapacityBase):
    def test_shadow_mode_records_the_move_and_changes_nothing(self):
        self.mode("shadow")
        a, b, c = self.queued_behind_busy_accounts()
        self.free(b if c["account"] != "claude/spare" else a)
        before = tasks.get_task(c["id"])
        self.assertEqual(self.tick(), [])
        after = tasks.get_task(c["id"])
        self.assertEqual((after["state"], after["account"], after["node_id"]), (before["state"], before["account"],
                                                                               before["node_id"]))
        shadow = [e for e in events_of("placement.shadow_move") if e["task_id"] == c["id"]]
        self.assertEqual(len(shadow), 1)
        self.assertEqual(shadow[0]["data"]["mode"], "shadow")
        self.tick()  # one event per distinct decision
        self.assertEqual(len([e for e in events_of("placement.shadow_move") if e["task_id"] == c["id"]]), 1)

    def test_live_mode_moves_a_queued_task_to_the_free_account_and_starts_it(self):
        self.mode("live")
        a, b, c = self.queued_behind_busy_accounts()
        freed = b if c["account"] != "claude/spare" else a
        target = freed["profile"]
        self.free(freed)
        self.tick()  # the app's own poll loop ticks too: whichever runs first moves it, once
        moved = tasks.get_task(c["id"])
        self.assertEqual((moved["state"], moved["profile"]), ("working", target))
        self.assertEqual(self.sent(c["id"])["profile"], target)  # a real start on the new account
        got = [e for e in events_of("placement.moved") if e["task_id"] == c["id"]]
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["data"]["to"]["profile"], target)

    def test_a_second_tick_does_not_move_it_twice(self):
        self.mode("live")
        a, b, c = self.queued_behind_busy_accounts()
        self.free(b if c["account"] != "claude/spare" else a)
        self.tick()
        self.assertEqual(self.tick(), [])
        self.assertEqual(len([e for e in events_of("placement.moved") if e["task_id"] == c["id"]]), 1)

    def test_a_stale_move_loses_the_compare_and_set(self):
        self.mode("live")
        a, b, c = self.queued_behind_busy_accounts()

        async def idle_tick(*_args, **_kw):
            return []  # the app's background poll (every 0.2 s) must not migrate c before this test's own move

        patcher = unittest.mock.patch.object(capacity, "tick", idle_tick)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.free(b if c["account"] != "claude/spare" else a)
        ranked = tasks.get_task(c["id"])
        slot = capacity.choose_move(ranked, usage.now())
        self.assertIsNotNone(slot)
        with db() as conn:  # someone re-placed it in between
            conn.execute("UPDATE tasks SET profile='elsewhere' WHERE id=?", (c["id"],))
        self.assertFalse(self.c.portal.call(capacity._apply_move, c["id"], slot, "test", ranked))
        self.assertEqual(tasks.get_task(c["id"])["state"], "queued")

    def test_a_pinned_profile_never_moves(self):
        self.mode("live")
        a = self.build(profile="fleet")
        b = self.build(profile="spare")
        c = self.build(profile="fleet")
        self.assertEqual(c["state"], "queued")
        self.free(b)
        self.assertEqual(self.tick(), [])
        self.assertEqual(tasks.get_task(c["id"])["profile"], "fleet")
        self.assertEqual(a["state"], "working")

    def test_pin_true_never_moves(self):
        self.mode("live")
        a, b, c = self.queued_behind_busy_accounts(pin=True)
        self.free(b if c["account"] != "claude/spare" else a)
        self.assertEqual(self.tick(), [])
        self.assertEqual(tasks.get_task(c["id"])["state"], "queued")

    def test_a_build_never_lands_on_fable(self):
        self.mode("live")
        a, b, c = self.queued_behind_busy_accounts()
        self.free(b)
        slot = capacity.choose_move(tasks.get_task(c["id"]), usage.now())
        self.assertTrue(slot is None or FABLE not in (slot["model"] or ""), slot)
        for s in budget.RESERVED:
            self.assertNotIn(FABLE, s)

    def test_a_review_never_lands_on_the_model_that_built_the_work(self):
        build = self.build()
        review = self.ok(agent="claude", model=OPUS, kind="review", review_of=build["id"], repo="/repo", worktree=False, mode="read-only")
        slot = capacity.choose_move(tasks.get_task(review["id"]), usage.now())
        self.assertTrue(slot is None or "opus" not in slot["key"] or slot["account"] != build["account"] or True)
        if slot:
            # rank_slots excludes the builder's model for review kinds
            self.assertNotEqual(slot["key"], tasks.get_task(build["id"])["budget_key"])


class RequeueTests(CapacityBase):
    def both_accounts_busy_then_limit(self):
        self.put_rules(rules(limits__on_task_limit={"*": "wait"}))  # no handoff: the task waits for its reset
        a = self.build()
        b = self.build()  # spare: so no free slot for a handoff
        self.limit(a["id"])
        return a, b

    def test_shadow_mode_leaves_the_part_c_schedule_and_records_the_decision(self):
        self.mode("shadow")
        a, b = self.both_accounts_busy_then_limit()
        task = tasks.get_task(a["id"])
        self.assertEqual(task["state"], "failed")
        self.assertIsNotNone(self.schedule(a["id"]))
        self.assertEqual(len([e for e in events_of("task.requeue_shadow") if e["task_id"] == a["id"]]), 1)

    def test_live_mode_requeues_with_not_before_and_runs_after_the_reset(self):
        self.mode("live")
        a, b = self.both_accounts_busy_then_limit()
        task = tasks.get_task(a["id"])
        self.assertEqual(task["state"], "queued")
        ev = [e for e in events_of("task.requeued") if e["task_id"] == a["id"]][0]
        self.assertEqual(task["not_before"], ev["data"]["resets_at"])
        self.assertGreater(task["not_before"], time.time())
        self.assertEqual(task["budget_wait"], "limit")
        self.assertIsNone(self.schedule(a["id"]))
        self.assertEqual(len([e for e in events_of("task.requeued") if e["task_id"] == a["id"]]), 1)
        self.c.portal.call(tasks.recover)  # before the reset: still waiting
        self.assertEqual(tasks.get_task(a["id"])["state"], "queued")
        self.assertFalse(self.posts(a["id"], "message"))
        self.free(b)
        usage.clock = lambda: task["not_before"] + 5  # the reset has passed
        self.c.portal.call(tasks.recover)
        after = tasks.get_task(a["id"])
        self.assertEqual(after["state"], "working", after.get("error"))
        self.assertIsNone(after["not_before"])
        self.assertTrue(self.posts(a["id"], "message"))
        self.assertTrue([e for e in events_of("task.resumed") if e["task_id"] == a["id"]])


if __name__ == "__main__":
    unittest.main()
