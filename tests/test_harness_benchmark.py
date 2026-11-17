"""Pure tests for evals/harness_benchmark.py; no controller or runner is contacted."""
import sys
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
