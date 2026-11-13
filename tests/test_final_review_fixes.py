"""Fix round for the Codex final review t-adaecf2a: capped continuation, supervised pre-grants, leader near-limit and
holds. Run this file in its own process (scripts/gate.sh convention)."""
import json
import time
import unittest
from unittest.mock import patch

from tests.test_controller import FakeRunner
from tests.test_limit_handoff import HandoffBase, OPUS, SONNET

import budget
import handoff
import leader
import tasks
from store import db


def slot(account, model=SONNET):
    return {"node": "master", "agent": "claude", "profile": "fleet", "account": account, "model": "claude:claude-sonnet-*",
            "concrete_model": model, "state": "free", "until": None, "allowed_kinds": ["lead", "review"]}


class CappedContinuationTests(HandoffBase):
    def cap(self, task):
        FakeRunner.jobs[task["id"]].update(state="capped", exit_code=0, result="did half; left X", branch=f"fleet/{task['id']}",
                                           gate={"state": "skipped"})
        self.c.portal.call(tasks.poll_task, task["id"])
        return tasks.get_task(task["id"])

    def test_capped_build_continues_once_in_the_same_worktree(self):
        src = self.build()
        self.assertEqual(self.cap(src)["state"], "capped")
        [new] = self.children(src["id"])
        t = self.get(new)
        self.assertEqual((t["link"], t["kind"], t["agent"], t["profile"], t["worktree"]),
                         ("retry", "build", "claude", src["profile"], 1))
        self.assertEqual(self.sent(new)["continue_job"], src["id"])
        self.assertIn("did half; left X", t["prompt"])
        self.assertLessEqual(len(t["prompt"].encode()), tasks.CONTINUATION_PROMPT_MAX)
        self.assertEqual(self.get(src["id"])["resolution"]["task_id"], new)
        self.assertIsNone(self.c.portal.call(tasks.auto_continue, src["id"]))  # once per capped task
        self.assertEqual(self.children(src["id"]), [new])

    def test_deferred_continuation_stays_queued(self):
        src = self.build()
        FakeRunner.jobs[src["id"]].update(state="capped", exit_code=0, result="s", branch="fleet/x", gate={"state": "skipped"})
        with patch.object(tasks, "try_start", return_value=False):  # admission deferred: no start
            self.c.portal.call(tasks.poll_task, src["id"])
        [new] = self.children(src["id"])
        self.assertEqual(self.get(new)["state"], "queued")

    def test_review_kind_is_not_continued(self):
        src = self.ok(agent="claude", model=SONNET, kind="review", mode="read-only", worktree=False)
        self.assertIsNone(self.cap(src) and self.c.portal.call(tasks.auto_continue, src["id"]))
        self.assertEqual(self.children(src["id"]), [])

    def test_build_without_worktree_is_not_continued(self):
        src = self.build(worktree=False)
        FakeRunner.jobs[src["id"]].update(state="capped", exit_code=0, result="s", branch="fleet/x", gate={"state": "skipped"})
        self.c.portal.call(tasks.poll_task, src["id"])
        self.assertEqual(self.children(src["id"]), [])

    def test_repeated_polls_of_a_capped_build_make_one_child(self):
        src = self.build()
        self.cap(src)
        self.c.portal.call(tasks.poll_task, src["id"])
        self.c.portal.call(tasks.continuation_sweep)
        self.assertEqual(len(self.children(src["id"])), 1)

    def test_continuation_chain_stops_at_the_limit(self):
        src = self.build()
        FakeRunner.jobs[src["id"]].update(state="capped", exit_code=0, result="s", branch="fleet/x", gate={"state": "skipped"})
        with patch.object(tasks, "continuation_depth", return_value=3):
            self.c.portal.call(tasks.poll_task, src["id"])
        self.assertEqual(self.children(src["id"]), [])
        with db() as c:
            n = c.execute("SELECT COUNT(*) FROM events WHERE type='task.continuation_limit' AND task_id=?", (src["id"],)).fetchone()[0]
        self.assertEqual(n, 1)

    def test_continuation_keeps_the_task_policy_and_effort(self):
        src = self.build(policy={"limits": {"task_minutes": 7}}, effort="high")
        self.cap(src)
        [new] = self.children(src["id"])
        t = self.get(new)
        self.assertEqual(t["effort"], "high")
        self.assertLessEqual(t["policy"]["limits"]["task_minutes"], 7)


class SupervisedTests(HandoffBase):
    POLICY = {"auto_approve": {"agents": ["claude"], "denied_tools": True, "tools": ["Edit"]}}

    def test_supervised_gets_no_launch_grants(self):
        self.assertEqual(tasks.launch_tools("claude", self.POLICY, "supervised"), [])

    def test_supervised_denials_are_not_auto_approved(self):
        t = self.build(mode="supervised", gate=None)
        task = tasks.get_task(t["id"])
        task["policy"] = dict(task["policy"] or {}, **self.POLICY)
        with patch.object(tasks, "denied_tool_names", return_value=["Edit"]):
            self.c.portal.call(tasks.auto_approve_denials, task)
        self.assertFalse(self.posts(t["id"], "message"))


