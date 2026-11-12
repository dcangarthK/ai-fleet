"""Part F round 2 (docs/SPEC-dashboard-truth.md 2.1, requirements 1-3): one state -> label -> tone matrix, used by
/api/tasks, /api/work and the Live chain stages. Fixture work items per spec section 5.
Run: uv run --with fastapi --with uvicorn --with httpx --with 'mcp<2' python -m unittest -v tests/test_dashboard_display.py
"""
import json
import time
import unittest

from fastapi.testclient import TestClient

from tests.test_controller import W, reset_limits, unmetered_claude_rules, wait_for
from tests.test_tasks_lineage import fresh_loop_state

import controller
import lineage
import tasks
from store import db

NOW = 1_800_000_000.0


def ds(**kw):
    base = {"id": "t-1", "state": "failed", "agent": "claude", "failure_reason": None, "resets_at": None,
            "resolution": None, "ended_at": NOW - 10}
    return lineage.display_status(base | kw, now=NOW)


class MatrixTests(unittest.TestCase):
    """The pure part of the matrix (rows 2-4, 8, 10-16)."""

    def test_settled_resolutions(self):
        self.assertEqual(ds(resolution={"kind": "done_elsewhere", "commit": "ec2ab20", "by": "me"}),
                         {"tone": "resolved", "label": "Done elsewhere · ec2ab20"})
        self.assertEqual(ds(resolution={"kind": "handed_off", "task_id": "t-new"}),
                         {"tone": "resolved", "label": "Handed off → t-new"})
        self.assertEqual(ds(resolution={"kind": "dismissed"}), {"tone": "muted", "label": "Dismissed"})
        self.assertEqual(ds(resolution={"kind": "resolved_by", "task_id": "t-2"}),
                         {"tone": "muted", "label": "Superseded by t-2"})

    def test_unsettled_rows(self):
        self.assertEqual(ds(state="blocked"), {"tone": "attention", "label": "Blocked: needs approval"})
        self.assertEqual(ds(state="awaiting_approval"), {"tone": "attention", "label": "Blocked: needs approval"})
        self.assertEqual(ds(failure_reason="limit", resets_at=NOW + 600)["tone"], "paused")
        self.assertEqual(ds(failure_reason="limit", resets_at=NOW - 600),
                         {"tone": "attention", "label": "Limit cleared: resume or hand off"})
        self.assertEqual(ds(state="interrupted"), {"tone": "attention", "label": "Interrupted"})
        self.assertEqual(ds(failure_reason="error"), {"tone": "attention", "label": "Failed: error"})
        self.assertEqual(ds(state="cancelled"), {"tone": "muted", "label": "Cancelled"})
        self.assertEqual(ds(state="review"), {"tone": "ok", "label": "Ready for review"})
        self.assertEqual(ds(state="done"), {"tone": "ok", "label": "Done"})
        self.assertEqual(ds(state="queued"), {"tone": "running", "label": "Queued"})


class DisplayBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fresh_loop_state()
        cls.ctx = TestClient(controller.app, base_url="http://127.0.0.1")
        cls.c = cls.ctx.__enter__()
        unmetered_claude_rules()
        wait_for(lambda: any(n["id"] == "master" and n["runner_ok"] for n in cls.c.get("/api/fleet").json()["nodes"]))

    @classmethod
    def tearDownClass(cls):
        cls.ctx.__exit__(None, None, None)

    def setUp(self):
        reset_limits()
        self.n = 0

    def put(self, **kw):
        """Insert a task row directly (a fixture, not an engine run)."""
        self.n += 1
        now = time.time()
        base = {"id": f"t-dd{time.time_ns() % 10**9}{self.n}", "title": "Fixture", "agent": "claude", "model": None,
                "prompt": "p", "mode": "auto-edit", "node_id": "master", "state": "failed", "failure_reason": "error",
                "created_at": now + self.n * 0.01, "updated_at": now, "ended_at": now, "exit_code": 1}
        base |= {k: (json.dumps(v) if isinstance(v, (dict, list)) else v) for k, v in kw.items()}
        with db() as c:
            c.execute(f"INSERT INTO tasks ({','.join(base)}) VALUES ({','.join('?' * len(base))})", tuple(base.values()))
        return base["id"]

    def api_task(self, tid):
        return self.c.get(f"/api/tasks/{tid}").json()["display"]

    def list_task(self, tid):
        return next(t for t in self.c.get("/api/tasks?limit=500").json() if t["id"] == tid)["display"]

    def work(self, root):
        item = next(i for i in self.c.get("/api/work?limit=500").json() if i["root_id"] == root)
        return item, {a["id"]: a["display"] for a in item["attempts"]}

    def stages(self, root):
        chain = next(c for c in self.c.get("/api/live").json()["chains"] if c.get("root_id") == root)
        return chain["stages"]

    def same_everywhere(self, tid, root, label, tone):
        want = {"tone": tone, "label": label}
        self.assertEqual(self.api_task(tid), want)
        self.assertEqual(self.list_task(tid), want)
        self.assertEqual(self.work(root)[1][tid], want)
        stage = next(s for s in self.stages(root) if s.get("task_id") == tid)
        self.assertEqual(stage["display"], want)


