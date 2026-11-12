"""Part E: stalled-task detection and response (docs/SPEC-stall-reroute.md; §12 v2 overrides §3-§10).

Each pass of live.sweep_once classifies every working task from the runner's own /health "jobs" report (polled or
pushed in a heartbeat; never a node file), then responds:
- dead or unreachable: stop the job under the task lock, then reroute its work (handoff.handle_handoff);
- idle: Claude and Codex get one nudge (stop, then one admission-checked resume on the same session; nudged_at is
  persisted so it happens once, even across restarts); Ollama, or a second idle after the nudge, is rerouted;
- overrun: an alert only; the runner's own kill stays the hard stop.

Lock order as Part C: the stop holds only the task lock; the reroute takes the work-item lock, then the task lock.
Pipeline-owned tasks are only stopped here; the pipeline poll hands them on under its own lock (12.13).
"""
import json
import time

from fastapi import HTTPException

import handoff
import tasks
from events import emit
from store import db

AUTO = True  # the sweep runs the stall pass (tests turn it off and call sweep() themselves)
STALL_KINDS = ("dead", "idle", "unreachable")
RESTART_GRACE = 120  # 12.15: no verdicts this long after a controller restart
TASK_GRACE = 300  # 12.15: nor during a task's (run's) first 5 minutes
FRESH = 60  # runner data older than this says nothing (the warm-model age rule, Part D 12.3)
UNREACHABLE_AFTER = 3  # consecutive failed job polls while the runner's /health answers
DEFAULT_JOB_MINUTES = 120  # the runner's max_job_minutes default
IDLE_MIN, IDLE_MAX = 15 * 60, 40 * 60
OVERRUN_SHARE = 0.8
NUDGE_TEXT = ("You have made no progress for {minutes} min. Report your status and continue, or stop and summarize "
              "what is done and what is left.")

CPU_SEEN: dict[str, float] = {}  # task id -> the job's CPU seconds at the last look


def reset_state() -> None:
    CPU_SEEN.clear()


def job_minutes(task: dict) -> float:
    """The job's time limit: the task policy's task_minutes, else the runner's default."""
    limits = (task.get("policy") or {}).get("limits") or {}
    value = limits.get("task_minutes")
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
        return float(value)
    return float(DEFAULT_JOB_MINUTES)


def idle_after(task: dict) -> float:
    """12.4: clamp(0.25 x job timeout, 15 min, 40 min), in seconds."""
    return min(max(0.25 * job_minutes(task) * 60, IDLE_MIN), IDLE_MAX)


def runner_age(node: dict, now: float) -> float | None:
    seen = max([t for t in (node.get("heartbeat_at"), node.get("polled_at")) if t] or [0])
    if not seen:
        return None
    return now - seen + (node.get("runner_age_s") or 0)


def _verdict(kind: str, detail: str, **extra) -> dict:
    return {"kind": kind, "detail": detail, "overrun": False, "idle_s": None} | extra


