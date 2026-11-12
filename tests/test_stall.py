"""Part E, controller side: stalled-task detection and rerouting (docs/SPEC-stall-reroute.md; §12 v2 overrides).
Fake runner with fixture /health "jobs" payloads for every verdict. Run:
uv run --with fastapi --with uvicorn --with httpx --with 'mcp<2' python -m unittest -v tests/test_stall.py
"""
import asyncio
import json
import time
import unittest

from tests.test_budget import rules
from tests.test_controller import FakeRunner, W, wait_for
from tests.test_limit_handoff import CLAUDE_LIMIT, FABLE, OPUS, SONNET, HandoffBase

import budget
import controller
import handoff
import live
import stall
import tasks
from store import db

HAIKU = "claude-haiku-5-5"


def job(task_id, **kw):
    """A runner /health jobs entry (the shared contract): {id, state, alive, gate_alive, idle_s, activity_src, cpu_s}."""
    return {"id": task_id, "state": "running", "alive": True, "gate_alive": False, "idle_s": 10,
            "activity_src": "output", "cpu_s": 5.0} | kw


class StallBase(HandoffBase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._auto = stall.AUTO
        stall.AUTO = False  # the background sweep leaves stalls to the tests, which call stall.sweep themselves

    @classmethod
    def tearDownClass(cls):
        stall.AUTO = cls._auto
        FakeRunner.health_extra = {}
        FakeRunner.cancel_reply = None
        FakeRunner.models.pop("other-model:3b", None)
        super().tearDownClass()

    def setUp(self):
        super().setUp()
        self._started = live.STARTED_AT
        live.STARTED_AT = time.time() - 600  # past the controller's restart grace
        self.addCleanup(setattr, live, "STARTED_AT", self._started)
        FakeRunner.cancel_reply = None
        stall.reset_state()
        tasks.UNREACHABLE_POLLS.clear()
        with db() as c:  # freshness here comes from the suite's fast polls, not from older heartbeat tests
            c.execute("UPDATE nodes SET heartbeat_at=NULL, heartbeat_ts=NULL")
        for model in (OPUS, SONNET, HAIKU, FABLE):  # the newest runs on record name each tier's concrete model
            self.seen(model)

    def tearDown(self):
        self.set_jobs()
        super().tearDown()

    # helpers
    def seen(self, model, agent="claude"):
        with db() as c:
            c.execute("INSERT OR IGNORE INTO agent_usage (ts, task_id, run, agent, model, outcome) VALUES (?,?,?,?,?, 'ok')",
                      (time.time() - 1, f"seed-{model}-{time.time()}", 1, agent, model))

    def set_jobs(self, *entries, absent=False):
        FakeRunner.health_extra = {} if absent else {"jobs": list(entries)}
        want = None if absent else list(entries)
        self.c.post("/api/refresh", headers=W)
        wait_for(lambda: (self.master()["runner"] or {}).get("jobs") == want and
                 (time.time() - (self.master().get("polled_at") or 0)) < 5)

    def master(self):
        return next(n for n in tasks.node_rows() if n["id"] == "master")

    def age(self, task_id, minutes=10):
        then = time.time() - minutes * 60
        with db() as c:
            c.execute("UPDATE tasks SET started_at=?, run_started_at=? WHERE id=?", (then, then, task_id))

    def sweep(self, now=None):
        self.c.portal.call(stall.sweep, now)

    def verdict(self, task_id, now=None):
        return stall.classify(tasks.get_task(task_id), self.master(), time.time() if now is None else now)

    def record(self, task_id):
        return tasks.get_task(task_id).get("stall") or {}

    def events(self, task_id, kind):
        with db() as c:
            return [json.loads(r[0] or "{}") for r in c.execute(
                "SELECT data FROM events WHERE task_id=? AND type=? ORDER BY id", (task_id, kind))]

    def running(self, model=OPUS, **kw):
        t = self.build(model=model, **kw)
        self.assertEqual(t["state"], "working")
        self.age(t["id"])
        return t


# ---------- the /health parser ----------

class HealthParserTests(unittest.TestCase):
    def test_jobs_are_kept_cleaned_and_capped(self):
        raw = [job(f"t-{i:08x}") | {"secret": "x", "cwd": "/home/me"} for i in range(40)]
        raw[1] = {"id": "t-00000001", "state": "running", "alive": "yes", "gate_alive": None, "idle_s": -5,
                  "activity_src": "session", "cpu_s": "lots"}
        raw.insert(0, "junk")
        out = tasks.clean_health({"ok": True, "jobs": raw})["jobs"]
        self.assertEqual(len(out), 32)
        self.assertEqual(set(out[0]), {"id", "state", "alive", "gate_alive", "idle_s", "activity_src", "cpu_s"})
        self.assertEqual(out[1], {"id": "t-00000001", "state": "running", "alive": None, "gate_alive": None,
                                  "idle_s": None, "activity_src": None, "cpu_s": None})

    def test_bad_jobs_value_is_dropped(self):
        self.assertNotIn("jobs", tasks.clean_health({"ok": True, "jobs": {"a": 1}}))
        self.assertNotIn("jobs", tasks.clean_health({"ok": True}))

    def test_a_heartbeat_without_jobs_drops_the_stored_list(self):
        stored = {"agents": {"claude": "/x"}, "jobs": [job("t-00000001")]}
        self.assertNotIn("jobs", tasks.merge_health(stored, {"ok": True, "running": 0}))
        self.assertEqual(tasks.merge_health(stored, {"ok": True, "jobs": []})["jobs"], [])


# ---------- verdicts from fixture payloads ----------

class VerdictTests(StallBase):
    def test_ok(self):
        t = self.running()
        self.set_jobs(job(t["id"]))
        self.assertEqual(self.verdict(t["id"])["kind"], "ok")

    def test_dead_when_running_but_neither_agent_nor_gate_is_alive(self):
        t = self.running()
        self.set_jobs(job(t["id"], alive=False, gate_alive=False))
        self.assertEqual(self.verdict(t["id"])["kind"], "dead")
        self.set_jobs(job(t["id"], alive=False, gate_alive=True))  # the gate counts as alive (12.2)
        self.assertEqual(self.verdict(t["id"])["kind"], "ok")

    def test_idle_after_the_clamped_threshold_and_cpu_growth_counts_as_activity(self):
        t = self.running()
        self.assertEqual(stall.idle_after(tasks.get_task(t["id"])), 30 * 60)  # 0.25 x 120 min
        self.set_jobs(job(t["id"], idle_s=29 * 60))
        self.assertEqual(self.verdict(t["id"])["kind"], "ok")
        self.set_jobs(job(t["id"], idle_s=31 * 60, cpu_s=5.0))
        self.assertEqual(self.verdict(t["id"])["kind"], "idle")
        self.set_jobs(job(t["id"], idle_s=31 * 60, cpu_s=9.0))  # CPU time grew since the last look: not idle
        self.assertEqual(self.verdict(t["id"])["kind"], "ok")

    def test_idle_after_clamp(self):
        self.assertEqual(stall.idle_after({"policy": {"limits": {"task_minutes": 20}}}), 15 * 60)
        self.assertEqual(stall.idle_after({"policy": {"limits": {"task_minutes": 600}}}), 40 * 60)
        self.assertEqual(stall.idle_after({"policy": {}}), 30 * 60)

    def test_no_jobs_in_the_payload_is_unknown(self):
        t = self.running()
        self.set_jobs(absent=True)
        self.assertEqual(self.verdict(t["id"])["kind"], "unknown")

    def test_a_job_missing_from_the_list_is_unknown(self):
        t = self.running()
        self.set_jobs(job("t-ffffffff"))
        self.assertEqual(self.verdict(t["id"])["kind"], "unknown")

    def test_stale_data_is_ignored(self):
        t = self.running()
        self.set_jobs(job(t["id"], alive=False))
        self.assertEqual(self.verdict(t["id"], now=time.time() + 120)["kind"], "unknown")

    def test_unreachable_after_three_failed_job_polls(self):
        t = self.running()
        self.set_jobs(job(t["id"]))
        self.assertEqual(self.verdict(t["id"])["kind"], "ok")
        saved = FakeRunner.jobs.pop(t["id"])  # /health still answers, GET /jobs/{id} now fails (as t-253101a7)
        self.addCleanup(FakeRunner.jobs.__setitem__, t["id"], saved)
        wait_for(lambda: tasks.UNREACHABLE_POLLS.get(t["id"], 0) >= 3)
        self.assertEqual(self.verdict(t["id"])["kind"], "unreachable")

    def test_task_grace_first_five_minutes(self):
        t = self.running()
        self.age(t["id"], minutes=4)
        self.set_jobs(job(t["id"], alive=False))
        self.assertEqual(self.verdict(t["id"])["kind"], "grace")

    def test_restart_grace_means_no_verdicts_at_all(self):
        t = self.running()
        self.set_jobs(job(t["id"], alive=False))
        live.STARTED_AT = time.time() - 60
        self.sweep()
        self.assertEqual(tasks.get_task(t["id"])["state"], "working")
        self.assertFalse(self.posts(t["id"], "cancel"))

    def test_overrun_alerts_once_and_does_not_stop(self):
        t = self.running()
        self.age(t["id"], minutes=100)  # over 80% of 120 min
        self.set_jobs(job(t["id"]))
        self.assertTrue(self.verdict(t["id"])["overrun"])
        self.sweep()
        self.sweep()
        self.assertEqual(tasks.get_task(t["id"])["state"], "working")
        self.assertEqual(len(self.events(t["id"], "task.overrun")), 1)

    def test_approval_and_blocked_tasks_are_never_touched(self):
        waiting = self.running()
        blocked = self.running(model=SONNET)
        with db() as c:
            c.execute("UPDATE tasks SET state='awaiting_approval' WHERE id=?", (waiting["id"],))
            c.execute("UPDATE tasks SET state='blocked' WHERE id=?", (blocked["id"],))
        self.set_jobs(job(waiting["id"], alive=False), job(blocked["id"], alive=False))
        self.sweep()
        self.assertEqual(tasks.get_task(waiting["id"])["state"], "awaiting_approval")
        self.assertEqual(tasks.get_task(blocked["id"])["state"], "blocked")
        self.assertFalse(self.posts(blocked["id"], "cancel"))


# ---------- the response ----------

class ResponseTests(StallBase):
    def test_dead_job_is_stopped_checkpointed_and_rerouted(self):
        t = self.running()
        self.set_jobs(job(t["id"], alive=False))
        self.sweep()
        old = tasks.get_task(t["id"])
        self.assertEqual((old["state"], old["failure_reason"]), ("failed", "stalled"))
        self.assertEqual(old["stall"]["kind"], "dead")
        self.assertEqual(len(self.posts(t["id"], "cancel")), 1)
        [cp] = self.posts(t["id"], "checkpoint")
        self.assertIn("stalled", cp["label"])
        [new_id] = self.children(t["id"])
        new = tasks.get_task(new_id)
        self.assertEqual(new["link"], "retry")
        self.assertEqual(new["launch_extra"]["base_branch"], f"fleet/{t['id']}")
        self.assertEqual(new["launch_extra"]["stall_reason"], "stalled")
        self.assertNotEqual((new["agent"], new["model"], new["account"]), (old["agent"], old["model"], old["account"]))
        self.assertEqual((new["agent"], new["model"]), ("claude", SONNET))  # build: cheapest capable tier first
        self.assertIn("stalled (dead)", new["prompt"])
        self.assertNotIn("usage limit", new["prompt"])
        self.assertEqual(old["resolution"]["kind"], "handed_off")
        self.assertEqual(len(self.events(t["id"], "task.stalled")), 1)
        self.sweep()  # a second pass changes nothing
        self.assertEqual(len(self.events(t["id"], "task.stalled")), 1)
        self.assertEqual(self.children(t["id"]), [new_id])
        # usage was recorded for the stopped run
        with db() as c:
            self.assertTrue(c.execute("SELECT 1 FROM agent_usage WHERE task_id=?", (t["id"],)).fetchone())

    def test_idle_claude_task_is_nudged_once_then_rerouted(self):
        t = self.running()
        self.set_jobs(job(t["id"], idle_s=40 * 60))
        self.sweep()
        now = tasks.get_task(t["id"])
        self.assertEqual(now["state"], "working")  # resumed on the same session
        [msg] = self.posts(t["id"], "message")
        self.assertIn("no progress", msg["text"])
        self.assertTrue(now["stall"].get("nudged_at"))
        self.assertEqual(self.children(t["id"]), [])
        # still idle after the nudge: stop and reroute, no second nudge
        self.age(t["id"])
        self.set_jobs(job(t["id"], idle_s=20 * 60, cpu_s=6.0))
        stall.reset_state()
        self.sweep()
        self.assertEqual(len(self.posts(t["id"], "message")), 1)
        self.assertEqual(tasks.get_task(t["id"])["failure_reason"], "stalled")
        self.assertEqual(len(self.children(t["id"])), 1)

    def test_nudged_at_survives_a_restart(self):
        t = self.running()
        self.set_jobs(job(t["id"], idle_s=40 * 60))
        self.sweep()
        stall.reset_state()  # a controller restart forgets everything in memory
        self.age(t["id"])
        self.sweep()
        self.assertEqual(len(self.posts(t["id"], "message")), 1)
        self.assertEqual(len(self.children(t["id"])), 1)

    def test_ollama_skips_the_nudge(self):
        FakeRunner.models["other-model:3b"] = False
        self.refresh_models()
        t = self.ok(agent="ollama", model="fake-model:1b", kind="draft")
        self.age(t["id"])
        self.set_jobs(job(t["id"], idle_s=40 * 60))
        self.sweep()
        self.assertFalse(self.posts(t["id"], "message"))
        [new_id] = self.children(t["id"])
        new = tasks.get_task(new_id)
        self.assertEqual(new["agent"], "ollama")  # free local work never becomes paid work by itself
        self.assertNotEqual(new["model"], "fake-model:1b")

    def refresh_models(self):
        self.c.post("/api/refresh", headers=W)
        wait_for(lambda: "other-model:3b" in self.master()["models"])

    def test_unconfirmed_stop_escalates_and_leaves_the_worktree(self):
        t = self.running()
        FakeRunner.cancel_reply = {"state": "running"}
        self.set_jobs(job(t["id"], alive=False))
        self.sweep()
        old = tasks.get_task(t["id"])
        self.assertEqual((old["state"], old["failure_reason"]), ("failed", "stalled"))
        self.assertTrue(old["stall"]["escalated"])
        self.assertFalse(self.posts(t["id"], "checkpoint"))
        self.assertEqual(self.children(t["id"]), [])
        self.assertEqual(len(self.events(t["id"], "task.escalated")), 1)
        self.assertIn("task.escalated", [a["type"] for a in live.alerts() if a["task_id"] == t["id"]])

    def test_checkpoint_failure_escalates(self):
        t = self.running()
        FakeRunner.checkpoint_error = (409, "worktree is mid-merge, mid-rebase or locked")
        self.set_jobs(job(t["id"], alive=False))
        self.sweep()
        old = tasks.get_task(t["id"])
        self.assertTrue(old["stall"]["escalated"])
        self.assertIsNone(old["resolution"])
        self.assertEqual(self.children(t["id"]), [])

    def test_a_job_that_already_exited_cleanly_settles_normally(self):
        t = self.running()
        FakeRunner.cancel_reply = {"state": "exited", "exit_code": 0, "result": "done",
                                   "gate": {"state": "passed", "exit_code": 0}}
        self.set_jobs(job(t["id"], alive=False))
        self.sweep()
        settled = tasks.get_task(t["id"])
        self.assertEqual(settled["state"], "review")
        self.assertIsNone(settled["failure_reason"])
        self.assertEqual(self.children(t["id"]), [])

    def test_interrupted_also_reroutes(self):
        t = self.running()
        FakeRunner.jobs[t["id"]].update(state="lost")
        wait_for(lambda: self.children(t["id"]))
        old = tasks.get_task(t["id"])
        self.assertEqual(old["state"], "interrupted")
        self.assertEqual(old["resolution"]["kind"], "handed_off")
        new = tasks.get_task(self.children(t["id"])[0])
        self.assertEqual(new["launch_extra"]["stall_reason"], "interrupted")

    def test_no_free_slot_waits_and_the_sweep_retries(self):
        body = rules()
        for key in body["models"]:
            if key != "claude:claude-opus-*":
                body["models"][key]["kinds"] = ["review"]  # only Opus may build
        self.put_rules(body)
        t = self.running()
        other = self.build()  # Opus on the spare account: the only other build slot is now busy
        self.assertEqual(other["profile"], "spare")
        self.set_jobs(job(t["id"], alive=False), job(other["id"]))
        self.sweep()
        old = tasks.get_task(t["id"])
        self.assertEqual(old["stall"]["waiting"], "stalled; no free slot")
        self.assertEqual(self.children(t["id"]), [])
        self.c.post(f"/api/tasks/{other['id']}/cancel", headers=W)
        self.sweep()
        [new_id] = self.children(t["id"])
        self.assertEqual(tasks.get_task(new_id)["profile"], "spare")
        self.assertNotIn("waiting", tasks.get_task(t["id"])["stall"])

    def test_on_task_stall_leave(self):
        body = rules()
        body["limits"]["on_task_stall"] = {"*": "leave"}
        self.put_rules(body)
        t = self.running()
        self.set_jobs(job(t["id"], alive=False))
        self.sweep()
        self.assertEqual(self.children(t["id"]), [])
        self.assertEqual(tasks.get_task(t["id"])["failure_reason"], "stalled")

    def test_third_stall_escalates_and_limit_handoffs_count_separately(self):
        t = self.running()
        current = t["id"]
        for _ in range(2):
            self.set_jobs(job(current, alive=False))
            self.sweep()
            [current] = self.children(current)
            self.age(current)
        self.assertEqual(handoff.stall_handoffs_in_chain(current), 2)
        self.assertEqual(handoff.handoffs_in_chain(current), 0)
        self.set_jobs(job(current, alive=False))
        self.sweep()
        self.assertEqual(self.children(current), [])
        last = tasks.get_task(current)
        self.assertTrue(last["stall"]["escalated"])
        self.assertEqual(len(self.events(current, "task.escalated")), 1)

    def test_a_claimed_task_is_never_rerouted_twice(self):
        t = self.running()
        FakeRunner.jobs[t["id"]].update(state="cancelled")
        self.fail_in_db(t["id"], failure_reason="stalled", result="", resets_at=None)
        blob = handoff.claim(t["id"])
        result = self.c.portal.call(handoff.handle_handoff, t["id"], "stalled")
        self.assertEqual(result["action"], "none")
        handoff.unclaim(t["id"], blob)

        async def both():
            return await asyncio.gather(handoff.handle_handoff(t["id"], "stalled"),
                                        handoff.handle_handoff(t["id"], "stalled"))
        self.c.portal.call(both)
        self.assertEqual(len(self.children(t["id"])), 1)

    def test_sweep_once_runs_the_stall_pass(self):
        t = self.running()
        self.set_jobs(job(t["id"], alive=False))
        stall.AUTO = True
        try:
            self.c.portal.call(live.sweep_once)
        finally:
            stall.AUTO = False
        self.assertEqual(tasks.get_task(t["id"])["failure_reason"], "stalled")


# ---------- ranking and allocation ----------

class RankTests(StallBase):
    def slots(self, kind, task=None, exclude=()):
        return handoff.rank_slots(kind, exclude, task)

    def test_build_prefers_standard_tier_then_idle_and_never_fable(self):
        ranked = [(s["model"], s["profile"]) for s in self.slots("build")]
        self.assertEqual(ranked[:2], [(SONNET, "fleet"), (SONNET, "spare")])
        self.assertLess(ranked.index((OPUS, "fleet")), ranked.index((HAIKU, "fleet")))
        self.assertNotIn(FABLE, [m for m, _ in ranked])

    def test_rule_zero_is_hard_coded(self):
        body = rules()
        body["models"]["claude:claude-fable-*"]["kinds"] = ["*"]
        self.put_rules(body)
        for kind in ("build", "fix", "test"):
            self.assertNotIn(FABLE, [s["model"] for s in self.slots(kind)], kind)
        self.assertIn(FABLE, [s["model"] for s in self.slots("review")])

    def test_busy_slots_rank_after_idle_ones(self):
        self.build(model=SONNET)  # on the fleet account
        ranked = [(s["model"], s["profile"]) for s in self.slots("build")]
        self.assertEqual(ranked[0], (SONNET, "spare"))
        self.assertLess(ranked.index((SONNET, "spare")), ranked.index((SONNET, "fleet")))

    def test_excludes_the_stalled_slot_and_full_or_limited_ones(self):
        t = self.build(model=OPUS)
        stalled = tasks.get_task(t["id"])
        ranked = self.slots("build", stalled, exclude=[handoff.slot_identity(stalled)])
        self.assertNotIn((OPUS, "fleet"), [(s["model"], s["profile"]) for s in ranked])  # stalled and full (max 1)
        self.limit(self.build(model=SONNET)["id"])
        ranked = [(s["model"], s["profile"]) for s in self.slots("build")]
        self.assertNotIn((SONNET, "fleet"), ranked)

    def test_review_excludes_every_builder_model_and_keeps_the_same_node(self):
        t = self.build(model=SONNET)
        review = {"id": t["id"], "node_id": "master", "agent": "claude"}
        models = {s["model"] for s in self.slots("review", review)}
        self.assertNotIn(SONNET, models)
        self.assertIn(FABLE, models)
        self.assertEqual({s["node"] for s in self.slots("build", review)}, {"master"})

    def test_cross_vendor_is_allowed_for_a_stalled_codex_build(self):
        self.seen("gpt-5-codex", agent="codex")
        t = self.ok(agent="codex", model="gpt-5-codex", kind="build", gate="pytest")
        stalled = tasks.get_task(t["id"])
        ranked = self.slots("build", stalled, exclude=[handoff.slot_identity(stalled)])
        self.assertEqual((ranked[0]["agent"], ranked[0]["model"]), ("claude", SONNET))


class AutoAllocationTests(StallBase):
    def test_auto_picks_the_cheapest_capable_free_slot(self):
        t = self.ok(agent="auto", kind="build", gate="pytest")
        self.assertEqual((t["agent"], t["model"]), ("claude", SONNET))
        self.assertTrue(t["placement"]["allocated"])
        self.assertTrue(self.events(t["id"], "task.allocated"))

    def test_auto_review_skips_the_builders(self):
        build = self.build(model=SONNET)
        t = self.ok(agent="auto", kind="review", parent_id=build["id"], link="review", mode="read-only",
                    worktree=False, gate=None)
        self.assertNotEqual(t["model"], SONNET)

    def test_auto_with_a_model_is_refused_and_plain_omitted_model_keeps_its_meaning(self):
        r = self.c.post("/api/tasks", json={"agent": "auto", "model": SONNET, "prompt": "x", "kind": "build"}, headers=W)
        self.assertEqual(r.status_code, 400)
        t = self.ok(agent="claude", kind="draft")
        self.assertIsNone(t["model"])
        self.assertFalse((t["placement"] or {}).get("allocated"))

    def test_auto_with_no_slot_is_409(self):
        body = rules()
        for key in body["models"]:
            body["models"][key]["kinds"] = ["review"]
        self.put_rules(body)
        r = self.c.post("/api/tasks", json={"agent": "auto", "prompt": "x", "kind": "build", "gate": "pytest",
                                            "repo": "/repo"}, headers=W)
        self.assertEqual(r.status_code, 409, r.text)


class ResourcesTests(StallBase):
    def test_resources_gain_tier_allowed_kinds_and_idle(self):
        rows = self.c.get("/api/resources", headers={"X-Fleet-Scope": "view"}).json()
        fable = next(r for r in rows if r["model"] == "claude:claude-fable-*")
        self.assertEqual(fable["tier"], "premium")
        self.assertNotIn("build", fable["allowed_kinds"])
        self.assertIn("review", fable["allowed_kinds"])
        sonnet = next(r for r in rows if r["model"] == "claude:claude-sonnet-*" and r["profile"] == "fleet")
        self.assertTrue(sonnet["idle"])
        self.assertEqual(sonnet["concrete_model"], SONNET)
        body = rules()
        body["models"]["claude:claude-fable-*"]["kinds"] = ["*"]
        self.put_rules(body)
        rows = self.c.get("/api/resources").json()
        fable = next(r for r in rows if r["model"] == "claude:claude-fable-*")
        self.assertNotIn("build", fable["allowed_kinds"])  # rule 0 holds whatever the rules say


class PipelineStallTests(StallBase):
    def test_a_stalled_build_is_rerouted_without_using_a_fix_round(self):
        body = {"title": "Stall pipeline", "spec": "Build the greeting", "repo": "/repo", "gate": "pytest",
                "mode": "auto-edit", "builder": {"agent": "claude", "model": OPUS}, "max_rounds": 3,
                "reviewers": [{"kind": "agent", "agent": "claude", "model": FABLE}]}
        r = self.c.post("/api/pipelines", json=body, headers=W)
        self.assertEqual(r.status_code, 200, r.text)
        p = r.json()
        first = p["build_task"]
        self.age(first)
        self.set_jobs(job(first, alive=False))
        self.sweep()  # pipeline-owned: the sweep only stops it; the pipeline poll reroutes it
        self.assertEqual(tasks.get_task(first)["failure_reason"], "stalled")

        async def polled():
            async with controller._pipeline_locks.setdefault(p["id"], asyncio.Lock()):
                pass
            await controller.poll_pipeline(p["id"])
        self.c.portal.call(polled)
        current = self.c.get(f"/api/pipelines/{p['id']}").json()
        [new] = self.children(first)
        self.assertEqual((current["state"], current["build_task"], current["round"]), ("building", new, p["round"]))
        self.assertTrue(any("stalled" in (h.get("note") or "") for h in current["history"]))
        self.assertFalse(any("usage limit" in (h.get("note") or "") for h in current["history"]))


if __name__ == "__main__":
    unittest.main()
