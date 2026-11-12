"""Part D: the Live page, all work through the fleet, heartbeats and failover (docs/SPEC-live-monitor.md).
Fake runner, fake clock where time matters. Run:
uv run --with fastapi --with uvicorn --with httpx --with 'mcp<2' python -m unittest -v tests/test_live.py
"""
import asyncio
import hashlib
import hmac
import json
import secrets
import time
import unittest

from tests.test_budget import rules
from tests.test_controller import FakeRunner, W, unmetered_claude_rules, wait_for
from tests.test_limit_handoff import CLAUDE_LIMIT, FABLE, OPUS, PROFILES, SONNET, fresh_loop_state

from fastapi.testclient import TestClient

import auth
import budget
import controller
import handoff
import live
import presence
import tasks
from store import db

PUBLIC = "https://master.tail.ts.net:8701"
METRICS = {"mem_total_gb": 48.0, "mem_used_gb": 20.0, "swap_used_gb": 0.5, "disk_free_gb": 200.0, "load5": 2.0,
           "cores": 12, "ollama": {"up": True, "loaded": ["qwen3:8b"]}}


class LiveBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fresh_loop_state()
        cls.ctx = TestClient(controller.app, base_url="http://127.0.0.1")
        cls.c = cls.ctx.__enter__()
        FakeRunner.agents["codex"] = "/x/codex"
        FakeRunner.profiles = PROFILES
        FakeRunner.health_extra = {"metrics": dict(METRICS, age_s=3), "heartbeat": True}
        cls.refresh()

    @classmethod
    def refresh(cls):
        cls.c.post("/api/refresh", headers=W)
        wait_for(lambda: (tasks.node_rows() and (next(n for n in tasks.node_rows() if n["id"] == "master")["runner"]
                                                 or {}).get("profiles") == FakeRunner.profiles))

    @classmethod
    def tearDownClass(cls):
        FakeRunner.agents["codex"] = None
        FakeRunner.profiles = None
        FakeRunner.health_extra = {}
        cls.c.post("/api/refresh", headers=W)
        unmetered_claude_rules()
        cls.ctx.__exit__(None, None, None)

    def setUp(self):
        self._started_at = live.STARTED_AT
        self.addCleanup(setattr, tasks, "NODE_POLL_SECONDS", tasks.NODE_POLL_SECONDS)
        tasks.NODE_POLL_SECONDS = 30.0  # the suite polls every 0.3 s; liveness ages here are real-world ones
        self.addCleanup(setattr, live, "STARTED_AT", self._started_at)  # tests move the startup grace
        self.settle_all()
        r = self.c.put("/api/budget/rules", json={"body": rules()}, headers=W)
        self.assertEqual(r.status_code, 200, r.text)
        presence.reset()
        live.reset_state()

    def settle_all(self):
        with db() as c:
            c.execute("UPDATE tasks SET state='done' WHERE state IN ('queued','starting','working','awaiting_approval')")
            c.execute("UPDATE tasks SET resets_at=NULL WHERE failure_reason='limit'")
            c.execute("UPDATE agent_usage SET resets_at=NULL WHERE outcome='limit'")
            c.execute("DELETE FROM agent_usage WHERE ts>?", (time.time(),))  # fake-clock rows from other modules
            c.execute("DELETE FROM resume_schedule")
            c.execute("DELETE FROM activity")
            c.execute("DELETE FROM limit_resets")
            c.execute("UPDATE nodes SET heartbeat_at=NULL, heartbeat_ts=NULL")
        for job in FakeRunner.jobs.values():
            if job["state"] == "running":
                job["state"] = "cancelled"

    def ok(self, **body):
        r = self.c.post("/api/tasks", json={"prompt": "Build the greeting module", "repo": "/repo"} | body, headers=W)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def live(self, **headers):
        r = self.c.get("/api/live", headers=headers)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def get(self, task_id):
        return self.c.get(f"/api/tasks/{task_id}").json()


# ---------- §4.2 /api/live ----------

class LiveApiTests(LiveBase):
    def test_running_rows_idle_codex_row_and_no_leakage(self):
        opus = self.ok(agent="claude", model=OPUS, kind="build", gate="pytest", stage="build-1",
                       prompt="SECRET-PROMPT-TEXT build it", title="Greeting build")
        sonnet = self.ok(agent="claude", model=SONNET, kind="draft", title="Greeting draft")
        FakeRunner.jobs[opus["id"]]["result"] = "SECRET-RESULT-TEXT"
        body = self.live()
        self.assertEqual(set(body), {"orchestrator", "workers", "queue", "chains", "accounts", "self_reported",
                                     "machines", "alerts", "generated_at"})
        rows = {w["task_id"]: w for w in body["workers"] if w.get("task_id")}
        self.assertEqual(set(rows) & {opus["id"], sonnet["id"]}, {opus["id"], sonnet["id"]})
        row = rows[opus["id"]]
        self.assertEqual((row["title"], row["agent"], row["model"], row["account_label"], row["kind"], row["stage"]),
                         ("Greeting build", "claude", OPUS, "owner-max", "build", "build-1"))
        self.assertEqual(row["branch"], f"fleet/{opus['id']}")
        self.assertIsNotNone(row["started_at"])
        self.assertIn("effort", row)
        codex = [w for w in body["workers"] if w["agent"] == "codex" and w.get("task_id") is None]
        self.assertEqual(len(codex), 1)
        self.assertIn("idle_since", codex[0])
        text = json.dumps(body)
        self.assertNotIn("SECRET-PROMPT-TEXT", text)
        self.assertNotIn("SECRET-RESULT-TEXT", text)
        self.assertNotIn("/repo", text)
        self.assertNotIn('"cwd"', text)

    def test_one_profile_running_two_tasks_gives_two_rows_and_idle_rows_show_limits(self):
        a = self.ok(agent="claude", model=SONNET, kind="draft")
        b = self.ok(agent="claude", model=SONNET, kind="draft")
        rows = [w for w in self.live()["workers"] if w.get("task_id") in (a["id"], b["id"])]
        self.assertEqual(len(rows), 2)
        self.assertEqual({r["profile"] for r in rows}, {"fleet"})
        with db() as c:  # the spare profile's account is limited
            c.execute("INSERT INTO agent_usage (ts,task_id,run,agent,model,identity,outcome,resets_at,account) "
                      "VALUES (?,?,?,?,?,?,?,?,?)", (time.time(), "t-" + secrets.token_hex(4), 1, "claude", OPUS,
                                                     "claude:" + OPUS, "limit", time.time() + 3600, "claude/spare"))
            c.execute("INSERT INTO slot_flags (node,agent,profile,state,reason,at) VALUES "
                      "('master','codex','fleet','not_signed_in','x',?)", (time.time(),))
        idle = {(w["agent"], w["profile"]): w for w in self.live()["workers"] if w.get("task_id") is None}
        self.assertGreater(idle[("claude", "spare")]["limited_until"], time.time())
        self.assertFalse(idle[("codex", "fleet")]["signed_in"])
        with db() as c:
            c.execute("DELETE FROM slot_flags")

    def test_queue_reasons(self):
        body = rules()
        body["models"]["claude:claude-opus-*"]["max_concurrent"] = 1
        r = self.c.put("/api/budget/rules", json={"body": body}, headers=W)
        self.assertEqual(r.status_code, 200)
        first = self.ok(agent="claude", model=OPUS, kind="build", gate="pytest", profile="fleet")
        slot = self.ok(agent="claude", model=OPUS, kind="build", gate="pytest", profile="fleet")
        approval = self.ok(agent="claude", model=SONNET, kind="draft", mode="supervised")
        queue = {q["task_id"]: q for q in self.live()["queue"]}
        self.assertEqual(queue[slot["id"]]["wait"], "slot")
        self.assertEqual(queue[approval["id"]]["wait"], "approval")
        self.assertNotIn(first["id"], queue)
        ids = [q["task_id"] for q in self.live()["queue"]]
        self.assertLess(ids.index(slot["id"]), ids.index(approval["id"]))  # oldest first
        with db() as c:  # the spare account's Sonnet is limited: a task pinned there waits for the reset
            c.execute("INSERT INTO agent_usage (ts,task_id,run,agent,model,identity,outcome,resets_at,account) "
                      "VALUES (?,?,?,?,?,?,?,?,?)", (time.time(), "t-" + secrets.token_hex(4), 1, "claude", SONNET,
                                                     "claude:" + SONNET, "limit", time.time() + 3600, "claude/spare"))
        limited = self.ok(agent="claude", model=SONNET, kind="draft", profile="spare")
        r = self.c.put("/api/policies/node/master", json={"body": {"limits": {"max_concurrent": 1}}}, headers=W)  # the build above already runs
        self.assertEqual(r.status_code, 200, r.text)
        try:  # a real node limit holds this one back
            held = self.ok(agent="claude", model=SONNET, kind="draft")
            self.c.portal.call(tasks.try_start, tasks.get_task(held["id"]))
        finally:
            self.c.put("/api/policies/node/master", json={"body": {}}, headers=W)
        queue = {q["task_id"]: q for q in self.live()["queue"]}
        self.assertEqual(queue[limited["id"]]["wait"], "limit")
        self.assertGreater(queue[limited["id"]]["until"], time.time())
        self.assertEqual((queue[held["id"]]["wait"], queue[held["id"]]["reason"]), ("node", "max concurrent tasks reached"))

    def test_pipeline_strip_from_history(self):
        p = {"history": [{"round": 1, "state": "building"}, {"round": 1, "state": "reviewing"},
                         {"round": 1, "state": "building", "note": "fix round"}, {"round": 2, "state": "reviewing"}],
             "state": "reviewing", "round": 2, "gate": "pytest"}
        strip = live.pipeline_strip(p)
        names = [s["stage"] for s in strip]
        self.assertEqual(names[:4], ["build", "gate", "review", "fix"])
        self.assertEqual(strip[-1]["state"], "current")
        self.assertEqual(strip[-1]["stage"], "review")

    def test_view_scope_can_read_live(self):
        r = self.c.get("/api/live", headers={"X-Fleet-Scope": "view"})
        self.assertEqual(r.status_code, 200, r.text)


