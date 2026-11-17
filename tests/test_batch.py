"""Batch pipeline pure module (docs/SPEC-batch-pipeline.md). Covers T1, T2 (mapping), T3-T7, T9, T11-T13.
Run: python3 -m unittest -v tests/test_batch.py
"""
import json
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "master" / "controller"))
import batch as B  # noqa: E402

NOW = 1_000_000.0
NO_LIMITS = {"models": {}, "accounts": {}}


def item(i, **kw):
    base = {"id": i, "title": f"item {i}", "spec": "do it", "repo": "/r", "gate": "pytest"}
    base.update(kw)
    return base


def make(items, **policy):
    return B.new_batch({"items": items, "policy": policy})


def tick(b, limits=None, now=NOW):
    return B.step(b, {"type": "tick", "now": now, "limits": limits or NO_LIMITS})


def pipe(state, **kw):
    return {"state": state, "round": kw.pop("round", 1), "history": kw.pop("history", []),
            "budget": kw.pop("budget", {}), "build_task": "t-1", **kw}


def settle_ready(b, ids):
    for i in ids:
        b, _ = B.step(b, {"type": "pipeline", "id": i, "pipeline": pipe("ready")})
    return b


class Validation(unittest.TestCase):
    def test_defaults(self):
        b = make([item("a")])
        self.assertEqual(b["policy"]["max_fix_rounds"], 3)
        self.assertEqual(b["policy"]["gate_bounces"], 3)
        self.assertEqual(b["policy"]["final_review"]["model"], "gpt-5.6-terra")
        self.assertEqual(b["policy"]["final_review"]["effort"], "high")
        self.assertEqual(b["items"][0]["state"], "queued")

    def test_whole_submit_rejected(self):
        bad = [
            [item("a"), item("a")],
            [item("a", repo="rel/path")],
            [item("a", builder="gemini")],
            [item("a", depends_on=["b"]), item("b", depends_on=["a"])],
            [item("a", depends_on=["zzz"])],
            [{"id": "a", "title": "t"}],
            [],
        ]
        for items in bad:
            with self.assertRaises(ValueError, msg=str(items)):
                make(items)

    def test_t4_same_agent_and_model_reviewer_rejected(self):
        with self.assertRaises(ValueError):
            B.check_reviewer({"agent": "claude", "model": "claude-sonnet-5-5"},
                             {"agent": "claude", "model": "claude-sonnet-5-5"})
        with self.assertRaises(ValueError):
            B.check_reviewer({"agent": "codex", "model": "gpt-5.6-terra"}, {"agent": "codex", "model": None})
        B.check_reviewer({"agent": "codex", "model": "gpt-5.6-terra"}, {"agent": "claude", "model": "claude-fable-5-2"})

    def test_max_rounds_mapping(self):  # T2 mapping; the 4-round run is in test_pipeline_blocked.py
        b = make([item("a")], max_fix_rounds=3)
        spec = B.pipeline_spec(b["items"][0], b["policy"], {"agent": "claude", "model": "claude-sonnet-5-5"},
                               {"agent": "claude", "model": "claude-fable-5-2"})
        self.assertEqual(spec["max_rounds"], 4)
        self.assertEqual(spec["builder"]["model"], "claude-sonnet-5-5")
        self.assertEqual(spec["reviewers"], [{"kind": "agent", "agent": "claude", "model": "claude-fable-5-2"}])