def classify(task: dict, node: dict | None, now: float) -> dict:
    """{"kind": ok|unknown|grace|dead|idle|unreachable, "detail", "overrun", "idle_s"} for one working task, from
    the latest runner data. Only "dead", "idle" and "unreachable" are stalls; "unknown" (no fresh data, an older
    runner without "jobs", the job not listed) never is."""
    if node is None or tasks.runner_offline(node, now):
        return _verdict("unknown", "the node is offline (Part D handles it)")
    started = task.get("run_started_at") or task.get("started_at")
    if not started or now - started < TASK_GRACE:
        return _verdict("grace", "the run started less than 5 min ago")
    age = runner_age(node, now)
    if age is None or age >= FRESH:
        return _verdict("unknown", "the runner's report is stale")
    jobs = (node.get("runner") or {}).get("jobs")
    if not isinstance(jobs, list):
        return _verdict("unknown", "the runner doesn't report jobs")
    elapsed = now - started
    overrun = elapsed > OVERRUN_SHARE * job_minutes(task) * 60
    if tasks.UNREACHABLE_POLLS.get(task["id"], 0) >= UNREACHABLE_AFTER:
        return _verdict("unreachable", f"the runner answered /health but not for this job "
                                       f"{tasks.UNREACHABLE_POLLS[task['id']]} times in a row", overrun=overrun)
    job = next((j for j in jobs if isinstance(j, dict) and j.get("id") == task["id"]), None)
    if job is None:
        return _verdict("unknown", "the runner's report doesn't list this job", overrun=overrun)
    if job.get("state") == "running" and job.get("alive") is False and job.get("gate_alive") is not True:
        return _verdict("dead", "the runner says running but neither the agent nor the gate process is alive",
                        overrun=overrun)
    idle_s, cpu_s = job.get("idle_s"), job.get("cpu_s")
    previous = CPU_SEEN.get(task["id"])
    if isinstance(cpu_s, (int, float)):
        CPU_SEEN[task["id"]] = cpu_s
    grew = previous is not None and isinstance(cpu_s, (int, float)) and cpu_s > previous
    threshold = idle_after(task)
    if handoff.stall_record(task).get("nudged_at"):
        threshold /= 2  # after the nudge, a further idle_after / 2 (5.2)
    alive = job.get("alive") is True or job.get("gate_alive") is True
    if alive and isinstance(idle_s, (int, float)) and idle_s >= threshold and not grew:
        return _verdict("idle", f"no output, file or CPU activity for {int(idle_s // 60)} min", overrun=overrun,
                        idle_s=idle_s)
    return _verdict("ok", "active", overrun=overrun, idle_s=idle_s)


# ---------- the response ----------

