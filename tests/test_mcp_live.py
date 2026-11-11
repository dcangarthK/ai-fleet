"""MCP bridge side of Part D (docs/SPEC-live-monitor.md 2.3, 2.4, AC 5, AC 7): session headers on every controller call
through one helper, and the new tools call the right endpoints. Run:
uv run --with fastapi --with uvicorn --with httpx --with 'mcp<2' python -m unittest -v tests/test_mcp_live.py
"""
import json
import os
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "master" / "mcp"))
sys.path.insert(0, str(ROOT / "master" / "controller"))

import server  # noqa: E402  (needs the mcp package; the gate installs it)


class Response:
    status_code, is_error, content = 200, False, b"{}"

    def json(self):
        return {"id": "t-1", "ok": True, "timed_out": False, "tasks": []}

    def raise_for_status(self):
        return None


class Bridge(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.saved = (server.httpx.request, server.httpx.get, server.httpx.post, dict(os.environ))
        server.httpx.request = lambda method, url, **kw: self.calls.append((method, url, kw)) or Response()
        server.httpx.get = lambda url, **kw: self.calls.append(("GET", url, kw)) or Response()
        server.httpx.post = lambda url, **kw: self.calls.append(("POST", url, kw)) or Response()
        os.environ.update({"FLEET_SESSION_ID": "orch-1", "FLEET_SESSION_MODEL": "claude-opus-5-5",
                           "FLEET_SESSION_ACCOUNT": "spare"})

    def tearDown(self):
        server.httpx.request, server.httpx.get, server.httpx.post, saved_env = self.saved
        os.environ.clear()
        os.environ.update(saved_env)

    def last(self):
        return self.calls[-1]

    def test_session_headers_on_every_controller_call(self):
        server.fleet_status()
        server.wait_tasks(["t-1"], timeout_seconds=1)
        server._fleet_json("/api/fleet")
        server._fallback_for("fast")
        server._report_body({"role": "fast"})
        server._spawn_escalation("fast")("claude", "t", "p")
        for method, url, kw in self.calls:
            headers = kw.get("headers") or {}
            self.assertEqual((headers.get("X-Fleet-Session"), headers.get("X-Fleet-Model"), headers.get("X-Fleet-Account")),
                             ("orch-1", "claude-opus-5-5", "spare"), url)
        wait = next(kw for m, u, kw in self.calls if "/api/tasks/wait" in u)
        self.assertNotIn("X-Fleet-Scope", wait["headers"])  # waits stay local: presence from waits counts

    def test_invalid_session_values_are_not_sent(self):
        os.environ["FLEET_SESSION_ID"] = "bad value!"
        server.fleet_status()
        self.assertNotIn("X-Fleet-Session", self.last()[2]["headers"])

    def test_one_helper_for_every_call(self):
        source = (ROOT / "master" / "mcp" / "server.py").read_text()
        calls = re.findall(r"httpx\.(?:get|post|request)\([^\n]*CONTROLLER[^\n]*", source)
        self.assertTrue(calls)
        for line in calls:
            self.assertIn("headers=_headers(", line, line)

    def test_new_tools_and_spawn_task_fields(self):
        server.report_activity("Read the spec", "subagent", "spare", "claude-opus-5-5", tokens={"input": 3})
        method, url, kw = self.last()
        self.assertEqual((method, url.rsplit("/", 2)[-1]), ("POST", "activity"))
        self.assertEqual(kw["headers"]["X-Fleet-Scope"], "operate")
        self.assertEqual(kw["json"]["tokens"], {"input": 3})
        server.report_activity("", "", "", "", status="done", activity_id="a-1")
        self.assertEqual(self.last()[2]["json"], {"id": "a-1", "status": "done"})
        server.set_chain("t-12345678", [{"stage": "build", "agent": "claude"}])
        method, url, kw = self.last()
        self.assertEqual((method, url.split("/api/")[1]), ("PUT", "work/t-12345678/chain"))
        server.spawn_task("claude", "test it", kind="test", parent_id="t-1", link="check", profile="spare",
                          stage="test-1", independence="waived", independence_reason="owner chose Opus as tester")
        body = self.last()[2]["json"]
        self.assertEqual({k: body[k] for k in ("kind", "link", "profile", "stage", "independence", "independence_reason")},
                         {"kind": "test", "link": "check", "profile": "spare", "stage": "test-1",
                          "independence": "waived", "independence_reason": "owner chose Opus as tester"})
        json.dumps(body)


if __name__ == "__main__":
    unittest.main()
