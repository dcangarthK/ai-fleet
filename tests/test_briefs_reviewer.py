"""P2 typed briefs and controller-selected reviewer regressions."""
import json
import time
import unittest
from unittest.mock import patch

from tests.test_controller import ControllerTests, W
from store import db
import briefs
import reviewer


class BriefUnitTests(unittest.TestCase):
    def test_build_brief_requires_goal_files_and_acceptance(self):
        for brief, text in [({}, "goal"), ({"goal": "x", "files_allowed": [] , "acceptance": ["x"]}, "files_allowed"),
                            ({"goal": "x", "files_allowed": ["x.py"], "acceptance": []}, "acceptance")]:
            with self.assertRaisesRegex(ValueError, text):
                briefs.validate(brief, "build")

    def test_build_and_review_rendering(self):
        brief = briefs.validate({"goal": "Ship it", "files_allowed": ["a.py"], "acceptance": ["tests pass"],
                                 "context_refs": ["a.py:12"], "done_when": "green", "gate": "pytest"}, "build")
        self.assertIn("# Build or fix", briefs.render(brief, "build"))
        self.assertIn("a.py:12", briefs.render(brief, "build"))
        self.assertIn("# Review", briefs.render(brief, "review"))


class ControllerCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ControllerTests.setUpClass()
        cls.c = ControllerTests.c

    @classmethod
    def tearDownClass(cls):
        ControllerTests.tearDownClass()

    def task(self, **kw):
        body = {"agent": "claude", "prompt": "add a hello function"} | kw
        r = self.c.post("/api/tasks", json=body, headers=W)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()


class BriefRouteTests(ControllerCase):
    def test_typed_brief_is_rendered_and_stored(self):
        body = {"agent": "claude", "kind": "build", "mode": "auto-edit", "prompt": "",
                "brief": {"goal": "Add endpoint", "files_allowed": ["master/controller/tasks.py"],
                          "acceptance": ["route responds"], "context_refs": ["master/controller/tasks.py:1472"],
                          "done_when": "test passes", "gate": "pytest"}}
        r = self.c.post("/api/tasks", json=body, headers=W)
        self.assertEqual(r.status_code, 200, r.text)
        task = self.c.get(f"/api/tasks/{r.json()['id']}", headers=W).json()
        self.assertIn("# Build or fix", task["prompt"])
        self.assertEqual(task["placement"]["brief"]["files_allowed"], ["master/controller/tasks.py"])

    def test_legacy_prompt_path_still_works(self):
        task = self.task(kind="draft", prompt="legacy prompt")
        self.assertEqual(task["prompt"], "legacy prompt")

    def test_route_rejects_invalid_brief(self):
        r = self.c.post("/api/tasks", json={"agent": "claude", "kind": "build", "prompt": "",
                                             "brief": {"goal": "x", "files_allowed": [], "acceptance": ["ok"]}}, headers=W)
        self.assertEqual(r.status_code, 400)
        self.assertIn("files_allowed", r.text)


class ReviewerTests(ControllerCase):
    def _insert(self, task_id, kind, agent, model, parent_id=None):
        with db() as c:
            c.execute("INSERT INTO tasks (id,kind,agent,model,parent_id,state,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
                      (task_id, kind, agent, model, parent_id, "done", time.time(), time.time()))

    def _drop(self, *ids):
        with db() as c:
            c.executemany("DELETE FROM tasks WHERE id=?", [(task_id,) for task_id in ids])

    def test_final_review_prefers_codex_and_excludes_builder(self):
        self._insert("t-review-choice", "final_review", "claude", "claude-sonnet-5-5")
        self.addCleanup(self._drop, "t-review-choice")
        with patch("reviewer.budget.status", return_value={"models": [{"key": "codex", "state": "ok"}]}):
            choice = reviewer.propose("t-review-choice")
        self.assertEqual((choice["agent"], choice["model"]), ("codex", "gpt-5.6-terra"))

    def test_codex_limited_falls_back_to_independent_claude(self):
        self._insert("t-review-fallback", "final_review", "claude", "claude-sonnet-5-5")
        self.addCleanup(self._drop, "t-review-fallback")
        with patch("reviewer.budget.status", return_value={"models": [{"key": "codex", "state": "limited", "limited_until": 9e9},
                                                                         {"key": "claude:claude-opus-*", "state": "ok"}]}):
            choice = reviewer.propose("t-review-fallback")
        self.assertEqual((choice["agent"], choice["model"]), ("claude", "claude-opus-5-5"))
        self.assertNotIn("fable", choice["model"])


if __name__ == "__main__":
    unittest.main()