class FixtureWorkItems(DisplayBase):
    def test_done_elsewhere(self):
        t = self.put(resolution={"kind": "done_elsewhere", "commit": "ec2ab20", "by": "Sonnet"})
        self.same_everywhere(t, t, "Done elsewhere · ec2ab20", "resolved")

    def test_handed_off(self):
        t = self.put(resolution={"kind": "handed_off", "task_id": "t-next", "agent": "claude"})
        self.same_everywhere(t, t, "Handed off → t-next", "resolved")

    def test_dismissed(self):
        t = self.put(resolution={"kind": "dismissed"})
        self.same_everywhere(t, t, "Dismissed", "muted")

    def test_merged_with_failed_attempts_is_never_failed_or_red(self):
        root = self.put(outcome={"kind": "merged", "commit": "ec2ab20", "at": time.time()}, state="done",
                        failure_reason=None, exit_code=0)
        gate = self.put(parent_id=root, link="fix", failure_reason="gate_failed")
        good = self.put(parent_id=root, link="review", state="review", failure_reason=None, exit_code=0)
        want = {"tone": "ok", "label": "Merged · ec2ab20"}
        self.assertEqual(self.api_task(gate), want)
        self.assertEqual(self.work(root)[1][gate], want)
        self.assertEqual(self.work(root)[0]["display"], want)
        self.assertEqual(self.api_task(good)["label"], "Ready for review")  # a successful attempt keeps its state

    def test_superseded_by_later_success_without_a_stored_resolution(self):
        root = self.put(link="build", state="failed")
        later = self.put(parent_id=root, link="retry", state="review", failure_reason=None, exit_code=0)
        self.same_everywhere(root, root, f"Superseded by {later}", "muted")

    def test_failed_attempt_with_retry_child_still_running_reads_retried(self):
        root = self.put()
        child = self.put(parent_id=root, link="retry", state="working", failure_reason=None, exit_code=None)
        self.same_everywhere(root, root, f"Retried as {child}", "resolved")

    def test_auto_approved_first_run_is_not_blocked(self):
        """The resume event stores how the earlier run ended; an auto-approved denial is not 'Blocked'."""
        t = self.put(state="working", failure_reason=None, exit_code=None, runs=2)
        with db() as c:
            c.execute("INSERT INTO events (ts,task_id,type,data) VALUES (?,?,?,?)",
                      (time.time() + 5, t, "task.resumed", json.dumps(
                          {"run": 2, "from": "blocked", "failure_reason": "denied", "auto_approved": True,
                           "display": {"tone": "attention", "label": "Blocked: needs approval"}})))
        first = self.work(t)[0]["attempts"][0]
        self.assertEqual(first["display"], {"tone": "muted", "label": "Auto-approved and resumed"})

    def test_blocked_now_is_blocked(self):
        t = self.put(state="blocked", failure_reason="denied")
        self.same_everywhere(t, t, "Blocked: needs approval", "attention")

    def test_cancelled_chain_reads_cancelled_not_failed(self):
        root = self.put(state="cancelled", failure_reason="cancelled")
        self.same_everywhere(root, root, "Cancelled", "muted")

    def test_interrupted_until_settled(self):
        t = self.put(state="interrupted", failure_reason="interrupted")
        self.same_everywhere(t, t, "Interrupted", "attention")
        with db() as c:
            c.execute("UPDATE tasks SET resolution=? WHERE id=?", (json.dumps({"kind": "dismissed"}), t))
        self.same_everywhere(t, t, "Dismissed", "muted")

    def test_limited_and_requeued(self):
        """A limit-paused attempt that was requeued shows the retry, in every vocabulary."""
        root = self.put(failure_reason="limit", resets_at=time.time() + 3600)
        child = self.put(parent_id=root, link="retry", state="queued", failure_reason=None, exit_code=None)
        self.same_everywhere(root, root, f"Retried as {child}", "resolved")

    def test_hidden_keeps_its_label(self):
        t = self.put(resolution={"kind": "dismissed"}, hidden=1)
        self.assertEqual(self.api_task(t), {"tone": "muted", "label": "Dismissed"})
        self.assertEqual(self.list_task(t), {"tone": "muted", "label": "Dismissed"})

    def test_live_stage_label_comes_from_display_not_state(self):
        root = self.put(state="done", failure_reason=None, exit_code=0)
        stage = self.stages(root)[0]
        self.assertEqual(stage["display"], {"tone": "ok", "label": "Done"})


class BatchTests(DisplayBase):
    def test_query_count_does_not_grow_with_rows(self):
        def queries(n):
            ids = [self.put() for _ in range(n)]
            count = {"n": 0}
            real = tasks.db

            class Counting:
                def __init__(self):
                    self.cm = real()

                def __enter__(self):
                    conn = self.cm.__enter__()
                    conn.set_trace_callback(lambda sql: count.__setitem__("n", count["n"] + 1))
                    return conn

                def __exit__(self, *a):
                    return self.cm.__exit__(*a)

            tasks.db = Counting
            try:
                got = tasks.list_tasks(limit=2000)
            finally:
                tasks.db = real
            self.assertTrue(set(ids) <= {t["id"] for t in got})
            return count["n"]

        queries(1)  # warm-up: first calls initialise lazily
        small = queries(5)
        large = queries(60)
        self.assertEqual(small, large, (small, large))


if __name__ == "__main__":
    unittest.main()