# ---------- §3.2 chains and stages ----------

class ChainTests(LiveBase):
    def test_stage_and_chain_validation(self):
        r = self.c.post("/api/tasks", json={"agent": "claude", "model": SONNET, "prompt": "x", "stage": "Bad Stage"},
                        headers=W)
        self.assertEqual(r.status_code, 422 if r.status_code == 422 else 400, r.text)
        root = self.ok(agent="claude", model=SONNET, kind="draft", stage="build")
        bad = self.c.put(f"/api/work/{root['id']}/chain", json={"stages": [{"stage": "UP", "agent": "claude"}]}, headers=W)
        self.assertEqual(bad.status_code, 400)
        unknown = self.c.put(f"/api/work/{root['id']}/chain", json={"stages": [{"stage": "x", "agent": "gpt"}]}, headers=W)
        self.assertEqual(unknown.status_code, 400)
        child = self.ok(agent="claude", model=SONNET, kind="draft", parent_id=root["id"], link="fix")
        not_root = self.c.put(f"/api/work/{child['id']}/chain", json={"stages": []}, headers=W)
        self.assertEqual(not_root.status_code, 400)
        view = self.c.put(f"/api/work/{root['id']}/chain", json={"stages": []},
                          headers=W | {"X-Fleet-Scope": "view"})
        self.assertEqual(view.status_code, 403)

    def test_chain_states_latest_attempt_wins_with_repeat_count(self):
        build = self.ok(agent="claude", model=SONNET, kind="draft", stage="build", title="Chain build")
        chain = [{"stage": "build", "agent": "claude"}, {"stage": "test-1", "agent": "claude", "model": OPUS},
                 {"stage": "review", "agent": "claude", "model": FABLE}, {"stage": "merge", "agent": "claude"}]
        r = self.c.put(f"/api/work/{build['id']}/chain", json={"stages": chain}, headers=W)
        self.assertEqual(r.status_code, 200, r.text)
        with db() as c:
            c.execute("UPDATE tasks SET state='review' WHERE id=?", (build["id"],))
        t1 = self.ok(agent="claude", model=SONNET, kind="draft", stage="test-1", parent_id=build["id"], link="check")
        with db() as c:
            c.execute("UPDATE tasks SET state='failed', failure_reason='error' WHERE id=?", (t1["id"],))
        t2 = self.ok(agent="claude", model=SONNET, kind="draft", stage="test-1", parent_id=build["id"], link="check")
        rev = self.ok(agent="claude", model=SONNET, kind="draft", stage="review", parent_id=build["id"], link="check")
        with db() as c:
            c.execute("UPDATE tasks SET state='failed', failure_reason='error', resolution=? WHERE id=?",
                      (json.dumps({"kind": "dismissed"}), rev["id"]))
        found = next(ch for ch in self.live()["chains"] if ch.get("root_id") == build["id"])
        states = {s["stage"]: s for s in found["stages"]}
        self.assertEqual(states["build"]["state"], "done")
        self.assertEqual((states["test-1"]["state"], states["test-1"]["count"], states["test-1"]["task_id"]),
                         ("current", 2, t2["id"]))
        self.assertEqual(states["review"]["state"], "superseded")
        self.assertEqual(states["merge"]["state"], "next")
        with db() as c:
            c.execute("UPDATE tasks SET state='failed', failure_reason='error' WHERE id=?", (t2["id"],))
        found = next(ch for ch in self.live()["chains"] if ch.get("root_id") == build["id"])
        self.assertEqual({s["stage"]: s["state"] for s in found["stages"]}["test-1"], "failed")


# ---------- §2.3 self-reported activity ----------

