"""Plan 2.4: merged review tasks close themselves; unmerged and missing branches are untouched.
Run: uv run -q --with fastapi --with uvicorn --with httpx --with 'mcp<2' python -m unittest tests/test_merge_close.py
"""
import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "master" / "controller"))
from tests.test_controller import TMP  # noqa: E402,F401
from tests.test_cleanup_safety import git, make_repo  # noqa: E402

import handoff  # noqa: E402
import merge_close  # noqa: E402
import tasks  # noqa: E402


class MergeClose(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(os.path.realpath(self.tmp.name)) / "repo"
        make_repo(self.repo)
        self.saved = (tasks.get_task, tasks.node_of, tasks.runner_call, tasks.emit, tasks.transition,
                      merge_close.emit, handoff.pipeline_owned)
        self.task = {"state": "review", "title": "t", "repo": str(self.repo), "branch": "fleet/t-3"}
        self.events, self.calls = [], []
        tasks.get_task = lambda _id: self.task

        def transition(task, allowed, new, **k):
            if task["state"] not in allowed:
                return False
            task["state"] = new
            return True

        async def fake_call(node, method, path, *a, **k):
            self.calls.append(path)
            return {"id": "t-3"}
        tasks.transition = transition
        tasks.node_of = lambda t: {}
        tasks.runner_call = fake_call
        tasks.emit = lambda *a, **k: None
        merge_close.emit = lambda tid, kind, data: self.events.append((tid, kind, data))
        handoff.pipeline_owned = lambda _id: False
        tasks._locks.clear()

    def tearDown(self):
        (tasks.get_task, tasks.node_of, tasks.runner_call, tasks.emit, tasks.transition,
         merge_close.emit, handoff.pipeline_owned) = self.saved
        self.tmp.cleanup()

    def exists(self):
        return git(self.repo, "rev-parse", "--verify", "-q", "fleet/t-3").returncode == 0

    def test_merged_closed_and_cleaned(self):
        git(self.repo, "branch", "fleet/t-3")
        self.assertTrue(asyncio.run(merge_close.close_if_merged("t-3")))
        self.assertEqual(self.task["state"], "done")
        self.assertIn("/jobs/t-3/cleanup", self.calls)
        self.assertFalse(self.exists())
        self.assertEqual([e[1] for e in self.events], ["task.auto_closed"])

    def test_unmerged_untouched(self):
        git(self.repo, "checkout", "-q", "-b", "fleet/t-3")
        git(self.repo, "commit", "--allow-empty", "-q", "-m", "work")
        git(self.repo, "checkout", "-q", "main")
        self.assertFalse(asyncio.run(merge_close.close_if_merged("t-3")))
        self.assertEqual(self.task["state"], "review")
        self.assertEqual(self.calls, [])
        self.assertTrue(self.exists())
        self.assertEqual(self.events, [])

    def test_missing_branch_untouched(self):
        self.assertFalse(asyncio.run(merge_close.close_if_merged("t-3")))
        self.assertEqual(self.task["state"], "review")
        self.assertEqual(self.calls, [])
        self.assertEqual(self.events, [])

    def test_non_review_and_protected_skipped(self):
        git(self.repo, "branch", "fleet/t-3")
        self.task["state"] = "failed"
        self.assertFalse(asyncio.run(merge_close.close_if_merged("t-3")))
        self.task.update(state="review", branch="main")
        self.assertFalse(asyncio.run(merge_close.close_if_merged("t-3")))
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
