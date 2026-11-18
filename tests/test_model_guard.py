"""Model guard at admission (docs/SPEC-token-gaps-2026-11-17.md G1): Fable and Opus effort/kind.
Run: uv run -q --with fastapi --with uvicorn --with httpx --with 'mcp<2' python -m unittest -v tests/test_model_guard.py
"""
import copy
import unittest
from unittest import mock

from tests.test_controller import FakeRunner, W, fresh_loop_state, unmetered_claude_rules, wait_for  # noqa: F401

from fastapi.testclient import TestClient  # noqa: E402

import budget  # noqa: E402
import controller  # noqa: E402
import intel  # noqa: E402
import store  # noqa: E402

FABLE, OPUS = "claude-fable-5-2", "claude-opus-5-5"
KINDS = ["build", "fix", "test", "review", "re_review", "final_review", "security_review", "merge", "benchmark",
         "draft", "check", "lead"]


def admit(kind, model, effort=None, override=False, **kw):
    return budget.admit(agent="claude", model=model, kind=kind, effort=effort, override=override, **kw)


def refused(test, kind, model, effort=None):
    with test.assertRaises(budget.Refusal) as caught:
        admit(kind, model, effort)
    test.assertEqual(caught.exception.status, 400)
    test.assertEqual(caught.exception.refused_by, "model_guard")
    return caught.exception


class ModelGuardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        store.init_db()

    def test_fable_refused_on_every_kind_and_admitted_with_override(self):
        for kind in KINDS:
            with self.subTest(kind=kind):
                self.assertEqual(refused(self, kind, FABLE).message, "fable is paid: refused (owner override only)")
                d = admit(kind, FABLE, override=True)
                self.assertEqual(d["model"], FABLE)
                self.assertEqual(d["routed_why"], "owner override: fable is paid: refused (owner override only)")

    def test_opus_final_review_without_effort_gets_medium(self):
        for kind in ("final_review", "security_review"):
            self.assertEqual(admit(kind, OPUS)["effort"], "medium")

    def test_opus_high_effort_refused_unless_override(self):
        for effort in ("high", "xhigh", "max"):
            refused(self, "final_review", OPUS, effort)
        d = admit("final_review", OPUS, "high", override=True)
        self.assertEqual(d["effort"], "high")
        self.assertTrue(d["routed_why"].startswith("owner override: "))

    def test_opus_build_refused_unless_override(self):
        refused(self, "build", OPUS, "medium")
        for kind in ("fix", "test", "merge", "benchmark"):
            refused(self, kind, OPUS, "low")
        d = admit("build", OPUS, "medium", override=True)
        self.assertEqual(d["model"], OPUS)
        self.assertIn("owner override", d["routed_why"])

    def test_opus_check_and_draft_refused_by_guard_and_override_admits(self):
        for kind in ("check", "draft"):
            refused(self, kind, OPUS, "low")
        d = admit("draft", OPUS, "low", override=True)
        self.assertEqual(d["model"], OPUS)
        self.assertIn("owner override", d["routed_why"])
        self.assertIsNone(d["routed_from"])

    def test_check_with_opus_or_fable_is_refused_by_guard_first(self):
        for model in (OPUS, FABLE):
            refused(self, "check", model, "low")

    def test_opus_lead_and_reviews_default_to_low(self):
        for kind in ("lead", "review", "re_review"):
            self.assertEqual(admit(kind, OPUS)["effort"], "low")

    def test_refusal_detail_carries_refused_by(self):
        r = refused(self, "build", OPUS)
        self.assertEqual(r.detail["refused_by"], "model_guard")
        self.assertIn("error", r.detail)

    def test_sonnet_and_haiku_untouched(self):
        d = admit("build", "claude-sonnet-5-5")
        self.assertIsNone(d["routed_why"])

    def test_rank_never_returns_fable(self):
        rules = {"models": {"claude:claude-fable-*": {"tier": "premium", "kinds": ["*"]}},
                 "limits": {"near_limit_percent": 85}}
        for kind in intel.KIND_PREFERENCE:
            keys = [c["key"] for c in intel.rank(kind, rules=rules, stats={}, headroom={}, load={})]
            self.assertFalse(any("fable" in k for k in keys), kind)
        self.assertFalse(any("fable" in k for pref in intel.KIND_PREFERENCE.values() for k in pref))
        self.assertEqual(intel.KIND_PREFERENCE["lead"], ["claude:claude-sonnet-5-5", "claude:claude-opus-5-5"])

    def test_default_codex_fallback_is_opus(self):
        self.assertEqual(budget.DEFAULT_RULES["limits"]["fallbacks"]["codex"], ["claude:claude-opus-5-5"])

    def _codex_limited(self, rules):
        real = budget.blocked_by
        return (mock.patch.object(budget, "current_rules", return_value=rules),
                mock.patch.object(budget, "blocked_by",
                                  side_effect=lambda r, s, agent, key, ident: (9e12, "codex limited")
                                  if agent == "codex" else real(r, s, agent, key, ident)))

    def test_codex_limit_falls_back_to_opus_at_medium(self):
        rules = copy.deepcopy(budget.DEFAULT_RULES)
        rules["limits"]["on_limit"] = "fallback"
        a, b = self._codex_limited(rules)
        with a, b:
            d = budget.admit(agent="codex", model=None, kind="security_review", effort="high", override=False)
        self.assertEqual((d["agent"], d["model"]), ("claude", OPUS))
        self.assertEqual(d["effort"], "medium")
        self.assertEqual(d["routed_from"], "codex")

    def test_fallback_onto_a_refused_identity_is_skipped(self):
        rules = copy.deepcopy(budget.DEFAULT_RULES)
        rules["limits"]["on_limit"] = "fallback"
        rules["limits"]["fallbacks"]["codex"] = ["claude:claude-fable-5-2", "claude:claude-opus-5-5"]
        a, b = self._codex_limited(rules)
        with a, b:
            d = budget.admit(agent="codex", model=None, kind="security_review", effort=None, override=False)
        self.assertEqual(d["model"], OPUS)
        # A build is not allowed on Opus, so Codex stays queued rather than rerouting.
        a, b = self._codex_limited(rules)
        with a, b:
            d = budget.admit(agent="codex", model=None, kind="build", effort=None, override=False)
        self.assertEqual(d["agent"], "codex")
        self.assertEqual(d["wait"], "limit")

    def test_guard_ignores_rules_that_list_fable_and_opus_kinds(self):
        rules = copy.deepcopy(budget.DEFAULT_RULES)
        rules["models"]["claude:claude-fable-*"]["kinds"] = ["*"]
        rules["models"]["claude:claude-opus-*"]["kinds"] = ["*"]
        with mock.patch.object(budget, "current_rules", return_value=rules):
            for kind in ("build", "review", "final_review"):
                refused(self, kind, FABLE)
            refused(self, "build", OPUS)
            refused(self, "final_review", OPUS, "high")


class ModelGuardHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fresh_loop_state()
        cls.ctx = TestClient(controller.app, base_url="http://127.0.0.1")
        cls.c = cls.ctx.__enter__()
        cls.c.post("/api/refresh", headers=W)
        wait_for(lambda: any(n["id"] == "master" and n["runner_ok"] for n in cls.c.get("/api/fleet").json()["nodes"]))

    @classmethod
    def tearDownClass(cls):
        unmetered_claude_rules()
        cls.ctx.__exit__(None, None, None)

    def test_spawn_refusal_is_400_with_refused_by(self):
        r = self.c.post("/api/tasks", json={"prompt": "x", "agent": "claude", "model": FABLE, "kind": "review",
                                            "mode": "read-only", "worktree": False}, headers=W)
        self.assertEqual(r.status_code, 400, r.text)
        detail = r.json()["detail"]
        self.assertEqual(detail["refused_by"], "model_guard")
        self.assertIn("fable is paid", detail["error"])

    def test_override_admission_persists_routed_why_event(self):
        import json
        import store
        r = self.c.post("/api/tasks", json={"prompt": "x", "agent": "claude", "model": OPUS, "kind": "build",
                                            "effort": "medium", "mode": "read-only", "worktree": False,
                                            "budget_override": "owner test"}, headers=W)
        self.assertEqual(r.status_code, 200, r.text)
        tid = r.json()["id"]
        with store.db() as c:
            rows = c.execute("SELECT data FROM events WHERE task_id=? AND type='budget.override'", (tid,)).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertTrue(json.loads(rows[0][0])["routed_why"].startswith("owner override: "))


if __name__ == "__main__":
    unittest.main()