class ActivityTests(LiveBase):
    def post(self, scope="operate", **body):
        payload = {"title": "Subagent: read the spec", "kind": "review", "account_label": "spare", "model": OPUS,
                   "status": "running"} | body
        return self.c.post("/api/activity", json=payload, headers=W | {"X-Fleet-Scope": scope})

    def test_scope_label_validation_redaction_cap(self):
        self.assertEqual(self.post(scope="view").status_code, 403)
        self.assertEqual(self.post(account_label="nobody").status_code, 400)
        self.assertEqual(self.post(kind="nonsense").status_code, 400)
        r = self.post(note="token=abcd1234secret " + "x" * 600, tokens={"input": 10, "output": 5, "cached": 100})
        self.assertEqual(r.status_code, 200, r.text)
        rec = r.json()
        self.assertNotIn("abcd1234secret", rec["note"])
        self.assertLessEqual(len(rec["note"]), 300)
        shown = self.live()["self_reported"]
        self.assertEqual(shown[0]["id"], rec["id"])
        self.assertTrue(shown[0]["self_reported"])
        done = self.c.post("/api/activity", json={"id": rec["id"], "status": "done"}, headers=W | {"X-Fleet-Scope": "operate"})
        self.assertEqual(done.json()["status"], "done")
        self.assertIsNotNone(done.json()["ended_at"])

    def test_tokens_with_the_same_label_are_separate_actors(self):
        # Codex t-960c3797 (low): labels aren't unique; rate limits and ownership key on the token id.
        remote = TestClient(controller.app, base_url=PUBLIC)
        first, second = auth.create_token("operate", "twin"), auth.create_token("operate", "twin")
        body = {"title": "x", "kind": "subagent", "account_label": "spare", "model": OPUS, "status": "running"}
        rec = remote.post("/api/activity", json=body, headers=W | {"Authorization": f"Bearer {first}"}).json()
        other = remote.post("/api/activity", json={"id": rec["id"], "status": "done"},
                            headers=W | {"Authorization": f"Bearer {second}"})
        self.assertEqual(other.status_code, 404)
        mine = remote.post("/api/activity", json={"id": rec["id"], "status": "done"},
                           headers=W | {"Authorization": f"Bearer {first}"})
        self.assertEqual(mine.status_code, 200, mine.text)

    def test_rate_limit_per_actor(self):
        for _ in range(60):
            self.assertEqual(self.post(note="n").status_code, 200)
        self.assertEqual(self.post(note="n").status_code, 429)
        live.reset_state()

    def test_never_changes_enforcement_and_tokens_counted_separately(self):
        before = budget.limit_snapshot(budget.current_rules(), time.time(), "claude/spare")
        self.post(tokens={"input": 7, "output": 3, "cached": 1}, status="failed", note="usage limit reached")
        after = budget.limit_snapshot(budget.current_rules(), time.time(), "claude/spare")
        self.assertEqual(before["models"], after["models"])
        account = next(a for a in self.live()["accounts"] if a["label"] == "spare" and a["agent"] == "claude")
        self.assertEqual(account["self_reported_tokens"]["input"], 7)

    def test_pruned_after_30_days(self):
        rec = self.post().json()
        with db() as c:
            c.execute("UPDATE activity SET updated_at=?, ts=? WHERE id=?",
                      (time.time() - 31 * 86400, time.time() - 31 * 86400, rec["id"]))
        self.c.portal.call(controller.retention_once)
        with db() as c:
            self.assertIsNone(c.execute("SELECT 1 FROM activity WHERE id=?", (rec["id"],)).fetchone())


# ---------- §2.4 presence ----------

class PresenceTests(LiveBase):
    H = {"X-Fleet-Session": "orch-1", "X-Fleet-Model": "claude-opus-5-5", "X-Fleet-Account": "spare"}

    def test_presence_from_headers_only_local_or_operate(self):
        self.c.get("/api/fleet", headers=self.H | {"X-Fleet-Scope": "view"})
        self.assertEqual(self.live()["orchestrator"], [])
        self.c.get("/api/fleet", headers=self.H)
        [s] = self.live()["orchestrator"]
        self.assertEqual((s["session"], s["model"], s["account_label"]), ("orch-1", "claude-opus-5-5", "spare"))
        self.assertFalse(s["stale"])
        self.c.get("/api/fleet", headers={"X-Fleet-Session": "bad value!", "X-Fleet-Account": "x"})
        self.assertEqual(len(self.live()["orchestrator"]), 1)
        self.c.get("/api/fleet", headers={"X-Fleet-Session": "orch-2", "X-Fleet-Account": "nobody"})
        two = {s["session"]: s for s in self.live()["orchestrator"]}
        self.assertIsNone(two["orch-2"]["account_label"])

    def test_at_most_20_sessions_and_stale_after_15_minutes(self):
        for i in range(25):
            self.c.get("/api/fleet", headers={"X-Fleet-Session": f"s-{i}"})
        sessions = self.live()["orchestrator"]
        self.assertEqual(len(sessions), 20)
        self.assertNotIn("s-0", {s["session"] for s in sessions})
        presence.SESSIONS["s-24"]["last_seen"] -= 16 * 60
        self.assertTrue(next(s for s in self.live()["orchestrator"] if s["session"] == "s-24")["stale"])

    def test_open_wait_is_shown_then_the_last_wait(self):
        t = self.ok(agent="claude", model=SONNET, kind="draft")

        async def waiting():
            return await asyncio.to_thread(
                lambda: self.c.get(f"/api/tasks/wait?ids={t['id']}&timeout=2", headers=self.H).json())

        fut = self.c.portal.start_task_soon(waiting)
        wait_for(lambda: presence.WAITS.get("orch-1"))
        [s] = self.live()["orchestrator"]
        self.assertEqual(s["waiting_on"], [t["id"]])
        fut.result(timeout=10)
        [s] = self.live()["orchestrator"]
        self.assertIsNone(s["waiting_on"])
        self.assertEqual(s["last_wait"]["ids"], [t["id"]])


# ---------- §4.3 accounts, budget.limit event ----------

class AccountTests(LiveBase):
    def limited_task(self, model=OPUS, profile="fleet"):
        t = self.ok(agent="claude", model=model, kind="review", profile=profile)
        FakeRunner.jobs[t["id"]].update(state="exited", exit_code=1, result=CLAUDE_LIMIT)
        self.c.portal.call(tasks.poll_task, t["id"])
        return self.get(t["id"])

    def test_limit_event_usage_label_and_account_room(self):
        t = self.limited_task()
        with db() as c:
            row = c.execute("SELECT account_label FROM agent_usage WHERE task_id=?", (t["id"],)).fetchone()
            event = c.execute("SELECT data FROM events WHERE type='budget.limit' AND task_id=?", (t["id"],)).fetchone()
        self.assertEqual(row["account_label"], "owner-max")
        self.assertIsNotNone(event)
        ok = self.ok(agent="claude", model=SONNET, kind="draft", profile="fleet")
        FakeRunner.jobs[ok["id"]].update(state="exited", exit_code=0, result="done", usage={"input": 11, "output": 4, "cached": 2})
        self.c.portal.call(tasks.poll_task, ok["id"])
        account = next(a for a in self.live()["accounts"] if a["label"] == "owner-max" and a["agent"] == "claude")
        self.assertIsNotNone(account["last_limit"]["at"])
        self.assertIn("limited until", account["status"])  # names the model when the limit is on one model
        self.assertGreaterEqual(account["tokens_since"]["input"] or 0, 0)
        self.assertIn("running", account)


# ---------- §2.1 profile pinning, §6 independence ----------

