"""Pipeline round arithmetic (T2) and blocked/awaiting_approval builders (T8a-T8d, AC17-AC19), with a fake controller
that records every call it is asked to make. docs/SPEC-batch-pipeline.md. Run: python3 -m unittest -v tests/test_pipeline_blocked.py
"""
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "master" / "controller"))
import pipeline as PL  # noqa: E402

DIFF = 'def f():\n+    return db.execute(f"SELECT {x}")\n'
ISSUE = {"severity": "high", "file": "app.py", "problem": "SQL injection", "evidence": 'db.execute(f"SELECT {x}")',
         "expected": "param", "actual": "f-string", "fix": "bind"}
CHANGES = json.dumps({"verdict": "request_changes", "issues": [ISSUE]})


def new(**kw):
    spec = {"title": "t", "spec": "s", "repo": "/r", "gate": "g", "builder": {"agent": "codex"},
            "reviewers": [{"kind": "agent", "agent": "claude", "model": "claude-fable-5-2"}], "max_rounds": 4}
    spec.update(kw)
    p = PL.new(spec)
    p, _ = PL.start(p)
    p["build_task"] = "t-build"
    return p


class FakeController:
    """Applies pipeline actions the way the controller does; records HTTP-ish calls. It has no approve method:
    anything calling one would raise AttributeError."""

    def __init__(self):
        self.calls = []

    def run(self, p, event):
        p, actions = PL.step(p, event)
        for a in actions:
            self.calls.append(a["do"])
        return p, actions


class Rounds(unittest.TestCase):
    def test_t2_four_rounds_three_fix_builds(self):
        fc, p = FakeController(), new()
        self.assertEqual(p["max_rounds"], 4)
        sent = 0
        for n in range(1, 5):
            p, _ = fc.run(p, {"type": "build", "state": "review"})
            self.assertEqual(p["state"], "reviewing")
            p, actions = fc.run(p, {"type": "review", "reviewer": 0, "raw": CHANGES, "diff": DIFF})
            sent += sum(a["do"] == "message_build" for a in actions)
            if n < 4:
                self.assertEqual(p["state"], "building", n)
        self.assertEqual(p["state"], "escalated")
        self.assertEqual(fc.calls.count("message_build"), 3)
        self.assertEqual(sent, 3)

    def test_t3_gate_bounces_three_then_escalate(self):
        fc, p = FakeController(), new()
        for n in (1, 2, 3):
            p, actions = fc.run(p, {"type": "build", "state": "failed", "failure_reason": "gate_failed",
                                    "gate_text": "FAILED x", "gate_failures": n})
            self.assertEqual([a["do"] for a in actions], ["message_build"])
            self.assertEqual(p["state"], "building")
        p, actions = fc.run(p, {"type": "build", "state": "failed", "failure_reason": "gate_failed",
                                "gate_text": "FAILED x", "gate_failures": 4})
        self.assertEqual((p["state"], actions), ("escalated", []))


class Blocked(unittest.TestCase):
    def assertEscalatedForOwner(self, p):
        self.assertEqual(p["state"], "escalated")
        self.assertTrue(p["history"][-1]["note"].startswith("owner approval needed"))

    def test_t8a_auto_approve_path_waits_then_continues(self):
        fc, p = FakeController(), new()
        p, actions = fc.run(p, {"type": "build", "state": "blocked", "auto_approve": "pending"})
        self.assertEqual((p["state"], actions), ("building", []))
        self.assertEqual(p["budget"]["waiting_approval"], "t-build")
        self.assertIn("waiting for controller auto-approve", p["history"][-1]["note"])
        n = len(p["history"])
        p, _ = fc.run(p, {"type": "build", "state": "blocked", "auto_approve": "pending"})  # second poll: no spam
        self.assertEqual(len(p["history"]), n)
        p, _ = fc.run(p, {"type": "build", "state": "working"})  # controller auto-approved: task left blocked
        self.assertNotIn("waiting_approval", p["budget"])
        self.assertEqual(p["state"], "building")
        p, actions = fc.run(p, {"type": "build", "state": "review"})
        self.assertEqual((p["state"], [a["do"] for a in actions]), ("reviewing", ["spawn_review"]))
        self.assertEqual(fc.calls, ["spawn_review"])

    def test_t8b_stays_blocked_escalates(self):
        fc, p = FakeController(), new()
        p, actions = fc.run(p, {"type": "build", "state": "blocked", "auto_approve": "done"})
        self.assertEscalatedForOwner(p)
        self.assertEqual(actions, [])

    def test_t8b_wait_then_held_escalates(self):
        fc, p = FakeController(), new()
        p, _ = fc.run(p, {"type": "build", "state": "blocked", "auto_approve": "pending"})
        p, _ = fc.run(p, {"type": "build", "state": "blocked", "auto_approve": "done"})
        self.assertEscalatedForOwner(p)

    def test_approval_wait_capped(self):
        fc, p = FakeController(), new()
        p, _ = fc.run(p, {"type": "build", "state": "blocked", "auto_approve": "pending"})
        p["budget"]["waiting_since"] -= PL.APPROVAL_WAIT_MAX + 1
        p, _ = fc.run(p, {"type": "build", "state": "blocked", "auto_approve": "pending"})
        self.assertEscalatedForOwner(p)
        self.assertNotIn("waiting_approval", p["budget"])

    def test_t8c_awaiting_approval_escalates_at_once(self):
        fc, p = FakeController(), new()
        p, actions = fc.run(p, {"type": "build", "state": "awaiting_approval"})
        self.assertEscalatedForOwner(p)
        self.assertEqual(actions, [])

    def test_t8d_no_approve_or_unblock_action_ever(self):
        fc = FakeController()
        for event in ({"state": "blocked", "auto_approve": "pending"}, {"state": "blocked", "auto_approve": "done"},
                      {"state": "awaiting_approval"}):
            p = new()
            p, _ = fc.run(p, {"type": "build", **event})
        allowed = {"spawn_build", "message_build", "spawn_review"}
        self.assertTrue(set(fc.calls) <= allowed, fc.calls)
        src = (ROOT / "master" / "controller" / "pipeline.py").read_text()
        for word in ("/approve", "unblock", "tasks.approve", "resume("):
            self.assertNotIn(word, src)

    def test_previous_build_states_unchanged(self):
        fc, p = FakeController(), new()
        p, _ = fc.run(p, {"type": "build", "state": "failed"})
        self.assertEqual(p["state"], "failed")


if __name__ == "__main__":
    unittest.main()
