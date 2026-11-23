"""Plan items 0.7 and 2.3: cost_source on every usage row, real run_prompt_bytes, an explicit gate_result, batch_report.
Run: FLEET_DB=/tmp/m.db uv run --with fastapi --with uvicorn --with httpx --with 'mcp<2' python -m unittest -v tests/test_measurement.py
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "master" / "controller"))
sys.path.insert(0, str(ROOT / "master" / "mcp"))
os.environ.setdefault("FLEET_DB", os.path.join(tempfile.mkdtemp(), "fleet.db"))

import batch_report  # noqa: E402
import tasks  # noqa: E402
from store import db, init_db  # noqa: E402


class CostSource(unittest.TestCase):
    def test_runner_words_stand(self):
        self.assertEqual(tasks.cost_source_of({"agent": "claude"}, {"cost_source": "transcript"}), "transcript")

    def test_local_agent(self):
        self.assertEqual(tasks.cost_source_of({"agent": "ollama"}, {}), "local")

    def test_cost_without_source_is_reported(self):
        self.assertEqual(tasks.cost_source_of({"agent": "codex"}, {"cost_usd": 0.5}), "reported")

    def test_tokens_only_is_estimated(self):
        self.assertEqual(tasks.cost_source_of({"agent": "codex"}, {"input": 10}), "estimated")

    def test_nothing_reported_is_none_not_null(self):
        self.assertEqual(tasks.cost_source_of({"agent": "claude"}, {}), "none")


class GateField(unittest.TestCase):
    def test_runner_gate_kept(self):
        got = json.loads(tasks.gate_field({"gate": {"state": "passed"}}, {"gate": "x"}))
        self.assertEqual(got["state"], "passed")

    def test_no_gate_named(self):
        self.assertEqual(json.loads(tasks.gate_field({}, {"gate": None}))["state"], "no_gate")

    def test_gate_named_not_run(self):
        self.assertEqual(json.loads(tasks.gate_field({}, {"gate": "make t"}))["state"], "not_run")

    def test_real_result_not_replaced(self):
        got = json.loads(tasks.gate_field({}, {"gate": "x", "gate_result": {"state": "failed"}}))
        self.assertEqual(got["state"], "failed")

    def test_job_fields_never_null_with_task(self):
        self.assertIsNotNone(tasks.job_fields({}, {"gate": None})["gate_result"])


class BatchReport(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()
        with db() as c:
            c.execute("DELETE FROM tasks WHERE id LIKE 'zbr-%'")
            c.execute("DELETE FROM agent_usage WHERE task_id LIKE 'zbr-%'")
            c.execute("INSERT INTO tasks (id, title, agent, model, state, gate, gate_result, result, outcome, branch)"
                      " VALUES ('zbr-1','a','claude','m1','done','g',?,?,?,'fleet/zbr-1')",
                      (json.dumps({"state": "passed"}), "chatter\nVERDICT: approve\n",
                       json.dumps({"kind": "merged", "commit": "abc1234"})))
            c.execute("INSERT INTO tasks (id, title, agent, state) VALUES ('zbr-2','b','ollama','review')")
            for run, outcome, cost in ((1, "limit", 0.25), (2, "ok", 0.5)):
                c.execute("INSERT INTO agent_usage (ts, task_id, run, agent, input, output, cached, cost_usd, outcome,"
                          " account, cost_source) VALUES (0,'zbr-1',?,'claude',10,5,1,?,?,'acct-a','result')",
                          (run, cost, outcome))

    def test_one_call_composes_everything(self):
        one = batch_report.report(["zbr-1", "zbr-2"])[0]
        self.assertEqual((one["model"], one["account"], one["commit"], one["gate"]), ("m1", "acct-a", "abc1234", "passed"))
        self.assertEqual(one["verdict"], "VERDICT: approve")
        self.assertEqual(one["tokens"], {"input": 20, "output": 10, "cached": 2})
        self.assertEqual(one["spend_usd"], 0.75)
        self.assertEqual((one["limit_hits"], one["runs"], one["cost_sources"]), (1, 2, ["result"]))

    def test_no_usage_and_no_gate(self):
        two = batch_report.report(["zbr-2"])[0]
        self.assertEqual((two["gate"], two["spend_usd"], two["limit_hits"]), ("no_gate", 0, 0))

    def test_unknown_id(self):
        self.assertEqual(batch_report.report(["zbr-missing"]), [{"id": "zbr-missing", "error": "not found"}])

    def test_mcp_tool_registered(self):
        import server
        self.assertTrue(callable(server.batch_report))


if __name__ == "__main__":
    unittest.main()