class PinningTests(LiveBase):
    def test_profile_pinning(self):
        t = self.ok(agent="claude", model=SONNET, kind="draft", profile="spare")
        self.assertEqual((t["profile"], t["account_label"]), ("spare", "spare"))
        self.assertEqual([b for p, b in FakeRunner.calls if p == "/jobs" and b["id"] == t["id"]][-1]["profile"], "spare")
        r = self.c.post("/api/tasks", json={"agent": "claude", "model": SONNET, "prompt": "x", "profile": "nope"}, headers=W)
        self.assertEqual(r.status_code, 400)
        with db() as c:
            c.execute("INSERT INTO slot_flags (node,agent,profile,state,reason,at) VALUES "
                      "('master','claude','spare','not_signed_in','x',?)", (time.time(),))
        r = self.c.post("/api/tasks", json={"agent": "claude", "model": SONNET, "prompt": "x", "profile": "spare"}, headers=W)
        self.assertEqual(r.status_code, 400)
        with db() as c:
            c.execute("DELETE FROM slot_flags")
            c.execute("INSERT INTO agent_usage (ts,task_id,run,agent,model,identity,outcome,resets_at,account) "
                      "VALUES (?,?,?,?,?,?,?,?,?)", (time.time(), "t-" + secrets.token_hex(4), 1, "claude", SONNET,
                                                     "claude:" + SONNET, "limit", time.time() + 3600, "claude/spare"))
        queued = self.ok(agent="claude", model=SONNET, kind="draft", profile="spare")
        self.assertEqual((queued["state"], queued["budget_wait"], queued["profile"]), ("queued", "limit", "spare"))
        body = rules()
        body["limits"]["on_limit"] = "refuse"
        self.c.put("/api/budget/rules", json={"body": body}, headers=W)
        r = self.c.post("/api/tasks", json={"agent": "claude", "model": SONNET, "prompt": "x", "profile": "spare"}, headers=W)
        self.assertEqual(r.status_code, 409)

    def test_a_named_profile_needs_an_explicit_model(self):
        # Codex t-960c3797 (high): a named profile has its own config dir, so the runner's reported default
        # (the fleet profile's) may not be what it runs.
        r = self.c.post("/api/tasks", json={"agent": "claude", "prompt": "x", "kind": "draft", "profile": "spare"}, headers=W)
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn("model", r.json()["detail"])
        self.ok(agent="claude", model=SONNET, kind="draft", profile="spare")  # with a model it runs there

    def test_a_model_less_paid_task_stays_on_the_fleet_profile(self):
        # Codex t-dd1f0248 (high): admission accounts a model-less task at the fleet default, so a named profile
        # must never run it.
        try:
            FakeRunner.profiles = {"claude": [{"id": "spare", "label": "spare", "ready": True}],
                                   "codex": [{"id": "fleet", "label": "owner-plus", "ready": True}]}
            self.refresh()
            r = self.c.post("/api/tasks", json={"agent": "claude", "prompt": "x", "kind": "draft"}, headers=W)
            self.assertEqual(r.status_code, 400, r.text)
            self.assertIn("model", r.json()["detail"])
            FakeRunner.profiles = {"claude": [{"id": "spare", "label": "spare", "ready": True},
                                              {"id": "fleet", "label": "owner-max", "ready": True}],
                                   "codex": [{"id": "fleet", "label": "owner-plus", "ready": True}]}
            self.refresh()
            t = self.ok(agent="claude", kind="draft")
            self.assertEqual((t["profile"], t["account_label"]), ("fleet", "owner-max"))
        finally:
            FakeRunner.profiles = PROFILES
            self.refresh()

    def test_independence_with_no_model_on_a_named_profile_is_unresolvable(self):
        build = self.ok(agent="claude", model=SONNET, kind="draft")
        node = next(n for n in tasks.node_rows() if n["id"] == "master")
        t = tasks.TaskIn(agent="claude", prompt="review", kind="review", parent_id=build["id"])
        original = budget.node_default_model
        budget.node_default_model = lambda agent, node_id=None: "claude-fable-5-2"
        try:
            self.assertIsNone(tasks.check_independence(t, "review", "claude", None, node, False, profile="fleet"))
            with self.assertRaises(tasks.HTTPException) as caught:
                tasks.check_independence(t, "review", "claude", None, node, False, profile="spare")
            self.assertIn("can't tell which", caught.exception.detail)
        finally:
            budget.node_default_model = original

    def test_test_kind_exists_and_fable_may_not_test(self):
        self.assertIn("test", budget.KINDS)
        self.assertIn("test", budget.DEFAULT_RULES["models"]["claude:claude-opus-*"]["kinds"])
        self.assertNotIn("test", budget.DEFAULT_RULES["models"]["claude:claude-fable-*"]["kinds"])

    def test_a_test_task_is_not_a_builder_in_the_work_item(self):
        body = rules()
        body["models"]["claude:claude-opus-*"]["kinds"].append("test")
        self.c.put("/api/budget/rules", json={"body": body}, headers=W)
        build = self.ok(agent="claude", model=OPUS, kind="build", gate="pytest")
        self.ok(agent="claude", model=SONNET, kind="test", parent_id=build["id"], mode="read-only",
                worktree=False, independence="waived", independence_reason="fixture")
        self.assertEqual(handoff.build_attempts(build["id"]), [("claude", OPUS)])

    def test_adhoc_independence_refused_unless_waived(self):
        body = rules()
        body["models"]["claude:claude-opus-*"]["kinds"].append("test")
        self.c.put("/api/budget/rules", json={"body": body}, headers=W)
        build = self.ok(agent="claude", model=OPUS, kind="build", gate="pytest")
        with db() as c:
            before = c.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
            decisions = c.execute("SELECT COUNT(*) FROM events WHERE type LIKE 'budget.%'").fetchone()[0]
        r = self.c.post("/api/tasks", json={"agent": "claude", "model": OPUS, "prompt": "test it", "kind": "test",
                                            "parent_id": build["id"], "link": "check", "mode": "read-only",
                                            "worktree": False}, headers=W)
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn("independ", r.text)
        with db() as c:  # a refused task leaves no trace (Fable's builder note b)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], before)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM events WHERE type LIKE 'budget.%'").fetchone()[0], decisions)
        no_reason = self.c.post("/api/tasks", json={"agent": "claude", "model": OPUS, "prompt": "test", "kind": "test",
                                                    "parent_id": build["id"], "independence": "waived"}, headers=W)
        self.assertEqual(no_reason.status_code, 400)
        waived = self.ok(agent="claude", model=OPUS, prompt="test it", kind="test", parent_id=build["id"], link="check",
                         mode="read-only", worktree=False, independence="waived",
                         independence_reason="owner chose Opus as tester")
        self.assertEqual(waived["independence"], {"waived": True, "reason": "owner chose Opus as tester"})
        with db() as c:
            self.assertIsNotNone(c.execute("SELECT 1 FROM events WHERE type='task.independence_waived' AND task_id=?",
                                           (waived["id"],)).fetchone())
        row = next(w for w in self.live()["workers"] if w.get("task_id") == waived["id"])
        self.assertEqual(row["independence"], "waived")
        secret = "sk-ant-" + "a1b2c3d4e5f6g7h8i9j0k1l2m3n4"
        leaky = self.ok(agent="claude", model=OPUS, prompt="test it", kind="test", parent_id=build["id"], link="check",
                        mode="read-only", worktree=False, independence="waived",
                        independence_reason=f"key {secret} was the reason")
        self.assertNotIn(secret, json.dumps(leaky["independence"]))
        with db() as c:
            events = " ".join(r[0] or "" for r in c.execute("SELECT data FROM events WHERE task_id=?", (leaky["id"],)))
        self.assertNotIn(secret, events)
        other = self.ok(agent="claude", model=SONNET, prompt="review", kind="review", parent_id=build["id"],
                        link="review", mode="read-only", worktree=False)
        self.assertNotIn(other["state"], ("failed",))


# ---------- §13 ----------

