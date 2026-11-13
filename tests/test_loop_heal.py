"""Part G batch 2b (docs/SPEC-capacity-placement.md 3.5, 3.6, 3.7, 3a): loop detection, gate bounce, service self-heal.
Run: uv run --with fastapi --with uvicorn --with httpx --with 'mcp<2' python -m unittest -v tests/test_loop_heal.py
"""
import json
import time
import unittest

from tests.test_budget import events_of
from tests.test_runner_budget import Base, runner

import loopheal
import pipeline as PL
import tasks
from store import db, init_db

NOW = 1_800_000_000.0


def use(i, name="Bash", cmd="rm x", text=None):
    content = [{"type": "tool_use", "id": i, "name": name, "input": {"command": cmd}}]
    if text:
        content.insert(0, {"type": "text", "text": text})
    return {"type": "assistant", "message": {"id": "m" + i, "content": content}}


def result(i, text="ok", error=False):
    return {"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": i, "content": text, "is_error": error}]}}


class DetectorTests(unittest.TestCase):
    def feed(self, det, events):
        for e in events:
            if e["type"] == "assistant":
                det.assistant(e["message"])
            else:
                for b in e["message"]["content"]:
                    det.tool_result(b)
        return det.found

    def test_same_refused_tool_twice_is_a_loop(self):
        det = runner.LoopDetector()
        self.assertIsNone(self.feed(det, [use("a"), result("a", "permission denied", True)]))
        found = self.feed(det, [use("b"), result("b", "Permission to use Bash was denied", True)])
        self.assertEqual(found["reason"], "loop")
        self.assertEqual(found["kind"], "refused_tool")

    def test_different_refused_calls_are_not_a_loop(self):
        det = runner.LoopDetector()
        found = self.feed(det, [use("a", cmd="x"), result("a", "denied", True),
                                use("b", cmd="y"), result("b", "denied", True)])
        self.assertIsNone(found)

    def test_n_identical_steps_is_a_loop_default_three(self):
        det = runner.LoopDetector()
        events = []
        for i in "abc":
            events += [use(i, cmd="ls"), result(i, "same output")]
        self.assertEqual(self.feed(det, events)["kind"], "no_progress")

    def test_narrated_tool_loop_is_caught(self):
        det = runner.LoopDetector()
        events = []
        for n, i in enumerate("abc"):
            events += [use(i, cmd="ls", text="Let me look again"),
                       result(i, "same output")]
        self.assertEqual(self.feed(det, events)["kind"], "no_progress")

    def test_narration_that_changes_is_not_a_loop(self):
        det = runner.LoopDetector()
        events = []
        for n, i in enumerate("abc"):
            events += [use(i, cmd="ls", text="step %d" % n), result(i, "same output")]
        self.assertIsNone(self.feed(det, events))

    def test_changing_output_is_not_a_loop(self):
        det = runner.LoopDetector()
        events = []
        for n, i in enumerate("abcd"):
            events += [use(i, cmd="ls"), result(i, "output %d" % n)]
        self.assertIsNone(self.feed(det, events))

    def test_watch_stream_returns_a_loop_cap(self):
        class W(Base):
            def runTest(self):
                pass
        base = W()
        base.setUp()
        try:
            job = base.job()
            path = base.fleet / "jobs" / job["id"] / "events.jsonl"
            with open(path, "w") as handle:
                for e in [use("a"), result("a", "permission denied", True), use("b"),
                          result("b", "permission denied", True)]:
                    handle.write(json.dumps(e) + "\n")
            st = {"pos": 0, "buf": b"", "ids": set(), "cap": None, "dirty": False, "pending": set(),
                  "stamp_from": 0, "caps": {}}
            cap = base.m._watch_stream(job, st)
            self.assertEqual(cap["reason"], "loop")
        finally:
            base.tearDown()


class MarkCappedTests(Base):
    def test_loop_cut_keeps_a_long_verdict_untrimmed(self):
        verdict = json.dumps({"verdict": "approve", "issues": [], "notes": " ".join(["word"] * 400)})
        events = [{"type": "assistant", "message": {"id": "m1", "content": [{"type": "text", "text": verdict}]}},
                  {"type": "assistant", "message": {"id": "m2", "content": [{"type": "text", "text": "stuck again"}]}}]
        job = self.job()
        self.write_events(job, events)
        self.m._mark_capped(job, {"reason": "loop", "kind": "no_progress", "detail": "x"})
        self.assertEqual(job["result"], verdict)  # the verdict, not the later chatter, and not 300 words
        self.assertTrue(loopheal.has_verdict(job["result"]))

    def test_a_turn_cap_still_trims_to_300_words(self):
        long = " ".join(["w"] * 500)
        job = self.job()
        self.write_events(job, [{"type": "assistant", "message": {"id": "m1", "content": [
            {"type": "text", "text": long}]}}])
        self.m._mark_capped(job, {"reason": "turns"})
        self.assertEqual(len(job["result"].split()), 300)


class DbCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()

    def setUp(self):
        with db() as c:
            c.execute("DELETE FROM service_heal") if self._has_table(c) else None

    @staticmethod
    def _has_table(c):
        return c.execute("SELECT 1 FROM sqlite_master WHERE name='service_heal'").fetchone() is not None


class LoopOutcomeTests(unittest.TestCase):
    JOB = {"state": "capped", "cap": {"reason": "loop", "kind": "refused_tool", "detail": "Bash refused twice"},
           "result": "last words"}

    def test_a_loop_cut_fails_with_the_result_kept(self):
        self.assertTrue(loopheal.is_loop_cut(self.JOB))
        self.assertEqual(tasks.outcome({"mode": "auto-edit", "gate": None, "kind": "build"}, self.JOB), "failed")
        self.assertEqual(loopheal.loop_fields("failed", self.JOB)["failure_reason"], "loop")

    def test_a_review_with_a_verdict_settles_as_done(self):
        job = {**self.JOB, "result": json.dumps({"verdict": "approve", "issues": []})}
        task = {"mode": "read-only", "gate": None, "kind": "review"}
        self.assertEqual(tasks.outcome(task, job), "review")
        self.assertNotIn("failure_reason", loopheal.loop_fields("review", job))

    def test_a_review_without_a_verdict_fails(self):
        self.assertEqual(tasks.outcome({"mode": "read-only", "gate": None, "kind": "review"}, self.JOB), "failed")

    def test_a_plain_cap_is_still_capped(self):
        job = {"state": "capped", "cap": {"reason": "turns"}}
        self.assertEqual(tasks.outcome({"mode": "auto-edit", "gate": None}, job), "capped")


PYTEST_OUT = """FAILED tests/test_a.py::test_one - AssertionError: 1 != 2
FAILED tests/test_b.py::test_two - KeyError: 'x'
== 2 failed, 40 passed =="""
UNITTEST_OUT = """FAIL: test_embed (tests.test_onboard.Embed)
Traceback (most recent call last):
  File "x.py", line 3, in test_embed
AssertionError: runner.py embed is stale
"""


class GateBounceTests(unittest.TestCase):
    def pipeline(self):
        p, _ = PL.start(PL.new({"title": "t", "spec": "s", "repo": "/r", "gate": "g", "mode": "auto-edit",
                                "builder": {"agent": "claude"}, "reviewers": [{"kind": "role", "role": "reviewer"}]}))
        return p

    def event(self, count, out=PYTEST_OUT):
        return {"type": "build", "state": "failed", "failure_reason": "gate_failed", "gate_failures": count,
                "gate_text": loopheal.gate_text({"output_tail": out}, count)}

    def test_summary_is_names_and_first_errors_not_the_tail(self):
        text = loopheal.gate_text({"output_tail": PYTEST_OUT})
        self.assertIn("tests/test_a.py::test_one: AssertionError: 1 != 2", text)
        self.assertIn("tests/test_b.py::test_two: KeyError: 'x'", text)
        self.assertNotIn("40 passed", text)

    def test_unittest_shape_is_read_too(self):
        self.assertEqual(loopheal.summarize_gate_failure(UNITTEST_OUT),
                         [("tests.test_onboard.Embed.test_embed", "AssertionError: runner.py embed is stale")])

    def test_first_and_second_failures_resume_the_builder_with_the_summary(self):
        for count in (1, 2, 3):
            p, actions = PL.step(self.pipeline(), self.event(count))
            self.assertEqual(p["state"], "building")
            self.assertEqual(actions[0]["do"], "message_build")
            self.assertIn("test_one", actions[0]["text"])

    def test_the_fourth_failure_escalates(self):
        p, actions = PL.step(self.pipeline(), self.event(4))
        self.assertEqual(p["state"], "escalated")
        self.assertEqual(actions, [])

    def test_trap_signature_adds_the_checklist_line(self):
        text = loopheal.gate_text({"output_tail": UNITTEST_OUT})
        self.assertIn("python3 nodes/build.py", text)
        self.assertNotIn("python3 nodes/build.py", loopheal.gate_text({"output_tail": PYTEST_OUT}))

    def test_old_onboarding_kept_after_a_merge_signature(self):
        out = "FAIL: test_x (tests.test_onboard.Env)\nTraceback\nAssertionError: '--enroll-file' not found in 'usage'\n"
        text = loopheal.gate_text({"output_tail": out})
        self.assertIn("git checkout main -- nodes/onboard.sh", text)
        self.assertNotIn("python3 nodes/build.py` to re-embed", text)
        # conflict markers alone no longer trigger it
        self.assertNotIn("git checkout main", loopheal.gate_text({"output_tail": "<<<<<<< merge\ntest_onboard"}))

    def test_traps_match_raw_output_when_no_test_names_parse(self):
        out = "tests/test_onboard.py: runner embed out of date\n"
        self.assertEqual(loopheal.summarize_gate_failure(out), [])
        self.assertIn("python3 nodes/build.py", loopheal.gate_text({"output_tail": out}))
        out = "test_onboard: '--enroll-file' not found in 'usage'\n"
        self.assertIn("git checkout main -- nodes/onboard.sh", loopheal.gate_text({"output_tail": out}))

    def test_a_stale_embed_not_found_error_gets_only_the_build_trap(self):
        out = "FAIL: test_embed (tests.test_onboard.Embed)\nTraceback\nAssertionError: 'def main' not found in runner embed\n"
        text = loopheal.gate_text({"output_tail": out})
        self.assertIn("python3 nodes/build.py", text)
        self.assertNotIn("git checkout main", text)

    def test_a_non_gate_failure_still_fails_the_pipeline(self):
        p, actions = PL.step(self.pipeline(), {"type": "build", "state": "failed", "failure_reason": "error"})
        self.assertEqual(p["state"], "failed")