class LeaderTests(HandoffBase):
    def setUp(self):
        super().setUp()
        with db() as c:
            c.execute("DELETE FROM plan_items")
            c.execute("DELETE FROM leader")
        self.saved_resources = leader.handoff.resources

    def tearDown(self):
        leader.handoff.resources = self.saved_resources
        with db() as c:
            c.execute("DELETE FROM plan_items")
            c.execute("DELETE FROM leader")
        super().tearDown()

    def plan(self):
        body = {"items": [{"work_item": "w-1", "next_steps": ["go"], "status": "open"}]}
        r = self.c.put("/api/plan", json=body, headers={"X-Fleet": "1", "X-Fleet-Scope": "operate"})
        self.assertEqual(r.status_code, 200, r.text)

    def test_near_limit_slot_is_skipped_when_another_has_headroom(self):
        near = slot("claude/aaa-near")
        ok = slot("claude/zzz-ok")
        waits = lambda probe, now=None: ("limit", "near") if probe["account"] == "claude/aaa-near" else (None, None)
        with patch.object(leader.budget, "start_wait", side_effect=waits):
            picked = leader._lead_slots(time.time()) if not setattr(leader.handoff, "resources", lambda **_: [near, ok]) else []
        self.assertEqual([r["account"] for r in picked], ["claude/zzz-ok"])

    def test_queued_lead_is_waiting_not_settled(self):
        self.plan()
        leader.handoff.resources = lambda **_: [slot("claude/owner-max")]
        first = self.c.portal.call(leader.tick)
        with db() as c:
            c.execute("UPDATE tasks SET state='queued',budget_wait='limit' WHERE id=?", (first["session_task_id"],))
        self.assertIn("waiting", self.c.portal.call(leader.tick))
        self.assertFalse(leader.record()["halted"])

    def test_queued_lead_moves_to_a_ready_slot_on_another_account(self):
        self.plan()
        leader.handoff.resources = lambda **_: [slot("claude/owner-max")]
        first = self.c.portal.call(leader.tick)
        with db() as c:
            c.execute("UPDATE tasks SET state='queued',budget_wait='limit' WHERE id=?", (first["session_task_id"],))
            c.execute("UPDATE leader SET account='claude/owner-max' WHERE id=1")
        leader.handoff.resources = lambda **_: [slot("claude/owner-max"), slot("claude/spare")]
        waits = lambda probe, now=None: ("limit", "near") if probe["account"] == "claude/owner-max" else (None, None)
        with patch.object(leader.budget, "start_wait", side_effect=waits):
            second = self.c.portal.call(leader.tick)
        self.assertEqual(tasks.get_task(first["session_task_id"])["state"], "cancelled")
        self.assertNotEqual(second["session_task_id"], first["session_task_id"])

    def test_review_selector_prefers_a_slot_with_headroom(self):
        picks = []
        async def fake_create(body, scope=None):
            picks.append(body.profile)
            return {"id": "x"}
        near, ok = slot("claude/aaa"), slot("claude/zzz")
        near["profile"], ok["profile"] = "aaa", "zzz"
        near["allowed_kinds"] = ok["allowed_kinds"] = ["review"]
        waits = lambda probe, now=None: ("limit", "near") if probe["account"] == "claude/aaa" else (None, None)
        src = self.ok(agent="codex", kind="build")
        near["node"] = ok["node"] = tasks.get_task(src["id"])["node_id"]
        self.plan_pending(src["id"])
        leader.handoff.resources = lambda **_: [near, ok]
        with patch.object(leader.budget, "start_wait", side_effect=waits), patch.object(leader.tasks, "create_task", fake_create), \
                patch.object(leader.tasks, "get_task", return_value={"id": src["id"], "state": "done", "node_id": near["node"]}):
            self.c.portal.call(leader._queue_pending_reviews, time.time())
        self.assertEqual(picks, ["zzz"])

    def plan_pending(self, work_item):
        body = {"items": [{"work_item": work_item, "next_steps": ["go"], "status": "pending_review"}]}
        r = self.c.put("/api/plan", json=body, headers={"X-Fleet": "1", "X-Fleet-Scope": "operate"})
        self.assertEqual(r.status_code, 200, r.text)


class HeldAccountTests(HandoffBase):
    def test_held_account_is_not_free_and_is_never_ranked(self):
        with patch.object(budget, "held_accounts", return_value={"claude/spare"}):
            rows = handoff.resources()
            ranked = handoff.rank_slots("build")
        spare = [r for r in rows if r["account"] == "claude/spare"]
        self.assertTrue(spare and all(r["state"] == "held" and not r["idle"] for r in spare))
        self.assertFalse([r for r in ranked if r["account"] == "claude/spare"])
        self.assertTrue([r for r in ranked if r["account"] == "claude/owner-max"])


if __name__ == "__main__":
    unittest.main()
