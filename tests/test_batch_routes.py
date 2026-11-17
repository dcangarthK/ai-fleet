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
