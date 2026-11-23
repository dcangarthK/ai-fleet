"""Limit policy: plan 1.0 (regression tests pinning today's park / handoff / fail per entry point, see
docs/SPEC-limit-policy.md), 1.1 shadow (plan_limit logs a decision and a mismatch flag; callers unchanged),
1.2 (`same_node` flag) and 1.3 (decisions table and GET /api/decisions). Run:
uv run --with fastapi --with uvicorn --with httpx --with 'mcp<2' python -m unittest -v tests/test_limit_policy.py
"""
import os
import time
import unittest
import unittest.mock

from tests.test_budget import events_of
from tests.test_controller import W
from tests.test_limit_handoff import REVIEW_M, HandoffBase

import budget
import capacity
import decisions
import handoff
import limit_policy
import tasks
from store import db


def rows(task_id=None, kind=None, rule=None):
    return [d for d in decisions.list_decisions(task_id, 1000)
            if (kind is None or d["kind"] == kind) and (rule is None or d["rule"] == rule)]


class PinnedBehaviourTests(HandoffBase):
    """SPEC-limit-policy.md section 2: what each entry point does today. These must not change in P1.0/1.1."""

    def review(self, **kw):
        return self.ok(**({"agent": "claude", "model": REVIEW_M, "kind": "review", "mode": "read-only",
                           "worktree": False} | kw))

    def test_handle_limit_review_parks_at_reset_plus_delay(self):
        t = self.review()
        got = self.limit(t["id"])
        row = self.schedule(t["id"])
        self.assertAlmostEqual(row["at"], got["resets_at"] + handoff.RESUME_DELAY, delta=1)
        self.assertEqual(self.children(t["id"]), [])
        self.assertEqual(got["state"], "failed")  # parked as a schedule row; the task row is untouched

    def test_handle_limit_build_with_a_free_account_hands_off(self):
        src = self.build()
        self.limit(src["id"])
        self.assertEqual(len(self.children(src["id"])), 1)
        self.assertIsNone(self.schedule(src["id"]))

    def test_handle_limit_pinned_profile_parks_not_hands_off(self):
        src = self.build(profile="fleet")
        self.limit(src["id"])
        self.assertEqual(self.children(src["id"]), [])
        self.assertIn("pinned", self.schedule(src["id"])["reason"])

    def test_handle_limit_no_worktree_parks(self):
        src = self.build(worktree=False)
        self.limit(src["id"])
        self.assertEqual(self.children(src["id"]), [])
        self.assertEqual(self.schedule(src["id"])["reason"], "no worktree to checkpoint")

    def test_handle_limit_not_a_limit_failure_does_nothing(self):
        t = self.review()
        self.assertEqual(self.c.portal.call(handoff.handle_limit, t["id"]), {"action": "none"})

    def test_requeue_on_limit_shadow_mode_leaves_the_task_failed(self):
        self.put_rules(self_rules_wait())
        a, b = self.build(), self.build()
        with unittest.mock.patch.dict(os.environ, {"FLEET_CAPACITY_MODE": "shadow"}):
            self.limit(a["id"])
        task = tasks.get_task(a["id"])
        self.assertEqual((task["state"], task["not_before"]), ("failed", None))
        self.assertEqual(len([e for e in events_of("task.requeue_shadow") if e["task_id"] == a["id"]]), 1)

    def test_requeue_on_limit_live_waits_for_the_bare_reset(self):
        self.put_rules(self_rules_wait())
        with unittest.mock.patch.dict(os.environ, {"FLEET_CAPACITY_MODE": "live"}):
            a, b = self.build(), self.build()  # both accounts busy, as in test_capacity
            got = self.limit(a["id"])
        self.assertEqual(got["state"], "queued")
        [ev] = [e for e in events_of("task.requeued") if e["task_id"] == a["id"]]
        self.assertEqual(got["not_before"], ev["data"]["resets_at"])  # no resume delay, unlike handle_limit
        self.assertIsNone(self.schedule(a["id"]))


def self_rules_wait():
    from tests.test_limit_handoff import rules
    return rules(limits__on_task_limit={"*": "wait"})


