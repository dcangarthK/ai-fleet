"""Pure tests for evals/harness_benchmark.py; no controller or runner is contacted."""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "evals"))
import harness_benchmark as H  # noqa: E402


CASE = {"id": "case-a", "title": "Case A"}


class RecordingTests(unittest.TestCase):
    def test_record_uses_passed_gate_and_event_cache_tokens(self):
        task = {"id": "t-1", "state": "done", "gate_result": {"state": "passed"}, "runs": 3,
                "usage": {"input": 10, "output": 20, "cached": 30}}
        telemetry = {"input": 100, "output": 200, "cache_read": 300, "cache_write": 400}
        got = H.record_case(CASE, task, telemetry)
        self.assertEqual(got["tokens"], {"input": 100, "output": 200, "cache": 700})
        self.assertEqual((got["gate_passed"], got["rounds"], got["fix_rounds"]), (True, 3, 2))

    def test_record_falls_back_to_task_usage(self):
        task = {"id": "t-2", "state": "failed", "gate_result": {"state": "failed"}, "runs": 1,
                "usage": {"input": 10, "output": 20, "cached": 30}}
        got = H.record_case(CASE, task)
        self.assertEqual(got["tokens"], {"input": 10, "output": 20, "cache": 30})
        self.assertFalse(got["gate_passed"])

    def test_record_sums_each_resumed_round(self):
        task = {"id": "t-3", "state": "done", "gate_result": {"state": "passed"}, "runs": 2}
        got = H.record_case(CASE, task, round_tokens=[{"input": 10, "output": 20, "cache": 30},
                                                       {"input": 40, "output": 50, "cache": 60}])
        self.assertEqual(got["tokens"], {"input": 50, "output": 70, "cache": 90})


class CompareTests(unittest.TestCase):
    def test_compare_prints_old_new_and_deltas(self):
        old = {"variant": "before", "summary": {"gate_pass_rate": 0.5, "fix_rounds": 3,
               "tokens": {"input": 100, "output": 20, "cache": 5}}}
        new = {"variant": "after", "summary": {"gate_pass_rate": 1.0, "fix_rounds": 1,
               "tokens": {"input": 80, "output": 30, "cache": 7}}}
        text = H.compare(old, new)
        self.assertIn("old: before  new: after", text)
        self.assertIn("+50.0%", text)
        self.assertIn("-2", text)
        self.assertIn("-20", text)


class RunTests(unittest.TestCase):
    def test_run_sends_variant_ref_and_effort_and_records_them(self):
        args = SimpleNamespace(cases="unused", agent="claude", variant="changed-instructions",
                               variant_ref="feature/instructions", repo="/repo", gate=None, effort="high",
                               model=None, node=None, controller="http://controller", token="token",
                               timeout=1, max_rounds=1)
        case = CASE | {"prompt": "Do the task.", "gate": "fleet-gate"}
        task = {"id": "t-1", "state": "done", "gate_result": {"state": "passed"}, "runs": 1,
                "usage": {"input": 10, "output": 20, "cached": 30}}
        with mock.patch.object(H, "load_cases", return_value=[case]), \
             mock.patch.object(H, "request", side_effect=[{"id": "t-1"}, None]) as requests, \
             mock.patch.object(H, "wait_for_task", return_value=task):
            result = H.run(args)
        posted = requests.call_args_list[0].args[4]
        self.assertEqual((posted["kind"], posted["effort"]), ("benchmark", "high"))
        self.assertIn("git checkout --detach feature/instructions", posted["prompt"])
        self.assertEqual((result["variant_ref"], result["agent"], result["effort"]),
                         ("feature/instructions", "claude", "high"))

    def test_main_refuses_auto_and_codex(self):
        for agent in ("auto", "codex"):
            with self.subTest(agent=agent), self.assertRaises(SystemExit) as stopped:
                H.main(["label", "--variant-ref", "main", "--repo", "/repo", "--agent", agent])
            self.assertEqual(stopped.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