class Routing(unittest.TestCase):
    def test_t5_table(self):
        sonnet_limited = {"models": {"claude:claude-fable-5-2": NOW + 5}, "accounts": {}}
        cases = [
            ("auto", False, NO_LIMITS, ("claude", "claude-sonnet-5-5"), ("claude", "claude-fable-5-2")),
            ("auto", True, NO_LIMITS, ("claude", "claude-opus-5-5"), ("codex", "gpt-5.6-terra")),
            ("codex", False, NO_LIMITS, ("codex", "gpt-5.6-terra"), ("claude", "claude-fable-5-2")),
            ("codex", True, NO_LIMITS, ("codex", "gpt-5.6-terra"), ("claude", "claude-fable-5-2")),
            ("claude", False, sonnet_limited, ("claude", "claude-sonnet-5-5"), ("claude", "claude-opus-5-5")),
        ]
        for builder, sec, limits, want_build, want_review in cases:
            it = B.normalise_item(item("a", builder=builder, security_sensitive=sec))
            build = B.route_build(it, limits, NOW)
            self.assertEqual((build["agent"], build["model"]), want_build, (builder, sec))
            review = B.route_review(build, sec, limits, NOW)
            self.assertEqual((review["agent"], review["model"]), want_review, (builder, sec))

    def test_security_review_is_codex_high(self):
        build = B.route_build(B.normalise_item(item("a", security_sensitive=True)), NO_LIMITS, NOW)
        self.assertEqual(B.route_review(build, True, NO_LIMITS, NOW)["effort"], "high")

    def test_ac6_fable_never_builds_fixes_or_tests(self):
        for rows in B.BUILD_ROWS.values():
            for row in rows:
                self.assertNotIn("fable", row["model"] or "")
        sonnet_limited = {"models": {"claude:claude-sonnet-5-5": NOW + 9}, "accounts": {}}
        for builder in ("auto", "claude", "codex"):
            for sec in (False, True):
                for limits in (NO_LIMITS, sonnet_limited):
                    got = B.route_build(B.normalise_item(item("a", builder=builder, security_sensitive=sec)),
                                        limits, NOW)
                    self.assertNotIn("fable", str(got.get("model")))
        self.assertTrue(B.FABLE_NEVER_BUILDS)

    def test_limited_waits_for_earliest_reset(self):
        it = B.normalise_item(item("a", builder="codex"))
        got = B.route_build(it, {"models": {"codex:gpt-5.6-terra": NOW + 100}, "accounts": {}}, NOW)
        self.assertIsNone(got.get("agent"))
        self.assertEqual(got["wait_until"], NOW + 100)

    def test_account_limit_is_a_limit(self):
        it = B.normalise_item(item("a", builder="claude"))
        got = B.route_build(it, {"models": {}, "accounts": {"claude": NOW + 50}}, NOW)
        self.assertEqual(got["wait_until"], NOW + 50)


class Steps(unittest.TestCase):
    def test_t1_table(self):
        table = [("building", "building"), ("resolving", "building"), ("reviewing", "reviewing"),
                 ("review_waiting", "reviewing"), ("ready", "ready"), ("escalated", "escalated"),
                 ("failed", "failed"), ("cancelled", "cancelled")]
        for state, want in table:
            b = make([item("a")])
            b, actions = tick(b)
            self.assertEqual(actions[0]["do"], "start_item")
            b, _ = B.step(b, {"type": "pipeline", "id": "a", "pipeline": pipe(state)})
            self.assertEqual(b["items"][0]["state"], want, state)

    def test_tick_does_not_repeat_a_start(self):
        b = make([item("a")])
        b, first = tick(b)
        b, second = tick(b)
        self.assertEqual([a["do"] for a in first], ["start_item"])
        self.assertEqual(second, [])

    def test_t3_gate_bounce_budget_is_the_pipeline_constant(self):
        import pipeline as PL
        self.assertEqual(make([item("a")])["policy"]["gate_bounces"], PL.GATE_BOUNCES)
        self.assertEqual(B.GATE_BOUNCES, PL.GATE_BOUNCES)

    def test_escalation_reason_carried(self):
        b = make([item("a")])
        b, _ = tick(b)
        hist = [{"round": 1, "state": "escalated", "note": "owner approval needed: builder is blocked"}]
        b, _ = B.step(b, {"type": "pipeline", "id": "a", "pipeline": pipe("escalated", history=hist)})
        self.assertEqual(b["items"][0]["state"], "escalated")
        self.assertTrue(b["items"][0]["escalation"].startswith("owner approval needed"))

    def test_ac2_dependency_not_ready_settles_dependent(self):
        b = make([item("a"), item("b", depends_on=["a"])])
        b, _ = tick(b)
        b, _ = B.step(b, {"type": "pipeline", "id": "a", "pipeline": pipe("failed")})
        b, _ = tick(b)
        self.assertEqual(b["items"][1]["state"], "escalated")
        self.assertIn("dependency", b["items"][1]["escalation"])
        self.assertTrue(B.all_settled(b))

    def test_ac15_depends_and_overlap_run_in_order(self):
        b = make([item("a"), item("b", depends_on=["a"]), item("c", files=["x.py"]), item("d", files=["x.py"])],
                 concurrency={"claude": 9, "codex": 9})
        b, actions = tick(b)
        self.assertEqual(sorted(a["id"] for a in actions), ["a", "c"])
        b = settle_ready(b, ["a", "c"])
        b, actions = tick(b)
        self.assertEqual(sorted(a["id"] for a in actions), ["b", "d"])

    def test_t12_concurrency_cap(self):
        items = [item(f"x{i}", builder="codex") for i in range(4)] + [item(f"c{i}") for i in range(5)]
        b = make(items)
        b, actions = tick(b)
        by_agent = {}
        for a in actions:
            by_agent[a["agent"]] = by_agent.get(a["agent"], 0) + 1
        self.assertEqual(by_agent, {"codex": 1, "claude": 2})
        b, again = tick(b)
        self.assertEqual(again, [])

    def test_ac10_limit_wait_and_escalate(self):
        limits = {"models": {"codex:gpt-5.6-terra": NOW + 100}, "accounts": {}}
        b = make([item("a", builder="codex")])
        b, actions = tick(b, limits)
        self.assertEqual(actions, [])
        self.assertEqual(b["items"][0]["state"], "queued")
        self.assertEqual(b["items"][0]["wait_until"], NOW + 100)
        b, actions = tick(b, limits, now=NOW + 50)
        self.assertEqual(actions, [])
        b, actions = tick(b, NO_LIMITS, now=NOW + 101)
        self.assertEqual([a["do"] for a in actions], ["start_item"])
        far = {"models": {"codex:gpt-5.6-terra": NOW + 7 * 3600}, "accounts": {}}
        b2 = make([item("a", builder="codex")])
        b2, _ = tick(b2, far)
        self.assertEqual(b2["items"][0]["state"], "escalated")
        b3 = make([item("a", builder="codex")], on_limit="escalate")
        b3, _ = tick(b3, limits)
        self.assertEqual(b3["items"][0]["state"], "escalated")