class ShadowTests(HandoffBase):
    def test_plan_limit_is_fail_for_a_task_that_did_not_hit_a_limit(self):
        t = self.build()
        plan = limit_policy.plan_limit(tasks.get_task(t["id"]), budget.current_rules())
        self.assertIsInstance(plan, limit_policy.Fail)

    def test_plan_limit_hand_off_and_park_choices(self):
        src = self.build()
        self.fail_in_db(src["id"])
        task = tasks.get_task(src["id"])
        self.assertIsInstance(limit_policy.plan_limit(task, budget.current_rules()), limit_policy.Handoff)
        self.put_rules(self_rules_wait())
        park = limit_policy.plan_limit(task, budget.current_rules())
        self.assertIsInstance(park, limit_policy.Park)
        self.assertEqual(park.until, task["resets_at"])

    def test_park_is_logged_with_a_mismatch_flag_for_the_two_minute_delay(self):
        t = self.ok(agent="claude", model=REVIEW_M, kind="review", mode="read-only", worktree=False)
        got = self.limit(t["id"])
        [row] = rows(t["id"], "limit.park", "handle_limit")
        self.assertTrue(row["inputs"]["mismatch"])
        self.assertAlmostEqual(row["chosen"]["until"], got["resets_at"] + 120, delta=1)
        self.assertAlmostEqual(row["shadow_choice"]["until"], got["resets_at"], delta=1)
        self.assertEqual(len([e for e in events_of("limit.shadow_mismatch") if e["task_id"] == t["id"]]), 1)
        self.assertEqual(self.schedule(t["id"])["state"], "scheduled")  # behaviour unchanged

    def test_handoff_is_logged_without_a_mismatch_when_the_shadow_agrees(self):
        src = self.build()
        self.limit(src["id"])
        [row] = rows(src["id"], "limit.handoff")
        self.assertFalse(row["inputs"]["mismatch"])
        self.assertEqual(row["chosen"]["action"], "handoff")
        self.assertEqual(row["shadow_choice"]["slot"], row["chosen"]["slot"])
        self.assertEqual(len(self.children(src["id"])), 1)

    def test_requeue_shadow_path_records_a_decision(self):
        self.put_rules(self_rules_wait())
        a, b = self.build(), self.build()
        with unittest.mock.patch.dict(os.environ, {"FLEET_CAPACITY_MODE": "shadow"}):
            self.limit(a["id"])
        [row] = rows(a["id"], "limit.park", "requeue_on_limit")
        self.assertFalse(row["inputs"]["mismatch"])  # both wait for the bare reset

    def test_a_failing_shadow_never_changes_the_outcome(self):
        src = self.build()
        with unittest.mock.patch.object(limit_policy, "plan_limit", side_effect=RuntimeError("boom")):
            self.limit(src["id"])
        self.assertEqual(len(self.children(src["id"])), 1)
        self.assertEqual(rows(src["id"]), [])

    def test_a_raising_shadow_comparison_never_reaches_handle_limits_caller(self):
        src = self.build()
        with unittest.mock.patch.object(limit_policy, "shadow_compare", side_effect=RuntimeError("boom")):
            self.limit(src["id"])  # would raise out of after_settled if the block were unguarded
        self.assertEqual(len(self.children(src["id"])), 1)

    def test_mismatch_rules(self):
        park = {"action": "park", "until": 100.0}
        self.assertFalse(limit_policy.mismatch(park, {"action": "park", "until": 100.5}))
        self.assertTrue(limit_policy.mismatch(park, {"action": "park", "until": 220.0}))
        self.assertTrue(limit_policy.mismatch(park, {"action": "handoff", "slot": {}}))
        self.assertFalse(limit_policy.mismatch({"action": "handoff", "slot": {"a": 1}}, {"action": "handoff", "slot": {"a": 1}}))
        self.assertTrue(limit_policy.mismatch({"action": "handoff", "slot": {"a": 1}}, {"action": "handoff", "slot": {"a": 2}}))


