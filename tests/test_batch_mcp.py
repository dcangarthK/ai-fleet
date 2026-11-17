"""MCP batch tools (docs/SPEC-batch-pipeline.md T10, T11 wiring, T15). Run:
uv run --with fastapi --with uvicorn --with httpx --with 'mcp<2' python -m unittest -v tests/test_batch_mcp.py
"""
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "master" / "mcp"))
sys.path.insert(0, str(ROOT / "master" / "controller"))

import server  # noqa: E402


class Response:
    status_code, is_error, content = 200, False, b"{}"

    def __init__(self, data=None):
        self.data = data if data is not None else {"id": "b-1", "state": "running", "items": []}

    def json(self):
        return self.data


class Tools(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.saved = (server.httpx.request, server.httpx.get)
        server.httpx.request = lambda method, url, **kw: self.calls.append((method, url, kw)) or Response()
        server.httpx.get = lambda url, **kw: self.calls.append(("GET", url, kw)) or Response(
            {"timed_out": True, "state": "running", "items": []})

    def tearDown(self):
        server.httpx.request, server.httpx.get = self.saved

    def test_run_batch_posts_once(self):
        server.run_batch([{"id": "a"}], {"max_fix_rounds": 2})
        self.assertEqual(len(self.calls), 1)
        method, url, kw = self.calls[0]
        self.assertEqual((method, url.endswith("/api/batches")), ("POST", True))
        self.assertEqual(kw["json"]["policy"], {"max_fix_rounds": 2})

    def test_t10_wait_batch_is_one_request(self):
        out = json.loads(server.wait_batch("b-1", timeout_seconds=5))
        self.assertEqual(len(self.calls), 1)
        self.assertIn("/api/batches/b-1/wait?timeout=5", self.calls[0][1])
        self.assertTrue(out["timed_out"])
        self.assertEqual(out["state"], "running")

    def test_batch_status_full_flag(self):
        server.batch_status("b-1")
        server.batch_status("b-1", full=True)
        self.assertTrue(self.calls[0][1].endswith("full=false"))
        self.assertTrue(self.calls[1][1].endswith("full=true"))

    def test_t15_ten_item_batch_takes_at_most_five_tool_calls(self):
        items = [{"id": f"i{n}"} for n in range(10)]
        server.run_batch(items)
        server.wait_batch("b-1")
        server.wait_batch("b-1")  # a timeout round
        server.batch_status("b-1")
        self.assertLessEqual(len(self.calls), 5)


if __name__ == "__main__":
    unittest.main()
