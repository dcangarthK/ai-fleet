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
        self.assertIn("chatter", server.wait_tasks(["t-1"]))


class Fallbacks(unittest.TestCase):
    def test_other_finding_formats(self):
        for line in ("- **High** \u2014 bad thing", "[HIGH] bad thing", "1. HIGH: bad thing", "### HIGH \u2014 bad thing",
                     "HIGH (a.py:10): bad thing", "| HIGH | bad thing |", "| High | bad thing |"):
            v = server.trim_task({"id": "t", "state": "review", "result": f"intro\n{line}\n\nend"})
            self.assertEqual(len(v["findings"]), 1, line)

    def test_prose_not_a_finding(self):
        v = server.trim_task({"id": "t", "state": "review", "result": "Low-hanging fruit first.\nLow: tide today."})
        self.assertEqual(v["findings"], [])

    def test_unmatched_format_keeps_tail(self):
        text = "x" * 5000 + "\nThe answer is 42."
        v = server.trim_task({"id": "t", "state": "done", "result": text})
        self.assertTrue(v["result_tail"].endswith("The answer is 42."))
        self.assertLessEqual(len(v["result_tail"]), server.RESULT_TAIL)

    def test_non_approve_verdict_without_findings_keeps_tail(self):
        v = server.trim_task({"id": "t", "state": "review", "result": "Verdict: request_changes\nsee notes below"})
        self.assertIn("see notes below", v["result_tail"])

    def test_approve_verdict_no_tail(self):
        v = server.trim_task({"id": "t", "state": "review", "result": "Verdict: approve\nfine"})
        self.assertNotIn("result_tail", v)

    def test_failed_gate_unknown_output_keeps_tail(self):
        out = "\n".join(f"line {i}" for i in range(50)) + "\nGate timed out after 900 seconds"
        v = server.trim_task({"id": "t", "state": "failed", "result": "",
                              "gate_result": {"state": "failed", "exit_code": None, "output_tail": out}})
        self.assertEqual(len(v["gate"]["tail"]), server.GATE_TAIL_LINES)
        self.assertEqual(v["gate"]["tail"][-1], "Gate timed out after 900 seconds")

    def test_passed_gate_no_tail(self):
        v = server.trim_task({"id": "t", "state": "review", "result": "",
                              "gate_result": {"state": "passed", "exit_code": 0, "output_tail": "all fine"}})
        self.assertNotIn("tail", v["gate"])

    def test_denials_and_planned_kept(self):
        v = server.trim_task({"id": "t", "state": "blocked", "result": "", "planned": {"action": "wait"},
                              "denials": [{"tool_name": "Bash"}, {"tool": "Write"}, {"tool_name": "Bash"}]})
        self.assertEqual(v["denials"], ["Bash", "Write"])
        self.assertEqual(v["planned"], {"action": "wait"})

    def test_active_states_skip_extra_get(self):
        calls = []
        saved = server._controller
        server._controller = lambda *a, **k: calls.append(a) or "{}"
        try:
            for st in ("queued", "awaiting_approval", "starting", "working"):
                server._full_record({"id": "t", "state": st})
        finally:
            server._controller = saved
        self.assertEqual(calls, [])

    def test_active_states_match_controller(self):
        import tasks
        self.assertEqual(server.ACTIVE_STATES, set(tasks.ACTIVE))


class Docs(unittest.TestCase):
    def test_documented(self):
        for f in ("CLAUDE.md", "docs/MASTER-INSTRUCTIONS.md"):
            t = (ROOT / f).read_text()
            self.assertIn("fresh session per batch", t, f)
            self.assertIn("full=True", t, f)
        self.assertIn("event", (ROOT / "CLAUDE.md").read_text())


if __name__ == "__main__":
    unittest.main()
