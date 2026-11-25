"""Cleanup must not destroy unsaved or unmerged work.
Run: uv run -q --with fastapi --with uvicorn --with httpx --with 'mcp<2' python -m unittest tests/test_cleanup_safety.py
"""
import asyncio
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "master" / "controller"))
from tests.test_controller import TMP  # noqa: E402,F401  (sets up the controller test environment)
from tests.test_runner_budget import Base  # noqa: E402

import tasks  # noqa: E402

ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
       "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], env=ENV, capture_output=True, text=True)


def make_repo(path):
    path.mkdir(parents=True)
    git(path, "init", "-q", "-b", "main")
    git(path, "commit", "--allow-empty", "-q", "-m", "base")


class RunnerCleanup(Base):
    def test_dirty_worktree_commit_kept_on_branch(self):
        repo = self.code / "repo"
        make_repo(repo)
        wt = self.code / "wt"
        git(repo, "worktree", "add", "-q", "-b", "fleet/t-1", str(wt))
        (wt / "f.txt").write_text("work")
        job = {"id": "t-1", "state": "done", "worktree": True, "cwd": str(wt), "repo": str(repo)}
        self.m.refresh = lambda j: None
        self.m.public = lambda j: {"id": j["id"]}
        saved = {k: os.environ.get(k) for k in ENV if k.startswith("GIT_")}
        os.environ.update({k: v for k, v in ENV.items() if k.startswith("GIT_")})
        try:
            self.m.cleanup(job)
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        self.assertFalse(wt.exists())
        self.assertEqual(git(repo, "show", "fleet/t-1:f.txt").stdout, "work")


class ControllerCleanup(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(os.path.realpath(self.tmp.name)) / "repo"
        make_repo(self.repo)
        self.saved = (tasks.get_task, tasks.node_of, tasks.runner_call, tasks.emit)
        self.task = {"state": "done", "title": "t", "repo": str(self.repo), "branch": "fleet/t-2"}
        tasks.get_task = lambda _id: self.task

        async def fake_call(*a, **k):
            return {"id": "t-2"}
        tasks.node_of = lambda t: {}
        tasks.runner_call = fake_call
        tasks.emit = lambda *a, **k: None
        tasks._locks.clear()

    def tearDown(self):
        tasks.get_task, tasks.node_of, tasks.runner_call, tasks.emit = self.saved
        self.tmp.cleanup()

    def branch_exists(self):
        return git(self.repo, "rev-parse", "--verify", "-q", "fleet/t-2").returncode == 0

    def test_unmerged_branch_kept(self):
        git(self.repo, "checkout", "-q", "-b", "fleet/t-2")
        git(self.repo, "commit", "--allow-empty", "-q", "-m", "work")
        git(self.repo, "checkout", "-q", "main")
        out = asyncio.run(tasks.cleanup("t-2"))
        self.assertTrue(out["branch_kept"])
        self.assertTrue(self.branch_exists())

    def test_merged_branch_removed(self):
        git(self.repo, "branch", "fleet/t-2")
        out = asyncio.run(tasks.cleanup("t-2"))
        self.assertFalse(out["branch_kept"])
        self.assertFalse(self.branch_exists())

    def test_uncheckable_keeps_branch(self):
        self.task["repo"] = "/nonexistent"
        out = asyncio.run(tasks.cleanup("t-2"))
        self.assertTrue(out["branch_kept"])

    def test_multi_commit_no_ff_merge_removed(self):
        git(self.repo, "checkout", "-q", "-b", "fleet/t-2")
        for n in ("a", "b", "c"):
            (self.repo / (n + ".txt")).write_text(n)
            git(self.repo, "add", "-A")
            git(self.repo, "commit", "-q", "-m", n)
        git(self.repo, "checkout", "-q", "main")
        git(self.repo, "merge", "--no-ff", "-q", "-m", "merge", "fleet/t-2")
        out = asyncio.run(tasks.cleanup("t-2"))
        self.assertFalse(out["branch_kept"])
        self.assertFalse(self.branch_exists())

    def _merge_then_autosave(self, path):
        git(self.repo, "checkout", "-q", "-b", "fleet/t-2")
        git(self.repo, "commit", "--allow-empty", "-q", "-m", "work")
        git(self.repo, "checkout", "-q", "main")
        git(self.repo, "merge", "--no-ff", "-q", "-m", "merge", "fleet/t-2")
        git(self.repo, "checkout", "-q", "fleet/t-2")
        (self.repo / path).parent.mkdir(parents=True, exist_ok=True)
        (self.repo / path).write_text("x")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "fleet: work from task t-2")
        git(self.repo, "checkout", "-q", "main")

    def test_autosave_of_cache_file_after_merge_removed(self):
        self._merge_then_autosave(".vite/vitest/results.json")
        out = asyncio.run(tasks.cleanup("t-2"))
        self.assertFalse(out["branch_kept"])
        self.assertFalse(self.branch_exists())

    def test_autosave_of_real_file_after_merge_kept(self):
        self._merge_then_autosave("src/new.py")
        out = asyncio.run(tasks.cleanup("t-2"))
        self.assertTrue(out["branch_kept"])
        self.assertTrue(self.branch_exists())

    def test_autosave_after_main_moved_on_removed(self):
        self._merge_then_autosave("ui/.vite/vitest/results.json")
        (self.repo / "later.txt").write_text("l")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "main moves on")
        out = asyncio.run(tasks.cleanup("t-2"))
        self.assertFalse(out["branch_kept"])
        self.assertFalse(self.branch_exists())

    def test_autosave_plus_unmerged_commit_kept(self):
        self._merge_then_autosave(".vite/r.json")
        git(self.repo, "checkout", "-q", "fleet/t-2")
        (self.repo / "real.py").write_text("x")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "real work")
        git(self.repo, "checkout", "-q", "main")
        out = asyncio.run(tasks.cleanup("t-2"))
        self.assertTrue(out["branch_kept"])
        self.assertTrue(self.branch_exists())

    def test_autosave_of_node_modules_kept(self):
        self._merge_then_autosave("node_modules/x.js")
        out = asyncio.run(tasks.cleanup("t-2"))
        self.assertTrue(out["branch_kept"])
        self.assertTrue(self.branch_exists())

    def test_force_keeps_old_behaviour(self):
        git(self.repo, "branch", "fleet/t-2")
        out = asyncio.run(tasks.cleanup("t-2", force=True))
        self.assertNotIn("branch_kept", out)
        self.assertTrue(self.branch_exists())


if __name__ == "__main__":
    unittest.main()
