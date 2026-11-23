"""Plan 2.4: a task whose branch was merged into main closes itself (complete + cleanup) instead of waiting for
the orchestrator. Runs as a pass of stall.sweep (itself a pass of live.sweep_once): no thread of its own.

Only tasks in state review are looked at, and only when the branch tip is an ancestor of main
(`git merge-base --is-ancestor`, via tasks._branch_merged). An unmerged, missing or uncheckable branch is left
untouched. Never pushes; never deletes main (the branch delete is `git branch -d`, which also refuses a
checked-out branch). Pipeline-owned tasks are left to the pipeline.
"""
import asyncio

import handoff
import tasks
from events import emit
from store import db

PROTECTED = {"main", "master"}


async def close_if_merged(task_id: str) -> bool:
    """Close one review task if its branch is merged. True when closed."""
    task = tasks.get_task(task_id)
    if task["state"] != "review" or handoff.pipeline_owned(task_id):
        return False
    branch = task.get("branch") or "fleet/" + task_id
    if branch in PROTECTED:
        return False
    merged, _reason = await asyncio.to_thread(tasks._branch_merged, task.get("repo"), branch)
    if not merged:
        return False
    await tasks.complete(task_id)
    cleaned = True
    try:
        await tasks.cleanup(task_id)
    except Exception as exc:  # completed but not cleaned: report it, the owner can still clean up by hand
        cleaned = False
        print(f"auto cleanup of {task_id} failed: {exc!r}")
    emit(task_id, "task.auto_closed", {"title": task["title"], "branch": branch, "cleaned": cleaned})
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
            print(f"merge close check of {task_id} failed: {exc!r}")
    return closed
