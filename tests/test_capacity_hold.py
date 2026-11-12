"""Part G batch 2a (docs/SPEC-capacity-placement.md 3.3, 3.9, 3a): the migration compare-and-set refuses a task that was
re-queued mid-tick, a re-queued task stops retrying its resume after 3 failures, and the owner's `hold` freezes an
account (idempotent column migration, carried across rules saves, nothing moves onto or off a held account). Run:
uv run --with fastapi --with uvicorn --with httpx --with 'mcp<2' python -m unittest -v tests/test_capacity_hold.py
"""
import json
import unittest

from fastapi import HTTPException

from tests.test_budget import events_of, rules
from tests.test_capacity import CapacityBase
from tests.test_controller import W

import budget
import capacity
import handoff
import tasks
import usage
from store import db


class CompareAndSetTests(CapacityBase):
    def test_a_task_requeued_mid_tick_is_not_moved(self):
        self.mode("live")
        a, b, c = self.queued_behind_busy_accounts()
        self.free(b if c["account"] != "claude/spare" else a)
        ranked = tasks.get_task(c["id"])
        slot = capacity.choose_move(ranked, usage.now())
        self.assertIsNotNone(slot)
        with db() as conn:  # a limit failure re-queued it (runs >= 1, not_before set) after it was ranked
            conn.execute("UPDATE tasks SET runs=1, not_before=? WHERE id=?", (usage.now() + 3600, c["id"]))
        self.assertFalse(self.c.portal.call(capacity._apply_move, c["id"], slot, "test", ranked))
        after = tasks.get_task(c["id"])
        self.assertEqual((after["state"], after["account"]), ("queued", ranked["account"]))
        self.assertFalse(events_of("placement.moved"))


class RestartCapTests(CapacityBase):
    def requeued(self):
        self.mode("live")
        self.put_rules(rules(limits__on_task_limit={"*": "wait"}))
        a = self.build()
        self.build()
        self.limit(a["id"])
        task = tasks.get_task(a["id"])
        self.assertEqual(task["state"], "queued")
        return task

    def test_resume_failures_stop_after_three(self):
        task = self.requeued()
        usage.clock = lambda: task["not_before"] + 5
        real = tasks.runner_call

        async def refuse(*args, **kw):
            raise HTTPException(409, "runner refused")

        tasks.runner_call = refuse
        try:
            for i in range(3):
                self.assertEqual(tasks.get_task(task["id"])["state"], "queued", i)
                self.assertFalse(self.c.portal.call(capacity.restart_requeued, tasks.get_task(task["id"])))
        finally:
            tasks.runner_call = real
        after = tasks.get_task(task["id"])
        self.assertEqual(len([e for e in events_of("task.restart_failed") if e["task_id"] == task["id"]]), 3)
        gave_up = [e for e in events_of("task.restart_gave_up") if e["task_id"] == task["id"]]
        self.assertEqual(len(gave_up), 1)
        self.assertEqual(after["state"], "failed")  # try_start no longer sees it as queued: no more retries
        self.assertIn("3 tries", after["error"])

    def test_fewer_than_three_failures_keep_it_queued(self):
        task = self.requeued()
        usage.clock = lambda: task["not_before"] + 5
        real = tasks.runner_call

        async def refuse(*args, **kw):
            raise HTTPException(409, "runner refused")

        tasks.runner_call = refuse
        try:
            self.c.portal.call(capacity.restart_requeued, tasks.get_task(task["id"]))
        finally:
            tasks.runner_call = real
        self.assertEqual(tasks.get_task(task["id"])["state"], "queued")
        self.assertFalse(events_of("task.restart_gave_up"))