async def stop(task_id: str, verdict: dict, *, nudge: bool, now: float) -> str:
    """Under the task lock (12.6): cancel, then read the job the runner returns. A job that already exited is
    settled as the poll would; otherwise the task becomes failed(stalled), never cancelled. Without the runner's
    confirmation that the process is gone it is escalated instead (12.9). With `nudge`, one resume on the same
    session follows. Returns "gone" | "settled" | "settled_failed" | "escalated" | "nudged" | "stopped"."""
    async with tasks._locks[task_id]:
        task = tasks.get_task(task_id)
        if task["state"] != "working":
            return "gone"
        try:
            job = await tasks.runner_call(tasks.node_of(task), "POST", f"/jobs/{task_id}/cancel")
        except HTTPException:
            job = None
        job = job if isinstance(job, dict) else None
        state = (job or {}).get("state")
        if job and state not in ("running", "cancelled", "lost"):
            new = tasks.outcome(task, job)
            if new != "working":
                fields = tasks.job_fields(job)
                if tasks.transition(task, {"working"}, new, **fields):
                    tasks.record_usage(task_id)
                    return "settled_failed" if new in ("failed", "interrupted") else "settled"
                return "gone"
        confirmed = state in ("cancelled", "lost")
        record = handoff.stall_record(task)
        record.update(stopped_at=now)
        if not confirmed:
            record.update(escalated=True, escalated_at=now,
                          escalated_reason="the runner could not confirm the process is gone")
        fields = {"failure_reason": "stalled", "error": f"stalled ({verdict['kind']}): {verdict['detail']}"[:500],
                  "stall": json.dumps(record)}
        if job and isinstance(job.get("usage"), dict):
            fields["usage"] = json.dumps(job["usage"])
        if not tasks.transition(task, {"working"}, "failed", **fields):
            return "gone"
        tasks.record_usage(task_id)
        CPU_SEEN.pop(task_id, None)
        if not confirmed:
            emit(task_id, "task.escalated", {"title": task["title"], "node": task["node_id"],
                                             "reason": record["escalated_reason"]})
            return "escalated"
        if not nudge:
            return "stopped"
        record["nudged_at"] = now  # persisted before the resume: one nudge, even across restarts
        handoff.save_stall(task_id, record)
        minutes = int((verdict.get("idle_s") or 0) // 60)
        try:
            await tasks.resume(tasks.get_task(task_id), NUDGE_TEXT.format(minutes=minutes), [])
        except HTTPException as exc:  # admission deferred or refused it, or the runner failed: reroute instead
            emit(task_id, "task.nudge_held", {"title": task["title"], "reason": str(exc.detail)[:300]})
            return "stopped"
        emit(task_id, "task.nudged", {"title": task["title"], "idle_min": minutes})
        return "nudged"


async def respond(task: dict, verdict: dict, now: float) -> str:
    record = handoff.stall_record(task)
    owned = handoff.pipeline_owned(task["id"])
    nudge = (verdict["kind"] == "idle" and task["agent"] != "ollama" and bool(task.get("session_id"))
             and not record.get("nudged_at") and not owned)
    result = await stop(task["id"], verdict, nudge=nudge, now=now)
    if owned:
        return result  # the pipeline poll hands it on, under the pipeline lock
    if result == "stopped":
        await handoff.handle_handoff(task["id"], "stalled", now=now)
    elif result == "settled_failed":
        await handoff.after_settled(task["id"])
    return result


def _note(task: dict, verdict: dict, now: float) -> None:
    """Store the verdict and emit task.stalled / task.recovered once per transition, task.overrun once."""
    record = handoff.stall_record(task)
    before = record.get("kind")
    changed = False
    if verdict["kind"] in STALL_KINDS and before != verdict["kind"]:
        record.update(kind=verdict["kind"], since=now, detail=verdict["detail"],
                      last_activity_at=(now - verdict["idle_s"]) if verdict.get("idle_s") is not None else None)
        changed = True
        emit(task["id"], "task.stalled", {"title": task["title"], "node": task["node_id"], "kind": verdict["kind"],
                                          "detail": verdict["detail"]})
    elif verdict["kind"] == "ok" and before in STALL_KINDS:
        record.update(kind="recovered", since=now, detail="activity resumed")
        changed = True
        emit(task["id"], "task.recovered", {"title": task["title"], "node": task["node_id"], "was": before})
    if verdict.get("overrun") and not record.get("overrun_at"):
        record["overrun_at"] = now
        changed = True
        emit(task["id"], "task.overrun", {"title": task["title"], "node": task["node_id"],
                                          "minutes": int(job_minutes(task))})
    if changed:
        handoff.save_stall(task["id"], record)


async def sweep(now: float | None = None) -> None:
    """One stall pass: classify and respond for every working task, then retry the stall reroutes waiting for a
    free slot (level-triggered, like Part D's failover)."""
    import live  # local import: live imports this module
    now = time.time() if now is None else now
    if now - live.STARTED_AT < RESTART_GRACE:
        return
    nodes = {n["id"]: n for n in tasks.node_rows()}
    with db() as c:
        working = [r[0] for r in c.execute("SELECT id FROM tasks WHERE state='working' ORDER BY started_at, rowid")]
    for task_id in working:
        try:
            task = tasks.get_task(task_id)
            if task["state"] != "working":
                continue
            verdict = classify(task, nodes.get(task["node_id"]), now)
            if verdict["kind"] in ("unknown", "grace"):
                continue
            _note(task, verdict, now)
            if verdict["kind"] in STALL_KINDS:
                await respond(tasks.get_task(task_id), verdict, now)
        except Exception as exc:  # one task must not stop the others
            print(f"stall check of {task_id} failed: {exc!r}")
    with db() as c:
        waiting = [r[0] for r in c.execute(
            "SELECT id FROM tasks WHERE state IN ('failed','interrupted') AND resolution IS NULL AND stall LIKE "
            "'%\"waiting\"%'")]
    for task_id in waiting:
        try:
            task = tasks.get_task(task_id)
            if not handoff.stall_record(task).get("waiting") or handoff.pipeline_owned(task_id):
                continue
            await handoff.handle_handoff(task_id, "interrupted" if task["state"] == "interrupted" else "stalled",
                                         now=now)
        except Exception as exc:
            print(f"stall reroute of {task_id} failed: {exc!r}")
