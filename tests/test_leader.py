"""Part G req 11 leader continuity. Run this file in its own process (scripts/gate.sh convention)."""
import time
import unittest

from tests.test_budget import events_of, rules
from tests.test_limit_handoff import HandoffBase, OPUS, SONNET

import leader
from store import db


def slot(agent, model, state="free", until=None):
    return {"node": "master", "agent": agent, "profile": "fleet", "account": f"{agent}/owner",
            "concrete_model": model, "state": state, "until": until, "allowed_kinds": ["lead"]}


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
        self.assertEqual((next_leader["account"], next_leader["model"]), ("codex/owner", "gpt-5.6-terra"))
        self.assertEqual(events_of("leader.assigned")[-1]["data"]["reason"], "account_limited")

    def test_falls_back_to_sonnet_when_codex_is_limited(self):
        self.plan()
        leader.handoff.resources = lambda **_: [slot("codex", "gpt-5.6-terra", "limited", time.time() + 30),
                                                slot("claude", SONNET)]
        current = self.tick()
        self.assertEqual((current["account"], current["model"]), ("claude/owner", SONNET))

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

    def test_owner_only_actions_are_refused_to_operate_scope(self):
        self.plan()
        operate = {"X-Fleet": "1", "X-Fleet-Scope": "operate"}
        self.assertEqual(self.c.put("/api/budget/rules", json={"body": rules()}, headers=operate).status_code, 403)
        # The plan endpoint is deliberately available to an orchestrator, while task approval remains owner-only.
        task = self.ok(agent="claude", model=OPUS, kind="lead", mode="supervised", worktree=False)
        self.assertEqual(self.c.post(f"/api/tasks/{task['id']}/approve", headers=operate).status_code, 403)


if __name__ == "__main__":
    unittest.main()
