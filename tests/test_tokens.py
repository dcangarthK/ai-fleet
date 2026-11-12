import asyncio
import importlib.util
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path


MODULE = Path(__file__).parents[1] / "master" / "controller" / "tokens.py"
if "fastapi" not in sys.modules:
    fastapi = types.ModuleType("fastapi")
    class APIRouter:
        def get(self, _path):
            return lambda function: function
    class HTTPException(Exception):
        def __init__(self, status_code, detail):
            self.status_code, self.detail = status_code, detail
    fastapi.APIRouter = APIRouter
    fastapi.HTTPException = HTTPException
    fastapi.Query = lambda default=None: default
    sys.modules["fastapi"] = fastapi
SPEC = importlib.util.spec_from_file_location("token_telemetry", MODULE)
tokens = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(tokens)


def assistant(input_tokens, cache_read, cache_write, output, content=None):
    return {"type": "assistant", "message": {"usage": {
        "input_tokens": input_tokens, "cache_read_input_tokens": cache_read,
        "cache_creation_input_tokens": cache_write, "output_tokens": output},
        "content": content or []}}


class TokenTelemetryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.jobs = Path(self.temp.name)
        self.previous = os.environ.get("FLEET_JOBS_DIR")
        os.environ["FLEET_JOBS_DIR"] = str(self.jobs)
        tokens._cache.clear()

    def tearDown(self):
        if self.previous is None:
            os.environ.pop("FLEET_JOBS_DIR", None)
        else:
            os.environ["FLEET_JOBS_DIR"] = self.previous
        self.temp.cleanup()

    def write(self, task_id, events):
        folder = self.jobs / task_id
        folder.mkdir()
        path = folder / "events.jsonl"
        path.write_text("\n".join(event if isinstance(event, str) else json.dumps(event) for event in events))
        return path

    def test_parses_peak_turn_one_tools_and_init(self):
        path = self.write("build-one", [
            {"type": "system", "subtype": "init", "tools": [1, 2], "mcp_servers": [1], "skills": [], "plugins": [1]},
            assistant(100, 200, 300, 40, [{"type": "tool_use", "name": "Read"}]),
            assistant(900, 1, 2, 50, [{"type": "tool_use", "name": "Read"}]),
        ])
        result = tokens.parse_events(path)
        self.assertEqual((result["turns"], result["turn1_context"], result["peak_context"]), (2, 600, 903))
        self.assertEqual((result["cache_read"], result["cache_write"], result["output"]), (201, 302, 90))
        self.assertEqual(result["tool_counts"], {"Read": 2})
        self.assertEqual(result["init"], {"tools": 2, "mcp": 1, "skills": 0, "plugins": 1})

    def test_skips_malformed_and_non_object_lines(self):
        path = self.write("test-bad", ["not json", "[]", assistant(3, 0, 0, 1)])
        self.assertEqual(tokens.parse_events(path)["turns"], 1)

    def test_eighty_percent_alert(self):
        self.write("build-cap", [assistant(120_000, 0, 0, 1)])
        row = asyncio.run(tokens.task_tokens("build-cap"))
        self.assertIn("context_80_percent", row["alerts"])

    def test_turn_one_alert(self):
        self.write("test-lean", [assistant(8_001, 0, 0, 1)])
        row = asyncio.run(tokens.task_tokens("test-lean"))
        self.assertIn("turn1_context", row["alerts"])

    def test_since_filter_and_unknown_task(self):
        old = self.write("test-old", [assistant(1, 0, 0, 1)])
        os.utime(old, (1, 1))
        self.write("test-new", [assistant(1, 0, 0, 1)])
        rows = asyncio.run(tokens.tokens_since(since=2))["tasks"]
        self.assertEqual([row["task_id"] for row in rows], ["test-new"])
        with self.assertRaises(tokens.HTTPException) as raised:
            asyncio.run(tokens.task_tokens("missing"))
        self.assertEqual(raised.exception.status_code, 404)


if __name__ == "__main__":
    unittest.main()