class SameNodeTests(HandoffBase):
    def test_default_is_same_node_and_matches_the_explicit_flag(self):
        src = self.build()
        self.fail_in_db(src["id"])
        task, rules_ = tasks.get_task(src["id"]), budget.current_rules()
        default = handoff.candidate_slots(task, rules_)
        self.assertTrue(default)
        self.assertEqual(default, handoff.candidate_slots(task, rules_, same_node=True))
        self.assertEqual({s["node"] for s in default}, {task["node_id"]})

    def test_same_node_false_also_asks_the_other_nodes_after_the_own_one(self):
        src = self.build()
        self.fail_in_db(src["id"])
        task, rules_ = tasks.get_task(src["id"]), budget.current_rules()
        other = {"id": "elsewhere"}
        seen = []

        def fake(t, r, now, node):
            seen.append(node["id"])
            return [{"node": node["id"]}]

        with unittest.mock.patch.object(tasks, "node_rows", return_value=[other, {"id": task["node_id"]}]), \
                unittest.mock.patch.object(handoff, "_candidates_on", side_effect=fake):
            self.assertEqual([s["node"] for s in handoff.candidate_slots(task, rules_, same_node=False)],
                             [task["node_id"], "elsewhere"])
            self.assertEqual(seen, [task["node_id"], "elsewhere"])
            seen.clear()
            handoff.candidate_slots(task, rules_)
            self.assertEqual(seen, [task["node_id"]])  # default never leaves the node


class DecisionTests(HandoffBase):
    def test_same_placement_choice_is_recorded_once_and_a_change_again(self):
        decisions._LAST.clear()
        before = len(rows(kind="placement"))
        self.build()
        self.build()  # same inputs, same node: no second row
        self.assertEqual(len(rows(kind="placement")), before + 1)
        decisions._LAST[next(iter(decisions._LAST))] = "other-node"  # the choice differs from the last one recorded
        self.build()
        self.assertEqual(len(rows(kind="placement")), before + 2)

    def test_the_sweep_path_records_no_placement_row(self):
        decisions._LAST.clear()
        before = len(rows(kind="placement"))
        tasks.pick_node("claude", None, "auto-edit", None, record=False, task_id="t-sweep")
        self.assertEqual(len(rows(kind="placement")), before)
        tasks.pick_node("claude", None, "auto-edit", None, task_id="t-known")
        self.assertEqual(rows("t-known", "placement")[0]["task_id"], "t-known")

    def test_prune_deletes_old_rows_and_keeps_only_the_newest_n(self):
        now = time.time()
        old = decisions.record("t-old", "placement", {}, [], "r", "n")
        with db() as c:
            c.execute("UPDATE decisions SET ts=? WHERE id=?", (now - 15 * 86400, old))
        for i in range(5):
            decisions.record("t-new", "placement", {"i": i}, [], "r", "n")
        self.assertGreaterEqual(decisions.prune(now), 1)
        self.assertEqual(rows("t-old"), [])
        self.assertEqual(len(rows("t-new")), 5)
        decisions.prune(now, keep=2)
        self.assertEqual([r["inputs"]["i"] for r in rows("t-new")], [4, 3])

    def test_prune_runs_by_itself_every_n_inserts(self):
        with unittest.mock.patch.object(decisions, "PRUNE_EVERY", 1), \
                unittest.mock.patch.object(decisions, "prune") as prune:
            decisions.record("t-x", "placement", {}, [], "r", "n")
        prune.assert_called_once()

    def test_placement_is_recorded_and_readable_over_the_api(self):
        decisions._LAST.clear()
        before = len(rows(kind="placement"))
        self.build()
        placed = rows(kind="placement")
        self.assertEqual(len(placed), before + 1)
        self.assertIn(placed[0]["chosen"], [n["id"] for n in tasks.node_rows()])
        self.assertTrue(placed[0]["candidates"])
        self.assertEqual(placed[0]["inputs"]["agent"], "claude")

    def test_api_filters_by_task_and_is_read_only(self):
        src = self.build()
        self.limit(src["id"])
        r = self.c.get(f"/api/decisions?task_id={src['id']}")
        self.assertEqual(r.status_code, 200)
        self.assertIn("limit.handoff", [d["kind"] for d in r.json()])
        self.assertEqual({d["task_id"] for d in r.json()}, {src["id"]})
        self.assertEqual(self.c.post("/api/decisions", json={}, headers=W).status_code, 405)

    def test_record_failure_is_swallowed(self):
        with unittest.mock.patch.object(decisions, "db", side_effect=RuntimeError("down")):
            self.assertIsNone(decisions.record("t-x", "placement", {}, [], "r", "n"))


if __name__ == "__main__":
    unittest.main()