class FinalReview(unittest.TestCase):
    def test_t6_gating(self):
        b = make([item("a"), item("b")], concurrency={"claude": 9, "codex": 9})
        b, _ = tick(b)
        b = settle_ready(b, ["a"])
        b, actions = tick(b)
        self.assertEqual(actions, [])
        b = settle_ready(b, ["b"])
        b, actions = tick(b)
        self.assertEqual([a["do"] for a in actions], ["spawn_final_review"])
        a = actions[0]
        self.assertEqual((a["agent"], a["model"], a["effort"]), ("codex", "gpt-5.6-terra", "high"))
        self.assertEqual(sorted(a["ids"]), ["a", "b"])
        self.assertEqual(b["state"], "final_review")
        b, one = tick(b)
        b, two = tick(b)
        self.assertEqual(one + two, [])

    def test_t7_codex_limited_parks_no_claude(self):
        b = make([item("a")])
        b, _ = tick(b)
        b = settle_ready(b, ["a"])
        limits = {"models": {"codex:gpt-5.6-terra": NOW + 300}, "accounts": {}}
        b, actions = tick(b, limits)
        self.assertEqual([a["do"] for a in actions], ["park_final"])
        self.assertEqual(actions[0]["until"], NOW + 300)
        self.assertNotIn("claude", json.dumps(actions))
        b, actions = tick(b, limits, now=NOW + 10)
        self.assertNotIn("spawn_final_review", [a["do"] for a in actions])
        b, actions = tick(b, NO_LIMITS, now=NOW + 301)
        self.assertEqual([a["do"] for a in actions], ["spawn_final_review"])

    def test_no_ready_items_no_final_review(self):
        b = make([item("a")])
        b, _ = tick(b)
        b, _ = B.step(b, {"type": "pipeline", "id": "a", "pipeline": pipe("escalated")})
        b, actions = tick(b)
        self.assertEqual(actions, [])
        self.assertEqual(b["state"], "done")

    def test_final_done_settles_batch(self):
        b = make([item("a")])
        b, _ = tick(b)
        b = settle_ready(b, ["a"])
        b, _ = tick(b)
        b, _ = B.step(b, {"type": "final_done", "verdict": "approve", "findings": ["x"], "model": "gpt-5.6-terra"})
        self.assertEqual(b["state"], "done")
        self.assertEqual(b["final"]["verdict"], "approve")