class HoldTests(CapacityBase):
    def tearDown(self):
        budget.replace_hold([], "test")
        super().tearDown()

    def test_hold_column_migration_is_idempotent(self):
        with db() as c:
            budget.ensure_hold_column(c)
            budget.ensure_hold_column(c)
            budget.ensure_hold_column(c)
            cols = [r[1] for r in c.execute("PRAGMA table_info(budget_rules)")]
        self.assertEqual(cols.count("hold"), 1)
        budget.seed_rules()
        budget.seed_rules()

    def test_hold_validates_account_keys(self):
        for bad in ("spare", "claude", "claude/", "bogus/x", "claude/a b", "claude/a[b]", 5, ""):
            self.assertFalse(budget.valid_hold_key(bad), bad)
        for good in ("claude/spare", "claude@m1/fleet", "codex/main"):
            self.assertTrue(budget.valid_hold_key(good), good)
        with self.assertRaises(ValueError):
            budget.replace_hold(["nonsense"], "owner")
        with self.assertRaises(ValueError):
            budget.replace_hold("claude/spare", "owner")

    def test_set_hold_is_a_new_version_and_survives_a_rules_save(self):
        before = budget.rules_row()["version"]
        saved = budget.replace_hold(["claude/spare", "claude/spare"], "owner")
        self.assertEqual(saved["version"], before + 1)
        self.assertEqual(budget.held_accounts(), {"claude/spare"})
        self.put_rules(rules())  # the owner edits other rules: hold carries forward
        self.assertEqual(budget.held_accounts(), {"claude/spare"})
        restored = budget.rules_row()
        self.assertEqual(json.loads(restored["hold"]), ["claude/spare"])
        budget.replace_hold([], "owner")
        self.assertEqual(budget.held_accounts(), set())

    def test_a_rules_body_cannot_set_hold(self):
        body = rules() | {"hold": ["claude/spare"]}
        with self.assertRaises(ValueError):
            budget.save_rules(body, "someone")
        self.assertEqual(budget.held_accounts(), set())

    def test_nothing_moves_onto_a_held_account(self):
        self.mode("live")
        a, b, c = self.queued_behind_busy_accounts()
        free = b if c["account"] != "claude/spare" else a
        target = free["account"]
        self.free(free)
        budget.replace_hold([target], "owner")
        self.assertIsNone(capacity.choose_move(tasks.get_task(c["id"]), usage.now()))
        self.assertEqual(self.tick(), [])
        self.assertEqual(tasks.get_task(c["id"])["account"], c["account"])
        budget.replace_hold([], "owner")  # released: the same move now happens
        self.assertEqual(self.tick(), [c["id"]])

    def test_nothing_moves_off_a_held_account(self):
        self.mode("live")
        a, b, c = self.queued_behind_busy_accounts()
        self.free(b if c["account"] != "claude/spare" else a)
        budget.replace_hold([c["account"]], "owner")
        self.assertIsNone(capacity.choose_move(tasks.get_task(c["id"]), usage.now()))
        self.assertEqual(self.tick(), [])
        self.assertEqual(tasks.get_task(c["id"])["account"], c["account"])

    def test_a_hold_set_after_ranking_stops_the_apply(self):
        self.mode("live")
        a, b, c = self.queued_behind_busy_accounts()
        self.free(b if c["account"] != "claude/spare" else a)
        ranked = tasks.get_task(c["id"])
        slot = capacity.choose_move(ranked, usage.now())
        self.assertIsNotNone(slot)
        budget.replace_hold([slot["account"]], "owner")
        self.assertFalse(self.c.portal.call(capacity._apply_move, c["id"], slot, "test", ranked))
        self.assertEqual(tasks.get_task(c["id"])["account"], c["account"])


