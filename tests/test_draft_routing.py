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

    def test_explicit_sonnet_and_opus_drafts_go_to_haiku(self):
        for model in ("claude-sonnet-5-5", "claude-opus-5-5"):
            d = admit("draft", model)
            self.assertEqual(d["model"], HAIKU)
            self.assertEqual(d["routed_from"], budget.identity("claude", model))

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