class OwnerRequirementTests(LiveBase):
    def paused(self, model=OPUS):
        t = self.ok(agent="claude", model=model, kind="review", profile="fleet")
        FakeRunner.jobs[t["id"]].update(state="exited", exit_code=1, result=CLAUDE_LIMIT)
        self.c.portal.call(tasks.poll_task, t["id"])
        self.c.portal.call(handoff.after_settled, t["id"])
        return self.get(t["id"])

    def usage(self, model, outcome, resets_at=None, account="claude/owner-max"):
        time.sleep(0.01)
        with db() as c:
            c.execute("INSERT INTO agent_usage (ts,task_id,run,agent,model,identity,outcome,resets_at,account) "
                      "VALUES (?,?,?,?,?,?,?,?,?)", (time.time(), "t-" + secrets.token_hex(4), 1, "claude", model,
                                                     "claude:" + model, outcome, resets_at, account))

    def test_limit_state_comes_from_the_account(self):
        t = self.paused()
        self.assertEqual(t["display"]["tone"], "paused")
        other = self.ok(agent="claude", model=SONNET, kind="draft", profile="fleet")  # a different identity succeeds
        FakeRunner.jobs[other["id"]].update(state="exited", exit_code=0, result="ok")
        self.c.portal.call(tasks.poll_task, other["id"])
        self.assertEqual(self.get(t["id"])["display"]["tone"], "paused")
        self.usage(OPUS, "ok")  # the same identity succeeds on the same account
        shown = self.get(t["id"])["display"]
        self.assertEqual(shown, {"tone": "attention", "label": "Limit cleared: resume or hand off"})

    def test_owner_reset_is_admin_only_and_clears_enforcement(self):
        t = self.paused()
        remote = TestClient(controller.app, base_url=PUBLIC)
        operate = auth.create_token("operate", "reset-op")
        admin = auth.create_token("admin", "reset-admin")
        body = {"account": "claude/owner-max"}
        r = remote.post("/api/budget/accounts/reset", json=body, headers=W | {"Authorization": f"Bearer {operate}"})
        self.assertEqual(r.status_code, 403)
        r = remote.post("/api/budget/accounts/reset", json={"account": "claude/nobody"},
                        headers=W | {"Authorization": f"Bearer {admin}"})
        self.assertEqual(r.status_code, 404)
        queued = self.ok(agent="claude", model=OPUS, kind="review", profile="fleet")
        self.assertEqual(queued["budget_wait"], "limit")
        r = remote.post("/api/budget/accounts/reset", json=body, headers=W | {"Authorization": f"Bearer {admin}"})
        self.assertEqual(r.status_code, 200, r.text)
        with db() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM limit_resets WHERE account=?", (body["account"],)).fetchone()[0], 1)
            self.assertIsNotNone(c.execute("SELECT 1 FROM events WHERE type='budget.limit_reset'").fetchone())
        self.assertEqual(self.get(t["id"])["display"]["label"], "Limit cleared: resume or hand off")
        self.c.portal.call(tasks.recover)
        self.assertEqual(self.get(queued["id"])["state"], "working")
        account = next(a for a in self.live()["accounts"] if a["account"] == "claude/owner-max")
        self.assertTrue(account["status"].startswith("reset by owner"), account)
        self.usage(OPUS, "limit", resets_at=time.time() + 3600)  # a newer limit limits the account again
        snap = budget.limit_snapshot(budget.current_rules(), time.time(), "claude/owner-max")
        self.assertIn("claude:" + OPUS, snap["models"])

    def test_retry_and_handoff_are_shown_on_the_source(self):
        src = self.ok(agent="claude", model=SONNET, kind="draft")
        with db() as c:
            c.execute("UPDATE tasks SET state='failed', failure_reason='error' WHERE id=?", (src["id"],))
        first = self.ok(agent="claude", model=SONNET, kind="draft", parent_id=src["id"], link="retry")
        time.sleep(0.01)
        newest = self.ok(agent="claude", model=SONNET, kind="draft", parent_id=src["id"], link="retry")
        self.assertEqual(self.get(src["id"])["display"]["label"], f"Retried as {newest['id']}")
        self.assertNotEqual(first["id"], newest["id"])


# ---------- §12 heartbeats, liveness, warm models, leases, failover ----------

def sign(token: str, node: str, ts: float, body: bytes) -> str:
    message = f"{node}\n{int(ts)}\n{hashlib.sha256(body).hexdigest()}".encode()
    return hmac.new(token.encode(), message, hashlib.sha256).hexdigest()