class FixRound(unittest.TestCase):
    def test_final_limited_parks_then_retries(self):
        b = make([item("a")])
        b, _ = tick(b)
        b = settle_ready(b, ["a"])
        b, _ = tick(b)
        b, _ = B.step(b, {"type": "final_limited", "until": NOW + 500})
        self.assertEqual((b["final"]["state"], b["final"]["attempt"]), ("parked", 1))
        b, actions = tick(b, now=NOW + 100)
        self.assertEqual(actions, [])
        b, actions = tick(b, now=NOW + 501)
        self.assertEqual([a["do"] for a in actions], ["spawn_final_review"])
        self.assertEqual(b["final"]["attempt"], 1)

    def test_final_limited_ignored_when_not_spawned(self):
        b = make([item("a")])
        b, _ = B.step(b, {"type": "final_limited", "until": NOW + 5})
        self.assertIsNone(b["final"])

    def test_effort_reaches_pipeline_spec(self):
        build = {"agent": "codex", "model": "gpt-5.6-terra", "effort": "medium"}
        review = {"agent": "claude", "model": "claude-fable-5-2", "effort": "medium"}
        spec = B.pipeline_spec(B.normalise_item(item("a")), B.new_batch({"items": [item("a")]})["policy"],
                               build, review, tag="b:a")
        self.assertEqual(spec["builder"]["effort"], "medium")
        self.assertEqual(spec["reviewers"][0]["effort"], "medium")
        self.assertEqual(spec["batch"]["tag"], "b:a")

    def test_slots_count_reviewing_and_final(self):
        b = make([item("a"), item("b")], concurrency={"claude": 2, "codex": 1})
        b, _ = tick(b)
        b["items"][0].update(state="reviewing", review={"agent": "claude", "model": "m"})
        self.assertEqual(B._slots(b).get("claude"), 2)  # a's reviewer + b's builder
        b["final"] = {"state": "spawned", "agent": "codex"}
        self.assertEqual(B._slots(b).get("codex"), 1)


class PolicyLocks(unittest.TestCase):
    def test_final_review_cannot_be_overridden(self):
        for fr in ({"agent": "claude"}, {"model": "gpt-5.6-sol"}, {"effort": "low"}):
            with self.assertRaises(ValueError):
                make([item("a")], final_review=fr)
        make([item("a")], final_review={"agent": "codex", "model": "gpt-5.6-terra", "effort": "high"})

    def test_concurrency_validated(self):
        for bad in ({"claude": 0}, {"claude": "2"}, {"codex": -1}, {"gemini": 1}, {"claude": True}, [1], 3):
            with self.assertRaises(ValueError, msg=bad):
                make([item("a")], concurrency=bad)
        b = make([item("a")], concurrency={"claude": 3})
        self.assertEqual(b["policy"]["concurrency"], {"codex": 1, "claude": 3})

    def test_gate_bounces_fixed(self):
        with self.assertRaises(ValueError):
            make([item("a")], gate_bounces=5)
        make([item("a")], gate_bounces=B.GATE_BOUNCES)


class Persistence(unittest.TestCase):
    def test_t9_restart_no_duplicate_spawn(self):
        b = make([item("a"), item("b")], concurrency={"claude": 9, "codex": 9})
        b, actions = tick(b)
        self.assertEqual(len(actions), 2)
        reloaded = json.loads(json.dumps(b))
        reloaded, actions = tick(reloaded)
        self.assertEqual(actions, [])
        reloaded = settle_ready(reloaded, ["a", "b"])
        reloaded, first = tick(reloaded)
        reloaded = json.loads(json.dumps(reloaded))
        reloaded, second = tick(reloaded)
        self.assertEqual(len(first), 1)
        self.assertEqual(second, [])


class Summary(unittest.TestCase):
    def _batch(self):
        b = make([item("a")])
        b, _ = tick(b)
        b["items"][0]["open_findings"] = [{"severity": "high", "problem": f"p{i}", "file": "f.py"} for i in range(8)]
        b["items"][0]["gate_tail"] = "x" * 5000
        return b

    def test_t11_trimmed_vs_full(self):
        b = self._batch()
        s = B.summary(b)
        self.assertNotIn("gate_tail", json.dumps(s))
        self.assertLessEqual(len(s["items"][0]["open_findings"]), 5)
        self.assertEqual(s["items"][0]["id"], "a")
        full = B.summary(b, full=True)
        self.assertEqual(len(full["items"][0]["gate_tail"]), 5000)
        self.assertEqual(len(full["items"][0]["open_findings"]), 8)

    def test_model_counts(self):
        b = self._batch()
        b["items"][0]["models"] = {"builder": {"claude-sonnet-5-5": 2}, "reviewer": {"claude-fable-5-2": 2}}
        self.assertEqual(B.summary(b)["task_counts"]["claude-sonnet-5-5"], 2)


class Static(unittest.TestCase):
    def test_t13_no_forbidden_calls(self):
        src = (ROOT / "master" / "controller" / "batch.py").read_text()
        code = re.sub(r'""".*?"""', "", src, flags=re.S)
        code = "\n".join(re.sub(r"#.*", "", line) for line in code.splitlines())
        code = re.sub(r'"[^"\n]*"|\'[^\'\n]*\'', '""', code)
        for word in ("merge", "push", "approve", "unblock", "complete_task", "cleanup_task", "budget_override",
                     "httpx", "subprocess"):
            self.assertIsNone(re.search(rf"\b{word}\w*", code), word)


if __name__ == "__main__":
    unittest.main()
