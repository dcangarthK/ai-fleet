"""Part H req 11 (docs/SPEC-token-budget.md 3.11): the orchestrator reads task results trimmed. Run:
uv run --with fastapi --with uvicorn --with httpx --with 'mcp<2' python -m unittest -v tests/test_orchestrator_hygiene.py
"""
import json
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "master" / "mcp"))
sys.path.insert(0, str(ROOT / "master" / "controller"))

import server  # noqa: E402

RESULT = "\n".join(["Long chatter " * 5] * 30 + [
    "VERDICT: changes requested",
    "HIGH: unchecked input in foo()", "  file a.py:10 detail", "",
    "MED: missing test", "  more detail", "",
    "LOW: naming", "",
    "More chatter " * 20])
TAIL = "\n".join(["test_a ... ok"] * 200 + [
    "FAIL: test_bad (tests.test_x.X)", "ERROR: test_worse (tests.test_x.X)",
    "Ran 202 tests in 1.2s", "FAILED (failures=1, errors=1)"])


def task(**kw):
    return {"id": "t-1", "state": "review", "result": RESULT, "agent": "claude",
            "gate_result": {"state": "failed", "exit_code": 1, "output_tail": TAIL}, **kw}


class Trim(unittest.TestCase):
    def test_keeps_verdict_findings_gate(self):
        v = server.trim_task(task())
        self.assertEqual(v["state"], "review")
        self.assertEqual(v["verdict"], "VERDICT: changes requested")
        self.assertEqual(len(v["findings"]), 3)
        self.assertTrue(v["findings"][0].startswith("HIGH: unchecked"))
        self.assertIn("file a.py:10", v["findings"][0])
        self.assertNotIn("chatter", json.dumps(v))
        g = v["gate"]
        self.assertEqual(g["state"], "failed")
        self.assertIn("Ran 202 tests in 1.2s", g["summary"])
        self.assertIn("FAILED (failures=1, errors=1)", g["summary"])
        self.assertIn("FAIL: test_bad (tests.test_x.X)", g["failing"])
        self.assertNotIn("test_a ... ok", json.dumps(v))
        self.assertLess(len(json.dumps(v)), 1500)

    def test_no_gate_no_findings(self):
        v = server.trim_task({"id": "t-2", "state": "working", "result": "", "gate_result": None})
        self.assertEqual(v["findings"], [])
        self.assertIsNone(v["gate"])


class Response:
    status_code, is_error, content = 200, False, b"{}"

    def __init__(self, data):
        self.data = data

    def json(self):
        return self.data


class Tools(unittest.TestCase):
    def setUp(self):
        self.saved = (server.httpx.request, server.httpx.get, dict(os.environ))
        server.httpx.request = lambda m, url, **kw: Response(task())
        server.httpx.get = lambda url, **kw: Response({"timed_out": False, "tasks": [task()]})

    def tearDown(self):
        server.httpx.request, server.httpx.get, env = self.saved
        os.environ.clear()
        os.environ.update(env)

    def test_defaults_trimmed(self):
        for out in (server.task_status("t-1"), server.wait_tasks(["t-1"])):
            self.assertNotIn("chatter", out)
            self.assertNotIn("test_a ... ok", out)
            self.assertIn("verdict", out)

    def test_full_flag(self):
        self.assertIn("chatter", server.task_status("t-1", full=True))
        self.assertIn("chatter", server.wait_tasks(["t-1"], full=True))

    def test_env_off(self):
        os.environ["FLEET_MCP_TRIM"] = "0"
        self.assertIn("chatter", server.task_status("t-1"))


class Docs(unittest.TestCase):
    def test_documented(self):
        for f in ("CLAUDE.md", "docs/MASTER-INSTRUCTIONS.md"):
            t = (ROOT / f).read_text()
            self.assertIn("fresh session per batch", t, f)
            self.assertIn("full=True", t, f)
        self.assertIn("event", (ROOT / "CLAUDE.md").read_text())


if __name__ == "__main__":
    unittest.main()
