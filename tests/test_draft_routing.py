"""kind=draft with an explicit sonnet/opus model is admitted on Haiku (docs/TOKEN-DEEPDIVE-2026-11-17.md 2.5).
Run: uv run -q --with fastapi --with uvicorn --with httpx --with 'mcp<2' python -m unittest -v tests/test_draft_routing.py
"""
import unittest

from tests.test_controller import FakeRunner, W  # noqa: F401  (sets up the controller test environment)

import budget  # noqa: E402
import store  # noqa: E402

HAIKU = "claude-haiku-5-5"


def admit(kind, model, override=False):
    return budget.admit(agent="claude", model=model, kind=kind, effort=None, override=override)


class DraftRoutingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        store.init_db()

    def test_explicit_sonnet_draft_goes_to_haiku(self):
        model = "claude-sonnet-5-5"
        d = admit("draft", model)
        self.assertEqual(d["model"], HAIKU)
        self.assertEqual(d["routed_from"], budget.identity("claude", model))
        self.assertEqual(d["routed_why"], "draft kind -> haiku")

    def test_explicit_opus_draft_is_refused_by_guard_not_rewritten(self):
        with self.assertRaises(budget.Refusal) as caught:
            admit("draft", "claude-opus-5-5")
        self.assertEqual(caught.exception.status, 400)
        self.assertEqual(caught.exception.refused_by, "model_guard")

    def test_other_routes_have_no_draft_reason(self):
        self.assertIsNone(admit("build", "claude-sonnet-5-5")["routed_why"])

    def test_budget_override_keeps_explicit_model(self):
        d = admit("draft", "claude-sonnet-5-5", override=True)
        self.assertEqual(d["model"], "claude-sonnet-5-5")
        self.assertIsNone(d["routed_from"])

    def test_other_kinds_unchanged(self):
        d = admit("build", "claude-sonnet-5-5")
        self.assertEqual(d["model"], "claude-sonnet-5-5")
        self.assertIsNone(d["routed_from"])

    def test_draft_without_model_or_with_haiku_unchanged(self):
        for model in (None, HAIKU):
            d = admit("draft", model)
            self.assertEqual(d["model"], model)
            self.assertIsNone(d["routed_from"])


if __name__ == "__main__":
    unittest.main()
