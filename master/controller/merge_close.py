"""Plan 2.4: a task whose branch was merged into main closes itself (complete + cleanup) instead of waiting for
the orchestrator. Runs as a pass of stall.sweep (itself a pass of live.sweep_once): no thread of its own.

Only tasks in state review are looked at. A task closes only when
- its kind is a build-like one (read-only reviews and checks never own commits, so their branch tip is trivially
  "merged": review, re_review, final_review, security_review, check are skipped, as is anything with review_of),
- its branch has commits of its own: the tip differs from the commit the branch was created at (the oldest reflog
  entry; no reflog means it can't be proven, so no close), and
- the tip is an ancestor of main (`git merge-base --is-ancestor`, via tasks._branch_merged).
An unmerged, missing or uncheckable branch is left untouched. Never pushes; never deletes main (tasks.cleanup
refuses main/master, and `git branch -d` refuses a checked-out branch). Pipeline-owned tasks are left to the
pipeline. State and ownership are re-checked under the task lock before the task is completed.
"""
import asyncio
import os
import subprocess

import handoff
import tasks
from events import emit
from store import db

_failed_seen: set[str] = set()  # tasks whose auto_close_failed was already emitted (once per task, per process)
PROTECTED = {"main", "master"}
READ_ONLY_KINDS = {"review", "re_review", "final_review", "security_review", "check"}


def has_own_commits(repo: str | None, branch: str) -> bool:
    """True when the branch tip is not the commit the branch started from (oldest reflog entry). Any failure or a
    missing reflog counts as False: no proof, no close."""
    def run(*args):
        return subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, timeout=30)
    try:
        log = run("reflog", "show", "--format=%H", f"refs/heads/{branch}")
        tip = run("rev-parse", "--verify", "-q", f"refs/heads/{branch}")
    except (OSError, subprocess.SubprocessError):
        return False
    entries = log.stdout.split()
    if log.returncode or tip.returncode or not entries:
        return False
    return tip.stdout.strip() != entries[-1]


def _failed(task_id: str, task: dict | None, stage: str, exc) -> None:
    if task_id in _failed_seen:
        return
    _failed_seen.add(task_id)
    print(f"auto close of {task_id} failed at {stage}: {exc!r}")
    emit(task_id, "task.auto_close_failed", {"title": (task or {}).get("title"), "stage": stage,
                                             "error": str(exc)[:300]})


def _eligible(task: dict, task_id: str) -> bool:
    return (task["state"] == "review" and task.get("kind") not in READ_ONLY_KINDS and not task.get("review_of")
            and not handoff.pipeline_owned(task_id))


async def close_if_merged(task_id: str) -> bool:
    """Close one review task if its branch is merged. True when closed."""
    task = tasks.get_task(task_id)
    if not _eligible(task, task_id):
        return False
    repo = task.get("repo")
    if not repo or not os.path.isdir(repo):
        return False  # nothing to check: no repo, no close, no event
    branch = task.get("branch") or "fleet/" + task_id
    if branch in PROTECTED:
        return False
    if not await asyncio.to_thread(has_own_commits, task.get("repo"), branch):
        return False
    merged, _reason = await asyncio.to_thread(tasks._branch_merged, task.get("repo"), branch)
    if not merged:
        return False
    async with tasks._locks[task_id]:
        task = tasks.get_task(task_id)  # re-check under the lock: the state may have moved while git ran
        if not _eligible(task, task_id) or not tasks.transition(task, {"review"}, "done"):
            return False
    try:
        await tasks.cleanup(task_id)
    except Exception as exc:  # completed but not cleaned: the owner can still clean up by hand
        _failed(task_id, task, "cleanup", exc)
        return True
    emit(task_id, "task.auto_closed", {"title": task["title"], "branch": branch, "cleaned": True})
    return True


async def sweep() -> list[str]:
    """One pass over the review tasks; returns the ids closed."""
    with db() as c:
        ids = [r[0] for r in c.execute("SELECT id FROM tasks WHERE state='review' ORDER BY updated_at, rowid")]
    closed = []
    for task_id in ids:
        try:
            if await close_if_merged(task_id):
                closed.append(task_id)
        except Exception as exc:  # one task must not stop the others
            _failed(task_id, None, "check", exc)
    return closed
