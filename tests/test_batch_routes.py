"""Batch routes against the real controller app, no runner (docs/SPEC-batch-pipeline.md AC1, AC5, AC13, T4, T8 poll
side). Run: uv run --with fastapi --with uvicorn --with httpx python -m unittest -v tests/test_batch_routes.py
"""
import asyncio
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="fleet-batch-"))
(TMP / "no-plugins").mkdir()
(TMP / "skills").mkdir()
os.environ.update({"FLEET_PLUGINS_DIR": str(TMP / "no-plugins"), "FLEET_DB": str(TMP / "fleet.db"),
                   "FLEET_POLL_SECONDS": "3600", "FLEET_TASK_POLL_SECONDS": "0",
                   "FLEET_PUBLIC_HOST": "master.tail.ts.net", "FLEET_PUBLIC_PORT": "8701", "LITELLM_MASTER_KEY": "",
                   "FLEET_SKILLS_DIR": str(TMP / "skills"), "FLEET_INSTRUCTIONS_DIR": str(TMP / "no-instructions"),
                   "FLEET_EVAL_RESULTS": str(TMP / "results.json")})
ROOT = Path(__file__).resolve().parent.parent
W = {"X-Fleet": "1"}
sys.path.insert(0, str(ROOT / "master" / "controller"))
from fastapi.testclient import TestClient  # noqa: E402
import controller  # noqa: E402
import tasks  # noqa: E402


def item(i, **kw):
    return {"id": i, "title": f"item {i}", "spec": "do it", "repo": "/repo", "gate": "pytest"} | kw


class Routes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        controller._tick_lock = asyncio.Lock()
        controller._pipeline_locks.clear()
        controller._batch_locks.clear()
        tasks._locks.clear()
        cls.ctx = TestClient(controller.app, base_url="http://127.0.0.1")
        cls.c = cls.ctx.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.ctx.__exit__(None, None, None)

    def count(self):
        with controller.db() as c:
            return c.execute("SELECT COUNT(*) FROM batches").fetchone()[0]

    def test_invalid_submit_is_400_and_creates_nothing(self):
        before = self.count()
        for body in ({"items": [item("a"), item("a")]}, {"items": [item("a", repo="rel")]},
                     {"items": [item("a", depends_on=["b"]), item("b", depends_on=["a"])]},
                     {"items": [item("a", builder="gemini")]}, {"items": []}):
            self.assertEqual(self.c.post("/api/batches", json=body, headers=W).status_code, 400, body)
        self.assertEqual(self.count(), before)

    def test_one_call_creates_batch_and_every_item(self):
        r = self.c.post("/api/batches", json={"items": [item("a"), item("b", builder="codex")]}, headers=W)
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()
        self.assertEqual([i["id"] for i in data["items"]], ["a", "b"])
        self.assertEqual(data["state"], "running")
        stored = self.c.get(f"/api/batches/{data['id']}", headers=W).json()
        self.assertEqual(len(stored["items"]), 2)
        self.assertNotIn("gate_tail", json.dumps(stored))
        self.assertIn("gate_tail", json.dumps(self.c.get(f"/api/batches/{data['id']}?full=true", headers=W).json()["items"][0]
                                              | {"gate_tail": 1}))

    def test_wait_returns_at_timeout_with_state(self):
        r = self.c.post("/api/batches", json={"items": [item("a", depends_on=[]), item("b", depends_on=["a"])],
                                              "policy": {"on_limit": "wait"}}, headers=W)
        bid = r.json()["id"]
        start = time.monotonic()
        out = self.c.get(f"/api/batches/{bid}/wait?timeout=1", headers=W).json()
        self.assertLess(time.monotonic() - start, 10)
        self.assertIn("state", out)
        self.assertIn("timed_out", out)

    def test_unknown_batch_404(self):
        self.assertEqual(self.c.get("/api/batches/b-nope", headers=W).status_code, 404)


class FixRoundController(unittest.TestCase):
    """Controller-side fix round: idempotent start by tag (AC12) and the final-review spawn."""

    def _batch(self):
        b = controller.batch_machine.new_batch({"items": [item("a")]})
        b["id"] = "b-fix"
        it = b["items"][0]
        it["build"] = {"agent": "codex", "model": "gpt-5.6-terra", "effort": "medium"}
        it["review"] = {"agent": "claude", "model": "claude-fable-5-2", "effort": "medium"}
        return b, it

    def test_start_item_adopts_tagged_pipeline(self):
        from unittest import mock
        b, it = self._batch()
        with mock.patch.object(controller, "_batch_pipeline_by_tag", return_value="p-old"), \
                mock.patch.object(controller, "create_pipeline", side_effect=AssertionError("duplicate")):
            asyncio.run(controller._batch_start_item(b, it))
        self.assertEqual(it["pipeline_id"], "p-old")

    def test_spawn_final_is_read_only_worktree_and_adopts_existing(self):
        from unittest import mock
        b, it = self._batch()
        it.update(state="ready", pipeline_id="p1", branch="fleet/t-1")
        b["final"] = {"state": "spawned", "ids": ["a"], "agent": "codex", "model": "gpt-5.6-terra", "effort": "high"}
        action = dict(b["final"], do="spawn_final_review")
        made = {}

        async def fake_create(t):
            made["t"] = t
            return {"id": "t-final"}

        async def fake_diff(_id):
            return {"diff": "d" * 100_000}

        with mock.patch.object(controller.tasks, "create_task", fake_create), \
                mock.patch.object(controller.tasks, "task_diff", fake_diff), \
                mock.patch.object(controller, "get_pipeline", return_value={"build_task": "t-b"}), \
                mock.patch.object(controller, "_find_final_task", return_value=None):
            asyncio.run(controller._batch_spawn_final(b, action))
        t = made["t"]
        self.assertEqual((t.mode, t.worktree, t.kind, t.effort), ("read-only", True, "final_review", "high"))
        self.assertLess(len(t.prompt), controller.FINAL_REVIEW_MAX_CHARS + 2000)
        self.assertIn("git diff main...fleet/t-1", t.prompt)
        self.assertEqual(b["final"]["task_id"], "t-final")
        b["final"].pop("task_id")
        with mock.patch.object(controller.tasks, "create_task", side_effect=AssertionError("duplicate")), \
                mock.patch.object(controller, "_find_final_task", return_value="t-final"):
            asyncio.run(controller._batch_spawn_final(b, action))
        self.assertEqual(b["final"]["task_id"], "t-final")


class AutoApproveStatus(unittest.TestCase):
    def task(self, **kw):
        base = {"id": "t-x", "agent": "claude", "mode": "auto-edit", "auto_approvals": 0,
                "policy": {"auto_approve": {"denied_tools": True, "agents": ["claude"]}}, "denials": [{"tool_name": "Bash"}]}
        return base | kw

    def test_no_policy_is_done(self):
        self.assertEqual(controller._auto_approve_status(self.task(policy=None)), "done")
        self.assertEqual(controller._auto_approve_status(self.task(mode="supervised")), "done")

    def test_exhausted_is_done(self):
        self.assertEqual(controller._auto_approve_status(self.task(auto_approvals=tasks.AUTO_APPROVE_ROUNDS)), "done")


if __name__ == "__main__":
    unittest.main()