class SelfHealTests(DbCase):
    def setUp(self):
        super().setUp()
        loopheal._ensure()
        with db() as c:
            c.execute("DELETE FROM service_heal")
        self.calls = []

    def restart(self, node, name):
        self.calls.append((node, name))

    def svc(self, **kw):
        return [{"node": "n1", "service": "runner", "missed_beats": 3, "unhealthy_since": None} | kw]

    def test_healthy_service_is_left_alone(self):
        out = loopheal.heal_tick(self.svc(missed_beats=2), self.restart, lambda *a: True, NOW, grace=False)
        self.assertEqual((out, self.calls), ([], []))

    def test_restarts_and_verifies_once(self):
        out = loopheal.heal_tick(self.svc(), self.restart, lambda *a: True, NOW, grace=False)
        self.assertEqual(out[0]["action"], "restarted")
        self.assertEqual(self.calls, [("n1", "runner")])
        self.assertTrue(events_of("service.restarted"))

    def test_two_minutes_of_failed_health_checks_restarts(self):
        out = loopheal.heal_tick(self.svc(missed_beats=0, unhealthy_since=NOW - 130),
                                 self.restart, lambda *a: True, NOW, grace=False)
        self.assertEqual(out[0]["action"], "restarted")

    def test_startup_grace_waits(self):
        out = loopheal.heal_tick(self.svc(), self.restart, lambda *a: True, NOW, grace=True)
        self.assertEqual((out[0]["action"], self.calls), ("grace", []))

    def test_second_failure_in_15_minutes_alerts_and_stops(self):
        before = len(events_of("service.alert"))
        for step in (0, 60):
            out = loopheal.heal_tick(self.svc(), self.restart, lambda *a: False, NOW + step, grace=False)
            self.assertEqual(out[0]["action"], "restart_failed")
        out = loopheal.heal_tick(self.svc(), self.restart, lambda *a: False, NOW + 120, grace=False)
        self.assertEqual(out[0]["action"], "alert")
        self.assertEqual(len(self.calls), 2)  # no third restart
        self.assertEqual(len(events_of("service.alert")), before + 1)
        loopheal.heal_tick(self.svc(), self.restart, lambda *a: False, NOW + 180, grace=False)
        self.assertEqual(len(events_of("service.alert")), before + 1)  # alert raised once

    def test_counters_survive_a_restart_and_the_window_expires(self):
        loopheal.heal_tick(self.svc(), self.restart, lambda *a: False, NOW, grace=False)
        loopheal.heal_tick(self.svc(), self.restart, lambda *a: False, NOW + 100, grace=False)
        out = loopheal.heal_tick(self.svc(), self.restart, lambda *a: True, NOW + 200 + loopheal.RESTART_WINDOW,
                                 grace=False)
        self.assertEqual(out[0]["action"], "restarted")

    def test_a_raising_restart_counts_as_failed(self):
        def boom(node, name):
            raise OSError("no service manager")
        out = loopheal.heal_tick(self.svc(), boom, lambda *a: True, NOW, grace=False)
        self.assertEqual(out[0]["action"], "restart_failed")


if __name__ == "__main__":
    unittest.main()
