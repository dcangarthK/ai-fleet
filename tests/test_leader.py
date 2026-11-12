"""Part G req 11 leader continuity. Run this file in its own process (scripts/gate.sh convention)."""
import time
import unittest

from tests.test_budget import events_of, rules
from tests.test_limit_handoff import HandoffBase, FABLE, OPUS, SONNET

import leader
from store import db


def slot(agent, model, state="free", until=None):
    account = "codex/owner-plus" if agent == "codex" else "claude/owner-max"
    return {"node": "master", "agent": agent, "profile": "fleet", "account": account,
            "concrete_model": model, "state": state, "until": until, "allowed_kinds": ["lead", "review"]}


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

    def plan(self, steps=None):
        body = {"items": [{"work_item": "w-1", "next_steps": steps or ["delegate build"], "status": "open"}]}
        response = self.c.put("/api/plan", json=body, headers={"X-Fleet": "1", "X-Fleet-Scope": "operate"})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def tick(self):
        return self.c.portal.call(leader.tick)

    def test_handover_on_limit_to_codex(self):
        self.plan()
        leader.handoff.resources = lambda **_: [slot("claude", OPUS)]
        first = self.tick()
        with db() as c:
            c.execute("UPDATE tasks SET state='failed',failure_reason='limit',resets_at=? WHERE id=?",
                      (time.time() + 3600, first["session_task_id"]))
        leader.handoff.resources = lambda **_: [slot("codex", "gpt-5.6-terra"), slot("claude", SONNET)]
        next_leader = self.tick()
        self.assertEqual((next_leader["account"], next_leader["model"]), ("codex/owner-plus", "gpt-5.6-terra"))
        self.assertEqual(events_of("leader.assigned")[-1]["data"]["reason"], "account_limited")

    def test_falls_back_to_sonnet_when_codex_is_limited(self):
        self.plan()
        leader.handoff.resources = lambda **_: [slot("codex", "gpt-5.6-terra", "limited", time.time() + 30),
                                                slot("claude", SONNET)]
        current = self.tick()
        self.assertEqual((current["account"], current["model"]), ("claude/owner-max", SONNET))

    def test_schedules_at_earliest_reset_when_none_has_capacity(self):
        self.plan()
        earlier = time.time() + 60
        leader.handoff.resources = lambda **_: [slot("codex", "gpt-5.6-terra", "limited", earlier + 30),
                                                slot("claude", SONNET, "limited", earlier)]
        self.assertEqual(self.tick(), {"waiting_until": earlier})
        self.assertEqual(leader.record()["not_before"], earlier)

    def test_event_is_emitted_and_lead_prompt_is_capped(self):
        self.plan(["x" * 1000] * 50)
        leader.handoff.resources = lambda **_: [slot("codex", "gpt-5.6-terra")]
        current = self.tick()
        event = events_of("leader.assigned")[-1]
        self.assertEqual(event["data"]["to"]["session_task_id"], current["session_task_id"])
        self.assertLessEqual(len(self.get(current["session_task_id"])["prompt"].encode()), 4096)
        self.assertEqual(self.get(current["session_task_id"])["launch_extra"]["max_turns"], 30)
        self.assertEqual(self.sent(current["session_task_id"])["policy"]["limits"]["task_minutes"], 30)

    def test_failed_lead_without_plan_progress_halts_instead_of_respawning(self):
        self.plan()
        leader.handoff.resources = lambda **_: [slot("codex", "gpt-5.6-terra")]
        first = self.tick()
        with db() as c:
            c.execute("UPDATE tasks SET state='failed',failure_reason='error' WHERE id=?", (first["session_task_id"],))
        self.assertEqual(self.tick(), {"halted": True})
        self.assertEqual(leader.record()["session_task_id"], first["session_task_id"])

    def test_blocked_plan_item_does_not_start_a_leader(self):
        body = {"items": [{"work_item": "w-1", "next_steps": ["owner input"], "status": "blocked"}]}
        self.assertEqual(self.c.put("/api/plan", json=body, headers={"X-Fleet": "1", "X-Fleet-Scope": "operate"}).status_code, 200)
        leader.handoff.resources = lambda **_: [slot("codex", "gpt-5.6-terra")]
        self.assertIsNone(self.tick())
        self.assertIsNone(leader.record())

    def test_limited_leader_is_cancelled_and_replaced_once(self):
        self.plan()
        leader.handoff.resources = lambda **_: [slot("codex", "gpt-5.6-terra")]
        first = self.tick()
        leader.handoff.resources = lambda **_: [slot("codex", "gpt-5.6-terra", "limited", time.time() + 60),
                                                slot("claude", SONNET)]
        second = self.tick()
        self.assertEqual(self.get(first["session_task_id"])["state"], "cancelled")
        self.assertEqual(self.get(second["session_task_id"])["parent_id"], first["session_task_id"])
        self.assertEqual(second["model"], SONNET)

    def test_plan_item_update_requires_current_revision(self):
        initial = self.plan()
        item = initial["items"][0]
        changed = self.c.put("/api/plan/w-1", json={"next_steps": ["review"], "status": "open",
                                                      "revision": item["revision"]},
                             headers={"X-Fleet": "1", "X-Fleet-Scope": "operate"})
        self.assertEqual(changed.status_code, 200, changed.text)
        self.assertGreater(changed.json()["revision"], initial["revision"])
        self.assertEqual(changed.json()["items"][0]["next_steps"], ["review"])
        stale = self.c.put("/api/plan/w-1", json={"next_steps": [], "status": "done", "revision": item["revision"]},
                           headers={"X-Fleet": "1", "X-Fleet-Scope": "operate"})
        self.assertEqual(stale.status_code, 409)

    def test_lead_kind_refuses_models_outside_the_allowlist(self):
        for agent, model in (("codex", "gpt-5.6-sol"), ("claude", FABLE), ("claude", "claude-haiku-5-5")):
            response = self.c.post("/api/tasks", json={"agent": agent, "model": model, "kind": "lead",
                                                         "prompt": "lead", "worktree": False}, headers={"X-Fleet": "1"})
            self.assertEqual(response.status_code, 400, response.text)

    def test_pending_codex_build_gets_a_claude_review_when_capacity_returns(self):
        build = self.ok(agent="codex", model="gpt-5.6-terra", kind="build", gate="pytest")
        with db() as c:
            c.execute("UPDATE tasks SET state='done' WHERE id=?", (build["id"],))
        body = {"items": [{"work_item": build["id"], "next_steps": [], "status": "pending_review"}]}
        self.assertEqual(self.c.put("/api/plan", json=body, headers={"X-Fleet": "1", "X-Fleet-Scope": "operate"}).status_code, 200)
        leader.handoff.resources = lambda **_: [slot("claude", SONNET)]
        self.assertIsNone(self.tick())
        plan = self.c.get("/api/plan", headers={"X-Fleet": "1", "X-Fleet-Scope": "operate"}).json()
        self.assertEqual(plan["items"][0]["status"], "review_queued")
        with db() as c:
            review = c.execute("SELECT * FROM tasks WHERE review_of=?", (build["id"],)).fetchone()
        self.assertIsNotNone(review)
        self.assertEqual(review["agent"], "claude")

    def test_failed_review_queue_does_not_stop_leader_tick(self):
        build = self.ok(agent="codex", model="gpt-5.6-terra", kind="build", gate="pytest")
        with db() as c:
            c.execute("UPDATE tasks SET state='done' WHERE id=?", (build["id"],))
        body = {"items": [{"work_item": build["id"], "next_steps": [], "status": "pending_review"},
                          {"work_item": "w-1", "next_steps": ["delegate build"], "status": "open"}]}
        self.assertEqual(self.c.put("/api/plan", json=body, headers={"X-Fleet": "1", "X-Fleet-Scope": "operate"}).status_code, 200)
        leader.handoff.resources = lambda **_: [slot("claude", SONNET), slot("codex", "gpt-5.6-terra")]
        original = leader.tasks.create_task

        async def refuse_review(task, **kwargs):
            if task.kind == "review":
                raise leader.HTTPException(429, "review budget refused")
            return await original(task, **kwargs)

        leader.tasks.create_task = refuse_review
        try:
            current = self.tick()
        finally:
            leader.tasks.create_task = original
        self.assertEqual(current["model"], "gpt-5.6-terra")
        self.assertEqual(events_of("leader.review_skipped")[-1]["data"]["work_item"], build["id"])

    def test_existing_unresolved_review_is_reused(self):
        build = self.ok(agent="codex", model="gpt-5.6-terra", kind="build", gate="pytest")
        review = self.ok(agent="claude", model=SONNET, kind="review", mode="read-only", worktree=False,
                         review_of=build["id"], parent_id=build["id"], link="review")
        with db() as c:
            c.execute("UPDATE tasks SET state='done' WHERE id=?", (build["id"],))
        body = {"items": [{"work_item": build["id"], "next_steps": [], "status": "pending_review"}]}
        self.assertEqual(self.c.put("/api/plan", json=body, headers={"X-Fleet": "1", "X-Fleet-Scope": "operate"}).status_code, 200)
        leader.handoff.resources = lambda **_: [slot("claude", SONNET)]
        self.assertIsNone(self.tick())
        with db() as c:
            reviews = c.execute("SELECT id FROM tasks WHERE review_of=?", (build["id"],)).fetchall()
        self.assertEqual([row["id"] for row in reviews], [review["id"]])
        plan = self.c.get("/api/plan", headers={"X-Fleet": "1", "X-Fleet-Scope": "operate"}).json()
        self.assertEqual(plan["items"][0]["status"], "review_queued")

    def test_owner_only_actions_are_refused_to_operate_scope(self):
        self.plan()
        operate = {"X-Fleet": "1", "X-Fleet-Scope": "operate"}
        self.assertEqual(self.c.put("/api/budget/rules", json={"body": rules()}, headers=operate).status_code, 403)
        # The plan endpoint is deliberately available to an orchestrator, while task approval remains owner-only.
        task = self.ok(agent="claude", model=OPUS, kind="lead", mode="supervised", worktree=False)
        self.assertEqual(self.c.post(f"/api/tasks/{task['id']}/approve", headers=operate).status_code, 403)


if __name__ == "__main__":
    unittest.main()
