"""find_agents: bundled Codex candidates (ChatGPT.app first, legacy Codex.app second)."""
import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("fleet_runner_find_codex", ROOT / "nodes" / "runner.py")
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class FindCodexTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.new = os.path.join(self.tmp.name, "chatgpt-codex")
        self.old = os.path.join(self.tmp.name, "legacy-codex")

    def make(self, path):
        Path(path).write_text("#!/bin/sh\n")
        os.chmod(path, 0o755)

    def find(self):
        with patch.object(runner, "CODEX_BUNDLED", (self.new, self.old)), \
             patch.object(runner, "agent_search_path", lambda: ""):
            return runner.find_agents("http://127.0.0.1:1", ollama_up=False)["codex"]

    def test_chatgpt_only(self):
        self.make(self.new)
        self.assertEqual(self.find(), self.new)

    def test_legacy_only(self):
        self.make(self.old)
        self.assertEqual(self.find(), self.old)

    def test_both_prefers_chatgpt(self):
        self.make(self.new)
        self.make(self.old)
        self.assertEqual(self.find(), self.new)

    def test_neither(self):
        self.assertIsNone(self.find())

    def test_not_executable_skipped(self):
        self.make(self.new)
        os.chmod(self.new, 0o644)
        self.make(self.old)
        self.assertEqual(self.find(), self.old)


if __name__ == "__main__":
    unittest.main()