class HeartbeatTests(LiveBase):
    def setUp(self):
        super().setUp()
        self.token = tasks.node_runner_token("master")
        self.assertTrue(self.token)

    def beat(self, client=None, node="master", ts=None, token=None, body=None, sig=None):
        client = client or self.c
        ts = time.time() if ts is None else ts
        raw = json.dumps(body if body is not None else {"ok": True, "version": "2", "agents": {"claude": "/x/claude"},
                                                          "metrics": dict(METRICS, age_s=1), "junk": "dropped"}).encode()
        headers = {"Content-Type": "application/json", "X-Fleet-Node": node, "X-Fleet-Ts": str(int(ts)),
                   "X-Fleet-Sig": sig or sign(token or self.token, node, ts, raw)}
        return client.post("/api/nodes/heartbeat", content=raw, headers=headers)

    def test_signed_heartbeat_is_accepted_without_a_user_token_on_the_public_port(self):
        remote = TestClient(controller.app, base_url=PUBLIC)
        r = self.beat(client=remote)
        self.assertEqual(r.status_code, 200, r.text)
        node = next(n for n in tasks.node_rows() if n["id"] == "master")
        self.assertIsNotNone(node["heartbeat_at"])
        self.assertNotIn("junk", node["runner"])
        self.assertEqual(node["runner"]["metrics"]["ollama"]["loaded"], ["qwen3:8b"])
        # the signature is worth nothing on any other path
        other = remote.get("/api/fleet", headers={"X-Fleet-Node": "master", "X-Fleet-Sig": "x"})
        self.assertEqual(other.status_code, 403)

    def test_every_refusal_is_the_same_401(self):
        now = time.time()
        cases = [self.beat(sig="0" * 64), self.beat(ts=now - 120), self.beat(node="ghost"),
                 self.beat(token="wrong-token")]
        with db() as c:
            c.execute("INSERT OR IGNORE INTO nodes (id, name, status, runner_token) VALUES ('pend', 'pend', 'pending', 'tok')")
        cases.append(self.beat(node="pend", token="tok"))
        for r in cases:
            self.assertEqual((r.status_code, r.json()), (401, {"detail": "heartbeat refused"}))

    def test_replay_size_and_rate_limits(self):
        ts = time.time()
        self.assertEqual(self.beat(ts=ts).status_code, 200)
        self.assertEqual(self.beat(ts=ts).status_code, 401)  # same ts: a replay
        self.assertEqual(self.beat(ts=ts + 1).status_code, 429)  # under 2 s since the last accepted post
        self.assertEqual(self.beat(ts=ts + 1).status_code, 401)  # ...but its ts advanced: no replay later
        big = {"ok": True, "pad": "x" * (65 * 1024)}
        self.assertEqual(self.beat(body=big, ts=ts + 3).status_code, 413)

    def test_a_chunked_body_is_cut_off_at_the_cap_whatever_content_length_says(self):
        # Codex t-960c3797 (medium): no Content-Length (chunked) must not let a huge body be buffered.
        def body():
            for _ in range(80):
                yield b"x" * 1024

        r = self.c.post("/api/nodes/heartbeat", content=body(),
                        headers={"X-Fleet-Node": "master", "X-Fleet-Ts": str(int(time.time())), "X-Fleet-Sig": "0" * 64})
        self.assertEqual(r.status_code, 413)
        lying = self.c.post("/api/nodes/heartbeat", content=b"x" * (70 * 1024),
                            headers={"Content-Length": "10", "X-Fleet-Node": "master", "X-Fleet-Ts": "1",
                                     "X-Fleet-Sig": "0" * 64})
        self.assertIn(lying.status_code, (400, 413, 401))
        self.assertNotEqual(lying.status_code, 200)

    def _stream_request(self, chunks, pulled):
        async def stream():
            for chunk in chunks:
                pulled.append(len(chunk))
                yield chunk

        headers = {"x-fleet-node": "master", "x-fleet-ts": str(int(time.time())), "x-fleet-sig": "0" * 64}
        return type("Req", (), {"headers": headers, "client": type("C", (), {"host": "testclient"})(), "stream": staticmethod(stream)})()

    def test_one_huge_chunk_is_refused_without_being_buffered(self):
        pulled, held = [], []

        class Spy(bytearray):
            def __iadd__(self, other):
                held.append(len(other))
                return super().__iadd__(other)

        big = self._stream_request([b"x" * (50 * 1024 * 1024), b"never pulled"], pulled)
        live.bytearray = Spy  # the handler's buffer: records every extension
        try:
            r = self.c.portal.call(live.heartbeat, big)
        finally:
            del live.bytearray
        self.assertEqual(r.status_code, 413)
        self.assertEqual(held, [])  # the oversized chunk was never appended
        self.assertEqual(pulled, [50 * 1024 * 1024])  # stopped at the first chunk, before extending the buffer

    def test_chunks_that_cross_the_cap_are_refused_at_the_chunk_that_does(self):
        pulled = []
        half = live.HEARTBEAT_MAX_BYTES // 2
        req = self._stream_request([b"x" * half, b"x" * half, b"x", b"never pulled"], pulled)
        r = self.c.portal.call(live.heartbeat, req)
        self.assertEqual(r.status_code, 413)
        self.assertEqual(pulled, [half, half, 1])

    def test_a_limited_source_sending_junk_headers_is_cut_off_before_the_body_is_read(self):
        for _ in range(30):
            self.beat(sig="0" * 64)
        pulled = []
        req = self._stream_request([b"x" * 100], pulled)
        req.headers["x-fleet-sig"] = "nothex"
        r = self.c.portal.call(live.heartbeat, req)
        self.assertEqual(r.status_code, 429)
        self.assertEqual(pulled, [])  # no body read, no DB lookup
        self.assertEqual(self.beat().status_code, 200)  # a validly signed post still passes

    def test_the_sweep_prunes_idle_failure_buckets(self):
        live.HEARTBEAT_FAILURES["203.0.113.9"].append(time.time() - 600)
        live.HEARTBEAT_FAILURES["203.0.113.10"]  # an empty bucket from a lookup
        live.HEARTBEAT_FAILURES["203.0.113.11"].append(time.time())
        self.c.portal.call(live.sweep_once)
        self.assertEqual(set(live.HEARTBEAT_FAILURES), {"203.0.113.11"})

    def test_bad_posts_never_use_up_the_nodes_own_allowance(self):
        for _ in range(5):
            self.beat(sig="0" * 64)
        self.assertEqual(self.beat().status_code, 200)

    def test_failed_attempts_are_limited_per_source_but_signed_posts_still_pass(self):
        for _ in range(30):
            self.beat(sig="0" * 64)
        self.assertEqual(self.beat(sig="0" * 64).status_code, 429)
        self.assertEqual(self.beat().status_code, 200)

    def test_heartbeat_never_resets_runner_fails(self):
        with db() as c:
            c.execute("UPDATE nodes SET runner_fails=3 WHERE id='master'")
        try:
            self.assertEqual(self.beat().status_code, 200)
            node = next(n for n in tasks.node_rows() if n["id"] == "master")
            self.assertEqual(node["runner_fails"], 3)
            self.assertTrue(tasks.runner_offline(node))
        finally:
            with db() as c:
                c.execute("UPDATE nodes SET runner_fails=0 WHERE id='master'")

    def test_the_poll_tells_the_runner_its_node_id(self):
        FakeRunner.node_ids.clear()
        self.refresh()
        self.assertIn("master", FakeRunner.node_ids)


class LivenessTests(unittest.TestCase):
    """runner_offline is pure: one rule, a fake clock."""
    def setUp(self):
        self.addCleanup(setattr, tasks, "NODE_POLL_SECONDS", tasks.NODE_POLL_SECONDS)
        tasks.NODE_POLL_SECONDS = 30.0

    def node(self, **kw):
        return {"status": "approved", "runner": {"ok": True}, "runner_fails": 0, "heartbeat_at": None,
                "polled_at": None} | kw

    def test_rules(self):
        now = 10_000.0
        def off(node, at, grace=False):  # a fake clock: the startup grace is tested on its own below
            return tasks.runner_offline(node, at, grace)
        self.assertIsNone(off(self.node(polled_at=now - 5), now))
        self.assertTrue(off(self.node(runner_fails=3), now))
        self.assertTrue(off(self.node(runner=None), now))
        # no accepted heartbeat: the poll rule only, whatever the age
        self.assertIsNone(off(self.node(polled_at=now - 500), now))
        # push broken but pollable: online
        self.assertIsNone(off(self.node(heartbeat_at=now - 300, polled_at=now - 2), now))
        # dead: both stale
        self.assertTrue(off(self.node(heartbeat_at=now - 26, polled_at=now - 66), now))
        self.assertIsNone(off(self.node(heartbeat_at=now - 24, polled_at=now - 66), now))
        # a poll older than one interval but inside two + 5 s is a healthy tick that ran long: still online
        for age in (36, 45, 65):
            self.assertIsNone(off(self.node(heartbeat_at=now - 300, polled_at=now - age), now), age)
        # a NULL polled_at counts as stale
        self.assertTrue(off(self.node(heartbeat_at=now - 26), now))
        # runner_fails >= 3 with fresh heartbeats stays offline
        self.assertTrue(off(self.node(heartbeat_at=now - 1, runner_fails=3), now))
        # the startup grace switches the expiry rule off
        self.assertIsNone(off(self.node(heartbeat_at=now - 26, polled_at=now - 66), now, grace=True))