class HoldDeltaTests(CapacityBase):
    def tearDown(self):
        budget.replace_hold([], "test")
        super().tearDown()

    def test_deltas_add_and_remove_one_account(self):
        budget.set_hold("claude/a", True, "owner")
        budget.set_hold("claude/b", True, "owner")
        self.assertEqual(budget.held_accounts(), {"claude/a", "claude/b"})
        self.assertEqual(budget.set_hold("claude/a", False, "owner")["hold"], ["claude/b"])
        with self.assertRaises(ValueError):
            budget.set_hold("nonsense", True, "owner")

    def test_concurrent_deltas_lose_no_update(self):
        import threading
        names = [f"claude/acct{i}" for i in range(8)]
        errors = []

        def hold(name):
            try:
                budget.set_hold(name, True, "owner")
            except Exception as exc:  # reported below
                errors.append(exc)

        threads = [threading.Thread(target=hold, args=(n,)) for n in names]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(budget.held_accounts(), set(names))


class HoldEverywhereTests(CapacityBase):
    def tearDown(self):
        budget.replace_hold([], "test")
        super().tearDown()

    def accounts(self, task):
        return {s["account"] for s in handoff.candidate_slots(tasks.get_task(task["id"]), budget.current_rules())}

    def test_handoff_candidates_skip_a_held_target(self):
        a = self.build()
        free = self.accounts(a)
        self.assertTrue(free, "a free spare slot is a candidate before the hold")
        budget.replace_hold(sorted(free), "owner")
        self.assertEqual(self.accounts(a), set())

    def test_handoff_off_a_held_account_has_no_candidates(self):
        a = self.build()
        self.assertTrue(self.accounts(a))
        budget.replace_hold([a["account"]], "owner")
        self.assertEqual(self.accounts(a), set())

    def test_the_handoff_apply_step_refuses_a_held_account(self):
        a = self.build()
        slot = handoff.candidate_slots(tasks.get_task(a["id"]), budget.current_rules())[0]
        budget.replace_hold([slot["account"]], "owner")  # held after the slot was chosen
        got = self.c.portal.call(handoff._claim_to_insert, tasks.get_task(a["id"]), slot, budget.current_rules(),
                                 usage.now())
        self.assertEqual(got["action"], "none")
        self.assertIn("hold", got["reason"])

    def test_corrupt_hold_json_records_an_event(self):
        budget._reported_bad_hold.clear()
        with db() as c:
            budget.ensure_hold_column(c)
            c.execute("UPDATE budget_rules SET hold='{not json' WHERE version=(SELECT MAX(version) FROM budget_rules)")
        self.assertEqual(budget.held_accounts(), set())
        budget.held_accounts()  # once per version
        bad = events_of("budget.hold_invalid")
        self.assertEqual(len(bad), 1)
        self.assertIn("JSON", bad[0]["data"]["error"])

    def test_invalid_keys_are_reported_and_valid_ones_kept(self):
        budget._reported_bad_hold.clear()
        with db() as c:
            budget.ensure_hold_column(c)
            c.execute("UPDATE budget_rules SET hold=? WHERE version=(SELECT MAX(version) FROM budget_rules)",
                      (json.dumps(["claude/spare", "junk"]),))
        self.assertEqual(budget.held_accounts(), {"claude/spare"})
        self.assertTrue(events_of("budget.hold_invalid"))

    def test_hold_route_is_admin_only(self):
        r = self.c.put("/api/budget/rules/hold", json={"account": "claude/spare", "hold": True},
                       headers=W | {"X-Fleet-Scope": "operate"})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(budget.held_accounts(), set())
        r = self.c.put("/api/budget/rules/hold", json={"account": "claude/spare", "hold": True}, headers=W)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(budget.held_accounts(), {"claude/spare"})
        self.assertEqual(self.c.get("/api/budget/rules/hold", headers=W).json(), {"hold": ["claude/spare"]})
        r = self.c.put("/api/budget/rules/hold", json={"account": "claude/spare", "hold": False}, headers=W)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(budget.held_accounts(), set())
        r = self.c.put("/api/budget/rules/hold", json={"account": "nonsense", "hold": True}, headers=W)
        self.assertEqual(r.status_code, 400)
        self.assertTrue(events_of("budget.hold_changed"))


if __name__ == "__main__":
    unittest.main()