class AlertTests(LiveBase):
    """The alerts strip lists only what still needs attention now."""

    def task(self, state, **cols):
        tid = "t" + secrets.token_hex(4)
        row = {"id": tid, "title": "Fable final review", "agent": "claude", "state": state,
               "created_at": time.time(), "updated_at": time.time()} | cols
        with db() as c:
            c.execute(f"INSERT INTO tasks ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})", tuple(row.values()))
        return tid

    def event(self, task_id, kind, **data):
        with db() as c:
            c.execute("INSERT INTO events (ts, task_id, type, data) VALUES (?,?,?,?)",
                      (time.time(), task_id, kind, json.dumps(data | {"title": "Fable final review"})))

    def kinds(self, task_id=None):
        return [a["id"] for a in self.live()["alerts"] if a["task_id"] == task_id and task_id]

    def test_a_resolved_failed_task_is_gone(self):
        tid = self.task("failed", resolution=json.dumps({"kind": "done_elsewhere"}))
        self.event(tid, "task.failed")
        self.assertEqual(self.kinds(tid), [])

    def test_a_task_with_an_outcome_is_gone(self):
        tid = self.task("failed", outcome="dismissed")
        self.event(tid, "task.failed")
        self.assertEqual(self.kinds(tid), [])

    def test_a_done_tasks_blocked_event_is_gone(self):
        tid = self.task("done")
        self.event(tid, "task.blocked")
        self.assertEqual(self.kinds(tid), [])

    def test_an_unresolved_failure_stays(self):
        tid = self.task("failed")
        self.event(tid, "task.failed")
        self.assertEqual(len(self.kinds(tid)), 1)

    def test_a_later_success_in_the_work_item_clears_it(self):
        tid = self.task("failed")
        self.event(tid, "task.failed")
        self.task("done", parent_id=tid, created_at=time.time() + 1)
        self.assertEqual(self.kinds(tid), [])

    def test_a_cleared_limit_is_gone_and_a_live_one_stays(self):
        account = "claude:test-acct"
        tid = self.task("done")
        self.event(tid, "budget.limit", agent="claude", account=account)
        self.assertNotIn("budget.limit", [a["type"] for a in self.live()["alerts"]])
        with db() as c:
            c.execute("INSERT INTO agent_usage (task_id, ts, agent, model, identity, outcome, resets_at, account) "
                      "VALUES (?,?,?,?,?,?,?,?)", (tid, time.time(), "claude", None, "claude", "limit",
                                                   time.time() + 3600, account))
        self.assertIn("budget.limit", [a["type"] for a in self.live()["alerts"]])
        with db() as c:
            c.execute("INSERT INTO limit_resets (account, at, actor) VALUES (?,?,?)", (account, time.time() + 1, "t"))
        self.assertNotIn("budget.limit", [a["type"] for a in self.live()["alerts"]])

    def test_duplicates_for_one_task_collapse_to_the_latest(self):
        tid = self.task("blocked")
        self.event(tid, "task.failed")
        self.event(tid, "task.blocked")
        self.event(tid, "task.blocked")
        alerts = [a for a in self.live()["alerts"] if a["task_id"] == tid]
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["type"], "task.blocked")

    def test_the_cap_applies_after_filtering(self):
        for _ in range(12):
            self.event(self.task("done"), "task.failed")
        keep = [self.task("failed") for _ in range(3)]
        for tid in keep:
            self.event(tid, "task.failed")
        for _ in range(12):
            self.event(self.task("done"), "task.failed")
        self.assertTrue(set(keep) <= {a["task_id"] for a in self.live()["alerts"]})


class SweepTests(LiveBase):
    def node_ages(self, heartbeat_age, poll_age):
        now = time.time()
        with db() as c:
            c.execute("UPDATE nodes SET heartbeat_at=?, polled_at=? WHERE id='master'",
                      (now - heartbeat_age, now - poll_age))

    def events(self, kind):
        with db() as c:
            return c.execute("SELECT COUNT(*) FROM events WHERE type=?", (kind,)).fetchone()[0]

    def test_offline_and_online_events_once_and_startup_grace(self):
        live.STARTED_AT = time.time()  # within the grace: seeds only
        self.node_ages(90, 90)
        before = self.events("node.runner_offline")
        self.c.portal.call(live.sweep_once)
        self.assertEqual(self.events("node.runner_offline"), before)
        live.STARTED_AT = time.time() - 120
        self.c.portal.call(live.sweep_once)
        self.c.portal.call(live.sweep_once)
        self.assertEqual(self.events("node.runner_offline"), before + 1)
        alerts = [a["type"] for a in self.live()["alerts"]]
        self.assertIn("node.runner_offline", alerts)
        on = self.events("node.runner_online")
        self.node_ages(1, 1)
        self.c.portal.call(live.sweep_once)
        self.c.portal.call(live.sweep_once)
        self.assertEqual(self.events("node.runner_online"), on + 1)

    def test_a_node_that_still_answers_polls_does_not_flap_offline(self):
        # Fable (medium): the poll loop is tick + 30 s sleep, so a good poll can be 36-45 s old.
        live.STARTED_AT = time.time() - 120
        before = self.events("node.runner_offline")
        for poll_age in (36, 45):
            self.node_ages(120, poll_age)  # pushes lost, polls still succeeding
            self.c.portal.call(live.sweep_once)
            self.assertIsNone(tasks.runner_offline(next(n for n in tasks.node_rows() if n["id"] == "master")))
        self.assertEqual(self.events("node.runner_offline"), before)
        self.c.post("/api/refresh", headers=W)  # a successful poll follows
        wait_for(lambda: next(n for n in tasks.node_rows() if n["id"] == "master")["polled_at"] > time.time() - 5)
        self.c.portal.call(live.sweep_once)
        self.assertEqual(self.events("node.runner_offline"), before)

    def test_unknown_metrics_raise_and_clear_no_warning_events(self):
        live.STARTED_AT = time.time() - 120
        FakeRunner.health_extra = {"metrics": dict(METRICS, swap_used_gb=6.0, age_s=2), "heartbeat": True}
        self.refresh_metrics()
        try:
            self.c.portal.call(live.sweep_once)  # active swap warning
            started = self.events("node.swap_high")
            with db() as c:  # the report goes stale: the machine reads as unknown, which is not "cleared"
                c.execute("UPDATE nodes SET polled_at=?, heartbeat_at=NULL WHERE id='master'", (time.time() - 600,))
            machine = live.machine_row(next(n for n in tasks.node_rows() if n["id"] == "master"), time.time())
            self.assertTrue(machine["unknown"])
            self.c.portal.call(live.sweep_once)
            self.assertEqual(self.events("node.swap_high"), started)
        finally:
            FakeRunner.health_extra = {"metrics": dict(METRICS, age_s=3), "heartbeat": True}
            self.refresh_metrics()

    def test_machine_warnings_and_deduplicated_node_events(self):
        live.STARTED_AT = time.time() - 120
        FakeRunner.health_extra = {"metrics": dict(METRICS, swap_used_gb=6.0, disk_free_gb=5.0, load5=30.0, age_s=2),
                                   "heartbeat": True}
        self.refresh_metrics()
        try:
            machine = next(m for m in self.live()["machines"] if m["node"] == "master")
            self.assertEqual(set(machine["warnings"]), {"swap", "disk", "load"})
            before = self.events("node.swap_high")
            self.c.portal.call(live.sweep_once)
            self.c.portal.call(live.sweep_once)
            self.assertEqual(self.events("node.swap_high"), before + 1)
        finally:
            FakeRunner.health_extra = {"metrics": dict(METRICS, age_s=3), "heartbeat": True}
            self.refresh_metrics()

    def refresh_metrics(self):
        want = FakeRunner.health_extra["metrics"]["swap_used_gb"]
        self.c.post("/api/refresh", headers=W)
        wait_for(lambda: ((next(n for n in tasks.node_rows() if n["id"] == "master")["runner"] or {})
                          .get("metrics") or {}).get("swap_used_gb") == want)

    def test_stale_metrics_show_unknown(self):
        node = {"id": "x", "name": "x", "runner": {"metrics": dict(METRICS, swap_used_gb=9.0, age_s=90)},
                "polled_at": time.time(), "heartbeat_at": None, "runner_age_s": 90}
        machine = live.machine_row(node, time.time())
        self.assertEqual(machine["warnings"], [])
        self.assertTrue(machine["unknown"])


class RoutingTests(LiveBase):
    def nodes(self, now, warm_age=1):
        base = {"status": "approved", "runner_fails": 0, "inventory": {"hardware": {"ram_gb": 16}}, "ceiling": "auto-edit",
                "running_tasks": 0, "models": ["qwen3:8b"], "heartbeat_at": now, "polled_at": now,
                "runner_age_s": warm_age}
        runner = {"agents": {"ollama": "http://o"}, "max_mode": "auto-edit"}
        cold = dict(base, id="big", name="big", inventory={"hardware": {"ram_gb": 64}},
                    runner=dict(runner, metrics={"ollama": {"up": True, "loaded": []}}))
        warm = dict(base, id="small", name="small", running_tasks=1,
                    runner=dict(runner, metrics={"ollama": {"up": True, "loaded": ["qwen3:8b"]}}))
        return [cold, warm]

    def test_warm_node_wins_only_while_fresh(self):
        now = time.time()
        original = tasks.node_rows
        try:
            tasks.node_rows = lambda: self.nodes(now)
            node, _, _ = tasks.pick_node("ollama", "qwen3:8b", "read-only", None)
            self.assertEqual(node["id"], "small")
            tasks.node_rows = lambda: self.nodes(now, warm_age=120)
            node, _, _ = tasks.pick_node("ollama", "qwen3:8b", "read-only", None)
            self.assertEqual(node["id"], "big")
        finally:
            tasks.node_rows = original

    def test_no_paid_probes_exist(self):
        FakeRunner.calls.clear()
        self.c.portal.call(live.sweep_once)
        self.c.post("/api/refresh", headers=W)
        time.sleep(0.2)
        self.assertEqual([p for p, _ in FakeRunner.calls if p == "/jobs"], [])


class FailoverTests(LiveBase):
    def tearDown(self):
        with db() as c:
            c.execute("DELETE FROM nodes WHERE id='n-off'")
        super().tearDown()

    def offline(self, node_id="n-off"):
        now = time.time()
        with db() as c:
            c.execute("INSERT OR REPLACE INTO nodes (id, name, status, runner, runner_fails, models, heartbeat_at, polled_at) "
                      "VALUES (?, ?, 'approved', ?, 0, '[]', ?, ?)",
                      (node_id, node_id, json.dumps({"agents": {"claude": "/x/claude"}, "max_mode": "auto-edit",
                                                     "budget": True}), now - 120, now - 120))
        return node_id

    def park(self, task, node_id, **placement):
        with db() as c:
            c.execute("UPDATE tasks SET state='queued', cwd=NULL, branch=NULL, node_id=?, placement=? WHERE id=?",
                      (node_id, json.dumps({"pinned": False} | placement), task["id"]))

    def test_lease_race_between_try_start_and_replace(self):
        dead = self.offline()
        live.STARTED_AT = time.time() - 120
        t = self.ok(agent="claude", model=SONNET, kind="draft")
        self.park(t, dead)
        stale = tasks.get_task(t["id"])  # try_start's snapshot, taken before the move
        self.assertTrue(live.replace(stale, time.time()))
        self.assertEqual(self.get(t["id"])["node_id"], "master")
        original, original_eligible = tasks.node_of, tasks.eligible
        tasks.node_of = lambda task: original(stale)  # the start still believes the old node
        tasks.eligible = lambda *a, **k: None  # eligibility passes, so only the conditional claim can refuse
        try:
            started = self.c.portal.call(tasks.try_start, stale)
        finally:
            tasks.node_of, tasks.eligible = original, original_eligible
        self.assertFalse(started)  # the lease holds on the node it was checked for
        self.assertEqual(self.get(t["id"])["state"], "queued")
        self.assertEqual(self.get(t["id"])["node_id"], "master")

    def test_lease_start_only_on_the_node_it_was_checked_for(self):
        t = self.ok(agent="claude", model=SONNET, kind="draft")
        with db() as c:
            c.execute("UPDATE tasks SET state='queued' WHERE id=?", (t["id"],))
        snapshot = tasks.get_task(t["id"])
        with db() as c:  # a re-placement moved it meanwhile
            c.execute("UPDATE tasks SET node_id='elsewhere' WHERE id=?", (t["id"],))
        self.assertFalse(tasks.transition(snapshot, {"queued"}, "starting", where_node=snapshot["node_id"]))
        self.assertEqual(tasks.get_task(t["id"])["state"], "queued")

    def test_placed_queued_work_moves_pinned_and_null_placement_wait(self):
        live.STARTED_AT = time.time() - 120
        dead = self.offline()
        placed = self.ok(agent="claude", model=SONNET, kind="draft")
        pinned = self.ok(agent="claude", model=SONNET, kind="draft")
        legacy = self.ok(agent="claude", model=SONNET, kind="draft")
        self.park(placed, dead)
        self.park(pinned, dead, pinned=True)
        with db() as c:
            c.execute("UPDATE tasks SET state='queued', cwd=NULL, node_id=?, placement=NULL WHERE id=?", (dead, legacy["id"]))
        self.c.portal.call(live.sweep_once)
        moved = self.get(placed["id"])
        self.assertEqual(moved["node_id"], "master")
        self.assertEqual((moved["profile"], moved["account"]), ("fleet", "claude/owner-max"))
        with db() as c:
            row = c.execute("SELECT policy, policy_hash FROM tasks WHERE id=?", (placed["id"],)).fetchone()
        effective = tasks.pick_node("claude", SONNET, "auto-edit", "master")[1]
        self.assertEqual((json.loads(row["policy"]), row["policy_hash"]), (effective["policy"], effective["hash"]))
        self.assertEqual(self.get(pinned["id"])["node_id"], dead)
        self.assertEqual(self.get(legacy["id"])["node_id"], dead)
        with db() as c:
            self.assertIsNotNone(c.execute("SELECT 1 FROM events WHERE type='task.replaced' AND task_id=?",
                                           (placed["id"],)).fetchone())
            c.execute("DELETE FROM nodes WHERE id=?", (dead,))

    def test_a_model_less_task_is_replaced_only_onto_the_fleet_profile(self):
        live.STARTED_AT = time.time() - 120
        try:  # a named profile listed first, on the same account as the fleet one
            FakeRunner.profiles = {"claude": [{"id": "spare", "label": "owner-max", "ready": True},
                                              {"id": "fleet", "label": "owner-max", "ready": True}],
                                   "codex": PROFILES["codex"]}
            self.refresh()
            dead = self.offline()
            t = self.ok(agent="claude", kind="draft")
            self.assertEqual(self.get(t["id"])["profile"], "fleet")
            self.park(t, dead)
            with db() as c:
                c.execute("UPDATE tasks SET model=NULL WHERE id=?", (t["id"],))
            self.assertTrue(live.replace(tasks.get_task(t["id"]), time.time()))
            self.assertEqual((self.get(t["id"])["node_id"], self.get(t["id"])["profile"]), ("master", "fleet"))
        finally:
            FakeRunner.profiles = PROFILES
            self.refresh()

    def test_a_queued_review_of_an_offline_nodes_build_is_pinned(self):
        build = self.ok(agent="claude", model=OPUS, kind="build", gate="pytest")
        review = self.ok(agent="claude", model=SONNET, kind="review", review_of=build["id"], mode="read-only",
                         worktree=False)
        with db() as c:
            placement = json.loads(c.execute("SELECT placement FROM tasks WHERE id=?", (review["id"],)).fetchone()[0])
        self.assertTrue(placement["pinned"])

    def test_level_triggered_replacement_when_a_node_appears_later(self):
        live.STARTED_AT = time.time() - 120
        dead = self.offline()
        t = self.ok(agent="claude", model=SONNET, kind="draft")
        self.park(t, dead)
        original = tasks.pick_node

        def nowhere(*a, **kw):
            raise tasks.HTTPException(409, "no node")

        tasks.pick_node = nowhere
        try:
            self.c.portal.call(live.sweep_once)
        finally:
            tasks.pick_node = original
        self.assertEqual(self.get(t["id"])["node_id"], dead)
        self.c.portal.call(live.sweep_once)  # next pass: a node qualifies now
        self.assertEqual(self.get(t["id"])["node_id"], "master")
        with db() as c:
            c.execute("DELETE FROM nodes WHERE id=?", (dead,))


if __name__ == "__main__":
    unittest.main()
