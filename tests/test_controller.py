"""Controller tests against a fake runner. Run:
uv run --with fastapi --with uvicorn --with httpx python -m unittest -v tests/test_controller.py
"""
import json
import asyncio
import hashlib
import os
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="fleet-ctl-"))
SKILLS_DIR = TMP / "skills"
SKILLS_DIR.mkdir()
EVAL_RESULTS = TMP / "eval-results" / "results.json"
FAKE_EVAL = TMP / "fake_eval.py"
FAKE_EVAL.write_text('''
import argparse, json, time
from pathlib import Path
p = argparse.ArgumentParser()
p.add_argument("--models"); p.add_argument("--roles"); p.add_argument("--ollama"); p.add_argument("--out")
a = p.parse_args()
time.sleep(0.25)
path = Path(a.out) / "results.json"
path.parent.mkdir(parents=True, exist_ok=True)
try: rows = json.loads(path.read_text())
except (OSError, ValueError): rows = []
row = {"model": a.models, "roles": {role: {"score": 91.0} for role in a.roles.split(",")},
       "size_gb": 1.0, "fits_16gb": True, "median_s": 0.5, "tokens_per_s": 42.0,
       "errors": [], "evaluated_at": time.time()}
rows = [old for old in rows if old.get("model") != a.models] + [row]
path.write_text(json.dumps(rows))
print("fake eval complete")
''')
FAKE_TS = TMP / "tailscale"
FAKE_TS.write_text("#!/bin/sh\necho '{\"Peer\":{}}'\n")
FAKE_TS.chmod(0o755)
(TMP / "no-plugins").mkdir(exist_ok=True)  # never load the real notification plugin in tests
os.environ.update({"FLEET_PLUGINS_DIR": str(TMP / "no-plugins"), "FLEET_DB": str(TMP / "fleet.db"), "TAILSCALE_BIN": str(FAKE_TS),
                   "FLEET_POLL_SECONDS": "0.3", "FLEET_TASK_POLL_SECONDS": "0.2",
                   "FLEET_PUBLIC_HOST": "master.tail.ts.net", "FLEET_PUBLIC_PORT": "8701", "LITELLM_MASTER_KEY": "",
                   "FLEET_SKILLS_DIR": str(SKILLS_DIR), "FLEET_INSTRUCTIONS_DIR": str(TMP / "no-instructions"),
                   "FLEET_EVAL_RESULTS": str(EVAL_RESULTS),
                   "FLEET_EVAL_CMD": f"{sys.executable} {FAKE_EVAL}"})


class FakeRegistry(BaseHTTPRequestHandler):
    sizes = {"library/qwen3/manifests/8b": 5_200_000_000, "library/tiny/manifests/1b": 800_000_000}

    def log_message(self, *a):
        pass

    def do_GET(self):
        key = self.path.split("/v2/", 1)[-1]
        if key not in FakeRegistry.sizes:
            self.send_response(404)
            self.end_headers()
            return
        data = json.dumps({"config": {"size": 0, "digest": "sha256:cfg"}, "layers": [{"size": FakeRegistry.sizes[key]}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(data)


registry = ThreadingHTTPServer(("127.0.0.1", 0), FakeRegistry)
threading.Thread(target=registry.serve_forever, daemon=True).start()
os.environ["FLEET_OLLAMA_REGISTRY"] = f"http://127.0.0.1:{registry.server_address[1]}/v2"


class FakeRunner(BaseHTTPRequestHandler):
    jobs: dict = {}
    calls: list = []
    max_mode = "auto-edit"
    tokens: list = []  # X-Runner-Token header of every request
    models: dict = {"fake-model:1b": False}  # name -> managed
    ops: dict = {}
    op_script: list = []  # states an ensure op goes through on successive polls; default ready
    agents: dict = {"claude": "/x/claude", "codex": None, "ollama": "http://o"}
    digest = hashlib.sha256((Path(__file__).resolve().parent.parent / "nodes" / "runner.py").read_bytes()).hexdigest()
    # Budget support (docs/SPEC-paid-budget.md): tree hashes per job, the delta since one, an optional full diff.
    trees: dict = {}
    deltas: dict = {}
    delta_text = None  # None: the delta is the same as the full diff
    diff_text = None  # None: "+hello"
    # Account pools (docs/SPEC-limit-handoff.md): None = an older runner that reports no profiles.
    profiles = None
    checkpoint_error = None  # (status, detail) to make the checkpoint fail
    # Live (docs/SPEC-live-monitor.md): extra /health fields (metrics), and the node id the controller told us
    health_extra: dict = {}
    node_ids: list = []
    # Part E (docs/SPEC-stall-reroute.md): what a cancel leaves the job as. None: "cancelled", group_gone true.
    cancel_reply: dict | None = None

    def log_message(self, *a):
        pass

    def _send(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        FakeRunner.tokens.append(self.headers.get("X-Runner-Token"))
        if self.path == "/models":
            return self._send(200, {"engine": "ollama", "version": "0.17.7", "free_disk_gb": 100, "ram_gb": 48,
                                    "models": [{"name": n, "size_gb": 1.0, "digest": "d", "managed": m}
                                               for n, m in FakeRunner.models.items()], "loaded": []})
        if self.path.startswith("/operations/"):
            op = FakeRunner.ops.get(self.path.split("/")[2])
            if not op:
                return self._send(404, {"detail": "no op"})
            if op["script"]:
                op["state"] = op["script"].pop(0)
                if op["state"] == "ready":
                    FakeRunner.models[op["ref"]] = True
                    if op.get("derived"):
                        FakeRunner.models[op["derived"]] = True
                if op["state"] == "removed":
                    FakeRunner.models.pop(op["ref"], None)
                    if op.get("derived"):
                        FakeRunner.models.pop(op["derived"], None)
            return self._send(200, {k: v for k, v in op.items() if k != "script"})
        if self.path == "/health":
            FakeRunner.node_ids.append(self.headers.get("X-Fleet-Node-Id"))
            return self._send(200, {**FakeRunner.health_extra, "ok": True, "version": "2", "digest": FakeRunner.digest,
                                    "max_mode": FakeRunner.max_mode, "running": 0,
                                    "gate_profiles": ["pytest"], "allow_raw_gates": False,
                                    "models": {"remote_management": True, "disk_reserve_gb": 8},
                                    "agents": dict(FakeRunner.agents), "budget": True,
                                    **({"profiles": FakeRunner.profiles, "checkpoint": True}
                                       if FakeRunner.profiles is not None else {})})
        jid = self.path.split("/")[2].split("?")[0]
        if jid not in self.jobs:
            return self._send(404, {"detail": "no job"})
        if "/diff?since=" in self.path:
            delta = FakeRunner.delta_text if FakeRunner.delta_text is not None else (FakeRunner.diff_text or "+hello")
            return self._send(200, {"stat": "", "diff": delta})
        if self.path.endswith("/diff"):
            return self._send(200, {"stat": "1 file changed", "diff": FakeRunner.diff_text or "+hello"})
        if "/log" in self.path:
            return self._send(200, {"offset": 5, "text": "hello"})
        return self._send(200, self.jobs[jid])

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        FakeRunner.tokens.append(self.headers.get("X-Runner-Token"))
        FakeRunner.calls.append((self.path, body))
        if self.path in ("/models/ensure", "/models/remove"):
            oid = f"op-{len(FakeRunner.ops) + 1}"
            kind = self.path.rsplit("/", 1)[1]
            script = list(FakeRunner.op_script) if kind == "ensure" and FakeRunner.op_script else (
                ["pulling", "ready"] if kind == "ensure" else ["removed"])
            FakeRunner.ops[oid] = {"id": oid, "kind": kind, "ref": body["ref"], "derived": body.get("derived"),
                                   "state": "checking", "reason": None, "progress": {"completed": 1, "total": 2},
                                   "script": script}
            return self._send(200, {k: v for k, v in FakeRunner.ops[oid].items() if k != "script"})
        if self.path == "/jobs":
            if body["id"] in self.jobs:
                return self._send(409, {"detail": "duplicate job id"})
            job = {"id": body["id"], "agent": body["agent"], "mode": body["mode"], "state": "running",
                   "exit_code": None, "session_id": "s-1", "cwd": "/w", "worktree": "/w", "branch": f"fleet/{body['id']}",
                   "result": None, "denials": [], "gate": None, "runs": 1, "profile": body.get("profile")}
            self.jobs[body["id"]] = job
            return self._send(200, job)
        jid = self.path.split("/")[2]
        if self.path.endswith("/tree") and jid in self.jobs:
            made = FakeRunner.trees.setdefault(jid, [])
            made.append(f"{len(made) + 1:040x}")
            return self._send(200, {"tree": made[-1]})
        if self.path.endswith("/checkpoint") and jid in self.jobs:
            if FakeRunner.checkpoint_error:
                return self._send(FakeRunner.checkpoint_error[0], {"detail": FakeRunner.checkpoint_error[1]})
            return self._send(200, {"sha": "c0ffee" + "0" * 34, "committed": True, "excluded": [".env"],
                                    "branch": f"fleet/{jid}", "stat": " app.py | 2 +-\n 1 file changed"})
        if self.path.endswith("/message"):
            self.jobs[jid].update(state="running", denials=[], runs=self.jobs[jid]["runs"] + 1)
        elif self.path.endswith("/cancel"):
            self.jobs[jid].update(FakeRunner.cancel_reply or {"state": "cancelled", "group_gone": True})
        return self._send(200, self.jobs[jid])


server = ThreadingHTTPServer(("127.0.0.1", 0), FakeRunner)
threading.Thread(target=server.serve_forever, daemon=True).start()
os.environ["FLEET_LOCAL_RUNNER"] = f"http://127.0.0.1:{server.server_address[1]}"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "master" / "controller"))
from fastapi.testclient import TestClient  # noqa: E402

import auth  # noqa: E402
import controller  # noqa: E402
import events  # noqa: E402
import tasks  # noqa: E402
from store import db  # noqa: E402

# Never probe the real model servers on this machine: a locally installed model (e.g. qwen3:8b) would leak into
# the master node's inventory and break the install tests. The fake runner's /models report is the only source.
controller.PROBES = []

W = {"X-Fleet": "1"}


def wait_for(fn, timeout=5):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(0.05)
    raise AssertionError("condition not met in time")


def settle_pipeline(client, pipeline_id, ready, timeout=15):
    """Drive a pipeline by hand instead of waiting on the background loop: poll its real tasks and run a
    pipeline poll on the app's loop until `ready(pipeline)` returns something truthy."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        current = client.get(f"/api/pipelines/{pipeline_id}").json()
        found = ready(current)
        if found:
            return found
        ids = [current.get("build_task")] + [r.get("task_id") for r in current["reviews"].values()]
        for task_id in filter(None, ids):
            client.portal.call(controller.tasks.poll_task, task_id)
        client.portal.call(controller.poll_pipeline, pipeline_id)
        client.portal.call(asyncio.sleep, 0.01)
    build = client.get(f"/api/tasks/{current['build_task']}").json() if current.get("build_task") else None
    raise AssertionError(f"pipeline did not reach the expected state: {current['state']} {current['history'][-2:]} "
                         f"build={build and (build['state'], build['error'], build['updated_at'])} job={FakeRunner.jobs.get(current.get('build_task'))}")


def unmetered_claude_rules():
    """For suites that start many Claude tasks with no model and never finish them. The default claude:* rule
    (which governs exactly those tasks) caps them at 2 running at once; here it keeps its limit breaker but has
    no cap, so the tests are explicit about the concurrency they rely on."""
    import copy

    import budget
    body = copy.deepcopy(budget.DEFAULT_RULES)
    body["models"]["claude:*"]["max_concurrent"] = 10000
    budget.save_rules(body, "test")


def reset_limits():
    """Every limit recorded so far has already reset: its reset time is in the past."""
    with db() as c:
        c.execute("UPDATE tasks SET resets_at=? WHERE failure_reason='limit'", (time.time() - 1,))
        c.execute("UPDATE agent_usage SET resets_at=? WHERE outcome='limit'", (time.time() - 1,))


class ControllerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ctx = TestClient(controller.app, base_url="http://127.0.0.1")
        cls.c = cls.ctx.__enter__()
        unmetered_claude_rules()
        wait_for(lambda: any(n["id"] == "master" and n["runner_ok"] for n in cls.c.get("/api/fleet").json()["nodes"]))

    @classmethod
    def tearDownClass(cls):
        cls.ctx.__exit__(None, None, None)

    def task(self, **kw):
        body = {"agent": "claude", "prompt": "add a hello function"} | kw
        r = self.c.post("/api/tasks", json=body, headers=W)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def state(self, tid):
        return self.c.get(f"/api/tasks/{tid}").json()["state"]

    def test_master_node_has_runner(self):
        master = next(n for n in self.c.get("/api/fleet").json()["nodes"] if n["id"] == "master")
        self.assertEqual(master["status"], "approved")
        self.assertEqual(master["effective_ceiling"], "auto-edit")

    def test_task_lifecycle_to_review_and_wait(self):
        t = self.task(gate="pytest")
        self.assertEqual(t["state"], "working")
        self.assertEqual(t["node_id"], "master")
        sent = [b for p, b in FakeRunner.calls if p == "/jobs" and b["id"] == t["id"]][0]
        self.assertEqual(sent["gate"], "pytest")
        FakeRunner.jobs[t["id"]].update(state="exited", exit_code=0, result="done!",
                                        gate={"cmd": "pytest", "state": "passed", "exit_code": 0})
        r = self.c.get(f"/api/tasks/wait?ids={t['id']}&timeout=5").json()
        self.assertEqual(r["tasks"][0]["state"], "review")
        self.assertEqual(r["tasks"][0]["result"], "done!")
        types = [e["type"] for e in self.c.get("/api/events/list?limit=1000").json() if e["task_id"] == t["id"]]
        self.assertEqual(types, ["task.queued", "task.starting", "task.working", "task.review"])
        self.assertEqual(self.c.get(f"/api/tasks/{t['id']}/diff").json()["diff"], "+hello")
        self.assertEqual(self.c.post(f"/api/tasks/{t['id']}/complete", headers=W).json()["state"], "done")

    def test_gate_failure_fails_task(self):
        t = self.task(gate="pytest")
        FakeRunner.jobs[t["id"]].update(state="exited", exit_code=0, gate={"state": "failed", "exit_code": 1})
        wait_for(lambda: self.state(t["id"]) == "failed")

    def test_message_resumes(self):
        t = self.task()
        FakeRunner.jobs[t["id"]].update(state="exited", exit_code=1)
        wait_for(lambda: self.state(t["id"]) == "failed")
        r = self.c.post(f"/api/tasks/{t['id']}/message", json={"text": "try again"}, headers=W).json()
        self.assertEqual((r["state"], r["runs"]), ("working", 2))

    def test_supervised_needs_approval(self):
        t = self.task(mode="supervised")
        self.assertEqual(t["state"], "awaiting_approval")
        self.assertIn(t["id"], [x["id"] for x in self.c.get("/api/inbox").json()])
        self.assertNotIn(t["id"], FakeRunner.jobs)
        self.assertEqual(self.c.post(f"/api/tasks/{t['id']}/approve", headers=W).json()["state"], "working")

    def test_denials_block_then_approve_grants_tools(self):
        t = self.task()
        FakeRunner.jobs[t["id"]].update(state="exited", exit_code=0,
                                        denials=[{"tool_name": "Bash", "tool_input": {"command": "ls"}}])
        wait_for(lambda: self.state(t["id"]) == "blocked")
        with db() as c:
            c.execute("UPDATE tasks SET auto_approvals=2 WHERE id=?", (t["id"],))
        approved = self.c.post(f"/api/tasks/{t['id']}/approve", headers=W).json()
        path, body = [c for c in FakeRunner.calls if c[0] == f"/jobs/{t['id']}/message"][-1]
        self.assertEqual(body["allowed_tools"], ["Bash"])
        self.assertEqual(approved["approved_tools"], ["Bash"])
        self.assertEqual(approved["auto_approvals"], 0)
        self.assertEqual(self.state(t["id"]), "working")

    def test_approved_tools_are_sent_on_later_message(self):
        t = self.task()
        FakeRunner.jobs[t["id"]].update(state="exited", exit_code=0, denials=[{"tool_name": "Bash"}])
        wait_for(lambda: self.state(t["id"]) == "blocked")
        self.c.post(f"/api/tasks/{t['id']}/approve", headers=W)

        FakeRunner.jobs[t["id"]].update(state="exited", exit_code=1, denials=[])
        wait_for(lambda: self.state(t["id"]) == "failed")
        resumed = self.c.post(f"/api/tasks/{t['id']}/message", json={"text": "fix review issues"}, headers=W).json()
        body = [b for path, b in FakeRunner.calls if path == f"/jobs/{t['id']}/message"][-1]
        self.assertEqual(body["allowed_tools"], ["Bash"])
        self.assertIsNone(body["mode"])
        self.assertEqual(resumed["approved_tools"], ["Bash"])

    def test_claude_deny_tools_are_not_approved_or_sent(self):
        self.c.put("/api/policies/agent/claude",
                   json={"body": {"tools": {"claude_deny": ["WebFetch"]}}}, headers=W)
        try:
            t = self.task()
            FakeRunner.jobs[t["id"]].update(state="exited", exit_code=0,
                                            denials=[{"tool_name": "WebFetch"}, {"tool_name": "Bash"}])
            wait_for(lambda: self.state(t["id"]) == "blocked")
            approved = self.c.post(f"/api/tasks/{t['id']}/approve", headers=W).json()
            body = [b for path, b in FakeRunner.calls if path == f"/jobs/{t['id']}/message"][-1]
            self.assertEqual(approved["approved_tools"], ["Bash"])
            self.assertEqual(body["allowed_tools"], ["Bash"])
        finally:
            self.c.put("/api/policies/agent/claude", json={"body": {}}, headers=W)

    def test_reject(self):
        t = self.task(mode="supervised")
        self.assertEqual(self.c.post(f"/api/tasks/{t['id']}/reject", headers=W).json()["state"], "cancelled")

    def test_ceiling_and_scheduler_errors(self):
        r = self.c.post("/api/tasks", json={"agent": "claude", "prompt": "x", "mode": "full"}, headers=W)
        self.assertEqual(r.status_code, 409)
        self.assertIn("ceiling", r.text)
        r = self.c.post("/api/tasks", json={"agent": "gemini", "prompt": "x"}, headers=W)
        self.assertEqual(r.status_code, 400)
        r = self.c.post("/api/tasks", json={"agent": "ollama", "prompt": "x"}, headers=W)
        self.assertEqual(r.status_code, 400)
        self.c.post("/api/nodes/master/ceiling", json={"mode": "read-only"}, headers=W)
        r = self.c.post("/api/tasks", json={"agent": "claude", "prompt": "x"}, headers=W)
        self.assertEqual(r.status_code, 409)
        self.c.post("/api/nodes/master/ceiling", json={"mode": "auto-edit"}, headers=W)

    def test_cancel(self):
        t = self.task()
        self.assertEqual(self.c.post(f"/api/tasks/{t['id']}/cancel", headers=W).json()["state"], "cancelled")
        self.assertEqual(FakeRunner.jobs[t["id"]]["state"], "cancelled")

    def test_lost_job_is_interrupted(self):
        t = self.task()
        FakeRunner.jobs[t["id"]].update(state="lost")
        wait_for(lambda: self.state(t["id"]) == "interrupted")

    def test_write_needs_header(self):
        r = self.c.post("/api/tasks", json={"agent": "claude", "prompt": "x"})
        self.assertEqual(r.status_code, 403)

    def test_remote_tokens_and_scopes(self):
        remote = TestClient(controller.app, base_url="https://master.tail.ts.net:8701")
        self.assertEqual(remote.get("/api/tasks").status_code, 403)
        # Claiming to be localhost on the public port doesn't skip the token.
        spoof = TestClient(controller.app, base_url="http://localhost:8701")
        self.assertEqual(spoof.get("/api/tasks").status_code, 403)
        self.assertEqual(TestClient(controller.app, base_url="http://evil.example").get("/api/tasks").status_code, 403)
        view, op, admin = (auth.create_token(s, s) for s in ("view", "operate", "admin"))
        h = lambda tok: {"Authorization": f"Bearer {tok}", **W}  # noqa: E731
        self.assertEqual(remote.get("/api/tasks", headers=h(view)).status_code, 200)
        self.assertEqual(remote.post("/api/tasks", json={"agent": "claude", "prompt": "x"}, headers=h(view)).status_code, 403)
        t = remote.post("/api/tasks", json={"agent": "claude", "prompt": "x", "mode": "supervised"}, headers=h(op))
        self.assertEqual(t.status_code, 200)
        tid = t.json()["id"]
        self.assertEqual(remote.post(f"/api/tasks/{tid}/approve", headers=h(op)).status_code, 403)
        self.assertEqual(remote.post("/api/nodes/master/drain", headers=h(op)).status_code, 403)
        self.assertEqual(remote.post(f"/api/tasks/{tid}/approve", headers=h(admin)).status_code, 200)
        r = remote.get(f"/login?token={view}", follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertIn("fleet_token", r.headers["set-cookie"])

    def test_plugin_receives_event(self):
        out = TMP / "plugin.out"
        script = TMP / "plug.py"
        script.write_text(f"import sys; open({str(out)!r}, 'a').write(sys.stdin.read() + '\\n')")
        events.PLUGINS.append({"name": "test", "events": {"task.awaiting_approval"},
                               "command": [sys.executable, str(script)]})
        try:
            t = self.task(mode="supervised", title="Tidy the README")
            wait_for(lambda: out.exists() and t["id"] in out.read_text())
            sent = [json.loads(line) for line in out.read_text().splitlines() if t["id"] in line][0]
            self.assertEqual(sent["notification"]["title"], "Approval needed")
            self.assertEqual(sent["notification"]["subtitle"], "Tidy the README")
            self.assertIn("supervised mode", sent["notification"]["message"])
        finally:
            events.PLUGINS.pop()

    def test_no_real_plugins_in_tests(self):
        self.assertEqual([p for p in events.PLUGINS if p["name"] == "macos-notify"], [])

    def test_notification_text(self):
        d = events.describe
        self.assertIsNone(d({"type": "task.state", "data": {}}))  # nothing meaningful: not shown
        self.assertIsNone(d({"type": "task.working", "data": {"title": "x"}}))
        self.assertEqual(d({"type": "pipeline.ready", "data": {"title": "G2"}})["title"], "Ready to merge")
        esc = d({"type": "pipeline.escalated", "data": {"title": "G2", "note": "no trusted review"}})
        self.assertEqual((esc["title"], esc["subtitle"], esc["message"]), ("Pipeline needs you", "G2", "no trusted review"))
        self.assertIsNone(d({"type": "model.state", "data": {"state": "ready"}}))
        self.assertIn("blocked", d({"type": "model.state", "data": {"state": "blocked", "model": "m", "node": "n"}})["title"])
        self.assertEqual(d({"type": "notify.test", "data": {}})["subtitle"], "Test notification")

    def test_repeated_notifications_are_dropped(self):
        out = TMP / "plugin2.out"
        script = TMP / "plug2.py"
        script.write_text(f"import sys; open({str(out)!r}, 'a').write(sys.stdin.read() + '\\n')")
        events.PLUGINS.append({"name": "test2", "events": {"pipeline.ready"}, "command": [sys.executable, str(script)]})
        try:
            for _ in range(3):
                events.run_plugins({"id": 1, "ts": 0, "task_id": None, "type": "pipeline.ready", "data": {"title": "Same"}})
            time.sleep(1)
            self.assertEqual(len(out.read_text().splitlines()), 1)
        finally:
            events.PLUGINS.pop()

    def _escalation_task(self, **kw):
        body = {"agent": "claude", "prompt": "q", "mode": "read-only", "worktree": False, "escalation": True} | kw
        return self.c.post("/api/tasks", json=body, headers=W)

    def _set_policy(self, scope, scope_id, body):
        r = self.c.put(f"/api/policies/{scope}/{scope_id}", json={"body": body}, headers=W)
        self.assertEqual(r.status_code, 200, r.text)

    def test_escalation_task_needs_the_selected_nodes_policy_to_allow_the_agent(self):
        allow = {"escalation": {"agents": ["claude"]}}
        try:
            self._set_policy("fleet", "*", allow)
            self._set_policy("node", "master", {"escalation": {"agents": []}})
            before = len(FakeRunner.calls)
            r = self._escalation_task()
            self.assertEqual(r.status_code, 409, r.text)  # fleet allows, node layer denies: refused
            self.assertIn("escalat", r.text)
            r = self._escalation_task(node="master")
            self.assertEqual(r.status_code, 409, r.text)
            self.assertEqual([c for c in FakeRunner.calls[before:] if c[0] == "/jobs"], [])  # nothing ran
            self._set_policy("node", "master", {"escalation": {"agents": ["codex"]}})
            self.assertEqual(self._escalation_task().status_code, 409)  # node excludes this agent
            self._set_policy("node", "master", allow)
            r = self._escalation_task()
            self.assertEqual(r.status_code, 200, r.text)  # fleet and node both allow: scheduled
            self.assertEqual(r.json()["node_id"], "master")
        finally:
            self._set_policy("fleet", "*", {})
            self._set_policy("node", "master", {})

    def test_escalation_marker_only_narrows_never_widens(self):
        try:
            # No layer opts in: escalation is refused by default, but an ordinary task is unaffected.
            self.assertEqual(self._escalation_task().status_code, 409)
            self.assertEqual(self._escalation_task(policy={"escalation": {"agents": ["claude"]}}).status_code, 409)
            r = self.c.post("/api/tasks", json={"agent": "claude", "prompt": "q", "mode": "read-only",
                                                 "worktree": False}, headers=W)
            self.assertEqual(r.status_code, 200, r.text)
            # An ordinary task is not blocked by a node that denies escalation.
            self._set_policy("fleet", "*", {"escalation": {"agents": ["claude"]}})
            self._set_policy("node", "master", {"escalation": {"agents": []}})
            r = self.c.post("/api/tasks", json={"agent": "claude", "prompt": "q", "mode": "read-only",
                                                 "worktree": False}, headers=W)
            self.assertEqual(r.status_code, 200, r.text)
            # The marker doesn't lift other limits (mode ceiling) either.
            self._set_policy("node", "master", {"escalation": {"agents": ["claude"]}, "mode_ceiling": "read-only"})
            r = self._escalation_task(mode="auto-edit")
            self.assertEqual(r.status_code, 409, r.text)
        finally:
            self._set_policy("fleet", "*", {})
            self._set_policy("node", "master", {})

    def test_trusted_bypass_needs_policy_and_auto_approves(self):
        r = self.c.post("/api/tasks", json={"agent": "claude", "prompt": "p", "trusted_bypass": True}, headers=W)
        self.assertEqual(r.status_code, 409, r.text)  # the node's ceiling doesn't allow full mode
        self.assertIn("ceiling", r.text)
        r = self.c.post("/api/tasks", json={"agent": "ollama", "model": "fake-model:1b", "prompt": "p",
                                             "trusted_bypass": True}, headers=W)
        self.assertEqual(r.status_code, 400)
        old = FakeRunner.max_mode
        FakeRunner.max_mode = "full"
        self.c.post("/api/refresh", headers=W)
        self.c.post("/api/nodes/master/ceiling", json={"mode": "full"}, headers=W)
        r = self.c.post("/api/tasks", json={"agent": "claude", "prompt": "p", "trusted_bypass": True}, headers=W)
        self.assertEqual(r.status_code, 403, r.text)  # ceiling allows it, but no policy pre-approves it
        r = self.c.post("/api/tasks", json={"agent": "claude", "prompt": "p", "trusted_bypass": True,
                                             "policy": {"auto_approve": {"agents": ["claude"], "full_mode": True}}}, headers=W)
        self.assertEqual(r.status_code, 403, r.text)  # a task can't pre-approve itself
        r = self.c.post("/api/tasks", json={"agent": "claude", "prompt": "p",
                                             "policy": {"auto_approve": {"agents": ["claude"], "denied_tools": True}}}, headers=W)
        self.assertEqual(r.status_code, 403, r.text)
        self.c.put("/api/policies/agent/claude", json={"body": {"auto_approve": {"agents": ["claude"], "full_mode": True}}}, headers=W)
        try:
            r = self.c.post("/api/tasks", json={"agent": "claude", "prompt": "p", "trusted_bypass": True}, headers=W)
            self.assertEqual(r.status_code, 200, r.text)
            t = r.json()
            self.assertEqual(t["mode"], "full")
            self.assertNotEqual(t["state"], "awaiting_approval")
            sent = [b for path, b in FakeRunner.calls if path == "/jobs" and b["id"] == t["id"]][0]
            self.assertEqual(sent["mode"], "full")
        finally:
            self.c.put("/api/policies/agent/claude", json={"body": {}}, headers=W)
            self.c.post("/api/nodes/master/ceiling", json={"mode": "auto-edit"}, headers=W)
            FakeRunner.max_mode = old
            self.c.post("/api/refresh", headers=W)

    def test_denied_tools_are_auto_approved_by_policy(self):
        self.c.put("/api/policies/agent/claude", json={"body": {"auto_approve": {"agents": ["claude"], "denied_tools": True}}}, headers=W)
        try:
            t = self.task(title="needs bash")
            FakeRunner.jobs[t["id"]].update(state="exited", exit_code=0, denials=[{"tool_name": "Bash"}])
            wait_for(lambda: [b for p_, b in FakeRunner.calls if p_ == f"/jobs/{t['id']}/message"])
            msg = [b for p_, b in FakeRunner.calls if p_ == f"/jobs/{t['id']}/message"][0]
            self.assertEqual(msg["allowed_tools"], ["Bash"])
            types = [e["type"] for e in self.c.get("/api/events/list?limit=1000").json() if e["task_id"] == t["id"]]
            self.assertIn("task.auto_approved", types)
        finally:
            self.c.put("/api/policies/agent/claude", json={"body": {}}, headers=W)

    def test_auto_approval_cap_resets_after_message(self):
        self.c.put("/api/policies/agent/claude",
                   json={"body": {"auto_approve": {"agents": ["claude"], "denied_tools": True}}}, headers=W)
        try:
            t = self.task()
            FakeRunner.jobs[t["id"]].update(state="exited", exit_code=0, denials=[{"tool_name": "Bash"}])
            wait_for(lambda: (current := self.c.get(f"/api/tasks/{t['id']}").json())["auto_approvals"] == 1
                     and current["state"] == "working")
            FakeRunner.jobs[t["id"]].update(state="exited", exit_code=1, denials=[])
            wait_for(lambda: self.state(t["id"]) == "failed")

            resumed = self.c.post(f"/api/tasks/{t['id']}/message", json={"text": "review feedback"}, headers=W).json()
            self.assertEqual(resumed["auto_approvals"], 0)
        finally:
            self.c.put("/api/policies/agent/claude", json={"body": {}}, headers=W)

    def test_auto_approval_cap_stops_fourth_consecutive_round(self):
        self.c.put("/api/policies/agent/claude",
                   json={"body": {"auto_approve": {"agents": ["claude"], "denied_tools": True}}}, headers=W)
        try:
            t = self.task()
            path = f"/jobs/{t['id']}/message"
            before = len([1 for called, _ in FakeRunner.calls if called == path])
            for round_number in range(1, tasks.AUTO_APPROVE_ROUNDS + 1):
                FakeRunner.jobs[t["id"]].update(state="exited", exit_code=0,
                                                denials=[{"tool_name": f"Tool{round_number}"}])
                wait_for(lambda n=round_number: (
                    (current := self.c.get(f"/api/tasks/{t['id']}").json())["auto_approvals"] == n
                    and current["state"] == "working"))

            FakeRunner.jobs[t["id"]].update(state="exited", exit_code=0, denials=[{"tool_name": "Tool4"}])
            wait_for(lambda: self.state(t["id"]) == "blocked")
            task = self.c.get(f"/api/tasks/{t['id']}").json()
            calls = [body for called, body in FakeRunner.calls if called == path]
            self.assertEqual(task["auto_approvals"], tasks.AUTO_APPROVE_ROUNDS)
            self.assertEqual(len(calls) - before, tasks.AUTO_APPROVE_ROUNDS)
            self.assertNotIn("Tool4", task["approved_tools"])
        finally:
            self.c.put("/api/policies/agent/claude", json={"body": {}}, headers=W)

    def test_validation_and_eligibility(self):
        bad = lambda body: self.c.post("/api/tasks", json=body, headers=W)  # noqa: E731
        self.assertEqual(bad({"agent": "claude", "prompt": "   "}).status_code, 422)
        r = bad({"agent": "claude", "prompt": "x", "mode": "read-only", "gate": "true"})
        self.assertEqual(r.status_code, 400)
        # runner reports codex: null (kind build: the budget rules, checked first, keep Codex off drafts)
        r = bad({"agent": "codex", "prompt": "x", "kind": "build"})
        self.assertEqual(r.status_code, 409)
        self.assertIn("no codex", r.text)
        r = bad({"agent": "ollama", "model": "nope:1b", "prompt": "x"})
        self.assertEqual(r.status_code, 409)

    def test_gate_profile_must_exist_on_node(self):
        r = self.c.post("/api/tasks", json={"agent": "claude", "prompt": "x", "gate": "nope"}, headers=W)
        self.assertEqual(r.status_code, 409)
        self.assertIn("gate profile nope", r.text)

    def test_per_node_runner_token(self):
        import subprocess
        ctl = Path(controller.__file__)
        out = subprocess.run([sys.executable, str(ctl), "runner-token", "set", "master"], input="tok-master-123\n",
                             capture_output=True, text=True, env=os.environ.copy(), timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertNotIn("tok-master-123", out.stdout + out.stderr)
        FakeRunner.tokens.clear()
        t = self.task()
        self.assertIn("tok-master-123", FakeRunner.tokens)
        for path in ("/api/fleet", f"/api/tasks/{t['id']}", "/api/events/list", "/api/usage"):
            self.assertNotIn("tok-master-123", self.c.get(path).text, path)
        out = subprocess.run([sys.executable, str(ctl), "runner-token", "clear", "master"],
                             capture_output=True, text=True, env=os.environ.copy(), timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        time.sleep(0.5)  # a background poll that read the old token just before the clear may still be in flight
        FakeRunner.tokens.clear()
        self.task()
        self.assertNotIn("tok-master-123", FakeRunner.tokens)

    # ---------- phase 6: instructions and policy ----------

    def test_policy_crud_versions_history_and_events(self):
        role = "p6-crud"
        first = self.c.put(f"/api/instructions/role/{role}", json={"text": "First."}, headers=W)
        second = self.c.put(f"/api/instructions/role/{role}", json={"text": "Second."}, headers=W)
        self.assertEqual((first.status_code, second.status_code), (200, 200))
        self.assertEqual((first.json()["version"], second.json()["version"]), (1, 2))
        current = [row for row in self.c.get("/api/instructions").json()
                   if row["scope"] == "role" and row["scope_id"] == role]
        self.assertEqual([(row["version"], row["text"]) for row in current], [(2, "Second.")])
        history = self.c.get(f"/api/instructions/role/{role}/history").json()
        self.assertEqual([(row["version"], row["text"]) for row in history], [(2, "Second."), (1, "First.")])

        bodies = [{"limits": {"task_minutes": 30}}, {"network": {"agents": "deny"}}]
        for body in bodies:
            response = self.c.put(f"/api/policies/role/{role}", json={"body": body}, headers=W)
            self.assertEqual(response.status_code, 200, response.text)
        current = [row for row in self.c.get("/api/policies").json()
                   if row["scope"] == "role" and row["scope_id"] == role]
        self.assertEqual([(row["version"], row["body"]) for row in current], [(2, bodies[1])])
        history = self.c.get(f"/api/policies/role/{role}/history").json()
        self.assertEqual([(row["version"], row["body"]) for row in history], [(2, bodies[1]), (1, bodies[0])])
        changed = [event for event in self.c.get("/api/events/list?limit=1000").json()
                   if event["type"] in ("instructions.changed", "policy.changed")
                   and event["data"].get("scope_id") == role]
        self.assertEqual([event["type"] for event in changed],
                         ["instructions.changed", "instructions.changed", "policy.changed", "policy.changed"])
        self.assertTrue(all(event["data"]["author"] == "local" for event in changed))

    def test_policy_preview(self):
        role = "p6-preview"
        self.c.put(f"/api/instructions/role/{role}", json={"text": "Review carefully."}, headers=W)
        self.c.put(f"/api/policies/role/{role}",
                   json={"body": {"limits": {"task_minutes": 30}, "network": {"agents": "deny"}}}, headers=W)
        response = self.c.post("/api/policies/preview", json={
            "node": "master", "role": role, "agent": "claude",
            "policy": {"limits": {"task_minutes": 10}},
        }, headers=W)
        self.assertEqual(response.status_code, 200, response.text)
        preview = response.json()
        self.assertEqual(preview["instructions"], "## role:p6-preview\nReview carefully.")
        self.assertEqual(preview["policy"]["limits"]["task_minutes"], 10)
        self.assertEqual(preview["policy"]["network"]["agents"], "deny")
        self.assertEqual(preview["sources"]["limits.task_minutes"], "task")
        self.assertEqual(len(preview["hash"]), 64)

    def test_policy_snapshot_sent_and_kept_on_message(self):
        role = "p6-snapshot"
        self.c.put(f"/api/instructions/role/{role}", json={"text": "Original instructions."}, headers=W)
        self.c.put(f"/api/policies/role/{role}",
                   json={"body": {"limits": {"task_minutes": 30}, "tools": {"claude_allow": ["Read"]}}},
                   headers=W)
        task = self.task(role=role, policy={"limits": {"task_minutes": 20}})
        saved = self.c.get(f"/api/tasks/{task['id']}").json()
        self.assertEqual(saved["role"], role)
        self.assertIn("Original instructions.", saved["instructions"])
        self.assertEqual(saved["policy"]["limits"]["task_minutes"], 20)
        self.assertEqual(len(saved["policy_hash"]), 64)
        sent = [body for path, body in FakeRunner.calls if path == "/jobs" and body["id"] == task["id"]][0]
        self.assertEqual(sent["instructions"], saved["instructions"])
        self.assertEqual(sent["policy"]["limits"]["task_minutes"], 20)
        self.assertEqual(sent["policy_hash"], saved["policy_hash"])

        self.c.put(f"/api/instructions/role/{role}", json={"text": "Changed later."}, headers=W)
        self.c.put(f"/api/policies/role/{role}", json={"body": {"limits": {"task_minutes": 1}}}, headers=W)
        FakeRunner.jobs[task["id"]].update(state="exited", exit_code=1)
        wait_for(lambda: self.state(task["id"]) == "failed")
        self.c.post(f"/api/tasks/{task['id']}/message", json={"text": "continue"}, headers=W)
        resumed = self.c.get(f"/api/tasks/{task['id']}").json()
        self.assertEqual((resumed["instructions"], resumed["policy"], resumed["policy_hash"]),
                         (saved["instructions"], saved["policy"], saved["policy_hash"]))
        self.c.post(f"/api/tasks/{task['id']}/cancel", headers=W)

    def test_policy_max_concurrent_queueing(self):
        role = "p6-concurrent"
        running = next(node["running_tasks"] for node in controller.tasks.node_rows() if node["id"] == "master")
        self.c.put(f"/api/policies/role/{role}",
                   json={"body": {"limits": {"max_concurrent": running + 1}}}, headers=W)
        first = self.task(role=role)
        self.assertEqual(first["state"], "working")
        second = self.task(role=role)
        self.assertEqual(second["state"], "queued")
        self.assertEqual(second["error"], "max concurrent tasks reached")
        self.assertNotIn(second["id"], FakeRunner.jobs)
        FakeRunner.jobs[first["id"]].update(state="exited", exit_code=0)
        wait_for(lambda: self.state(first["id"]) == "review")
        wait_for(lambda: self.state(second["id"]) == "working")
        self.c.post(f"/api/tasks/{second['id']}/cancel", headers=W)

    def test_policy_daily_budget_queueing(self):
        role = "p6-budget"
        self.c.put(f"/api/policies/role/{role}",
                   json={"body": {"limits": {"agent_minutes_per_day": 0.01}}}, headers=W)
        first = self.task(role=role)
        controller.tasks.update(first["id"], agent_seconds=1)
        second = self.task(role=role)
        self.assertEqual((second["state"], second["error"]), ("queued", "daily agent budget used"))
        self.assertNotIn(second["id"], FakeRunner.jobs)
        self.c.post(f"/api/tasks/{first['id']}/cancel", headers=W)
        self.c.post(f"/api/tasks/{second['id']}/cancel", headers=W)

    def test_policy_quiet_hours_uses_injected_clock(self):
        role = "p6-quiet"
        self.c.put(f"/api/policies/role/{role}",
                   json={"body": {"limits": {"quiet_hours": ["10:00-11:00"]}}}, headers=W)
        original = controller.tasks.local_minute
        try:
            controller.tasks.local_minute = lambda: 10 * 60 + 30
            task = self.task(role=role)
            self.assertEqual((task["state"], task["error"]), ("queued", "quiet hours"))
            self.assertNotIn(task["id"], FakeRunner.jobs)
            controller.tasks.local_minute = lambda: 12 * 60
            wait_for(lambda: self.state(task["id"]) == "working")
            self.c.post(f"/api/tasks/{task['id']}/cancel", headers=W)
        finally:
            controller.tasks.local_minute = original

    def test_policy_put_requires_admin_scope(self):
        remote = TestClient(controller.app, base_url="https://master.tail.ts.net:8701")
        view, operate, admin = (auth.create_token(scope, "phase6-" + scope)
                                for scope in ("view", "operate", "admin"))
        headers = lambda token: {"Authorization": f"Bearer {token}", **W}  # noqa: E731
        path = "/api/policies/role/p6-scope"
        self.assertEqual(remote.get("/api/policies", headers=headers(view)).status_code, 200)
        self.assertEqual(remote.post("/api/policies/preview", json={"node": "master", "agent": "claude"},
                                     headers=headers(view)).status_code, 200)
        self.assertEqual(remote.put(path, json={"body": {}}, headers=headers(operate)).status_code, 403)
        response = remote.put(path, json={"body": {}}, headers=headers(admin))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["author"], "phase6-admin")

    # ---------- phase 7: skills, evals and pipelines ----------

    def test_skills_crud_history_bindings_import_and_risk(self):
        skill_id = "phase7-review"
        first = self.c.put(f"/api/skills/{skill_id}", json={
            "description": "Review carefully", "agents": ["claude"],
            "risk": "prompt-only", "body": "Check every changed line.",
        }, headers=W)
        self.assertEqual(first.status_code, 200, first.text)
        second = self.c.put(f"/api/skills/{skill_id}", json={
            "description": "Review carefully", "agents": ["claude", "codex"],
            "risk": "prompt-only", "body": "Check every changed line and quote evidence.",
        }, headers=W)
        self.assertEqual(second.json()["version"], first.json()["version"] + 1)
        history = self.c.get(f"/api/skills/{skill_id}/history").json()
        self.assertEqual([row["version"] for row in history[:2]], [second.json()["version"], first.json()["version"]])
        self.assertNotEqual(history[0]["digest"], history[1]["digest"])

        binding = {"scope": "role", "scope_id": "phase7-reviewer"}
        self.assertEqual(self.c.post(f"/api/skills/{skill_id}/bindings", json=binding, headers=W).status_code, 200)
        current = next(row for row in self.c.get("/api/skills").json() if row["id"] == skill_id)
        self.assertIn(binding, current["bindings"])
        self.assertEqual(self.c.request("DELETE", f"/api/skills/{skill_id}/bindings", json=binding, headers=W).status_code, 200)

        refused = self.c.put("/api/skills/dangerous-skill", json={
            "description": "No", "agents": ["claude"], "risk": "executable", "body": "run it",
        }, headers=W)
        self.assertEqual(refused.status_code, 400)
        self.assertIn("not supported yet", refused.text)

        repo_dir = SKILLS_DIR / "repo-skill"
        repo_dir.mkdir(exist_ok=True)
        skill_file = repo_dir / "SKILL.md"
        skill_file.write_text("""---
id: repo-skill
version: 3
description: Imported instructions
agents: [claude, codex]
risk: prompt-only
---
Imported body.
""")
        imported = controller.fleet_skills.import_repo()
        self.assertEqual([(row["id"], row["version"], row["author"]) for row in imported],
                         [("repo-skill", 3, "repo")])
        self.assertEqual(controller.fleet_skills.import_repo(), [])
        skill_file.write_text(skill_file.read_text().replace("Imported body.", "Changed imported body."))
        changed = controller.fleet_skills.import_repo()
        self.assertEqual((changed[0]["version"], changed[0]["author"]), (4, "repo"))

    # ---------- G5: repo instructions and skills ----------

    @staticmethod
    def _forget_g5(skill_ids=(), instruction_scopes=()):
        with db() as c:
            for skill_id in skill_ids:
                c.execute("DELETE FROM skills WHERE id=?", (skill_id,))
                c.execute("DELETE FROM skill_bindings WHERE skill_id=?", (skill_id,))
            for scope, scope_id in instruction_scopes:
                c.execute("DELETE FROM instructions WHERE scope=? AND scope_id=?", (scope, scope_id))

    def test_g5_fleet_instruction_imported_once_never_over_operator(self):
        repo_text = controller.fleet_instructions.DEFAULT_DIR.joinpath("fleet.md").read_text().strip()
        self.assertLessEqual(len(repo_text.splitlines()), 8)
        self.assertNotIn("JSON", repo_text)
        root = TMP / "g5-instructions"
        (root / "role").mkdir(parents=True, exist_ok=True)
        (root / "fleet.md").write_text(repo_text + "\n")
        (root / "role" / "g5-role.md").write_text("Role seed.\n")
        scopes = [("fleet", "*"), ("role", "g5-role")]
        self._forget_g5(instruction_scopes=scopes)
        try:
            first = controller.fleet_instructions.import_repo(root)
            self.assertEqual([(r["scope"], r["scope_id"], r["version"], r["author"]) for r in first],
                             [("fleet", "*", 1, "repo"), ("role", "g5-role", 1, "repo")])
            self.assertEqual(first[0]["text"], repo_text)
            self.assertEqual(controller.fleet_instructions.import_repo(root), [])

            edited = self.c.put("/api/instructions/fleet/*", json={"text": "Operator wins."}, headers=W)
            self.assertEqual(edited.json()["version"], 2)
            (root / "fleet.md").write_text("A changed repo instruction.\n")
            self.assertEqual(controller.fleet_instructions.import_repo(root), [])
            history = self.c.get("/api/instructions/fleet/*/history").json()
            self.assertEqual([(r["version"], r["text"], r["author"]) for r in history],
                             [(2, "Operator wins.", "local"), (1, repo_text, "repo")])
        finally:
            self._forget_g5(instruction_scopes=scopes)

    def test_repo_instructions_validate_scope_id_like_the_api(self):
        root = TMP / "g5-instructions-scope"
        for scope in ("role", "agent", "node"):
            (root / scope).mkdir(parents=True, exist_ok=True)
        files = {("role", "g5-ok-role"): True, ("role", "Bad_Role"): False,
                 ("agent", "claude"): True, ("agent", "g5-nonagent"): False,
                 ("node", "g5-real-node"): True, ("node", "g5-ghost-node"): False}
        for (scope, scope_id) in files:
            (root / scope / f"{scope_id}.md").write_text("Seed.\n")
        with db() as c:
            c.execute("INSERT OR IGNORE INTO nodes (id,name,status) VALUES ('g5-real-node','g5-real-host','pending')")
        scopes = list(files)
        self._forget_g5(instruction_scopes=scopes)
        try:
            imported = controller.fleet_instructions.import_repo(root)
            self.assertCountEqual([(r["scope"], r["scope_id"]) for r in imported],
                                  [key for key, ok in files.items() if ok])
            with db() as c:
                stored = {(r[0], r[1]) for r in c.execute("SELECT scope,scope_id FROM instructions")}
            for key, ok in files.items():
                self.assertEqual(key in stored, ok, key)
        finally:
            self._forget_g5(instruction_scopes=scopes)
            with db() as c:
                c.execute("DELETE FROM nodes WHERE id='g5-real-node'")

    def test_g5_skill_bind_front_matter_applies_on_first_import_only(self):
        root = TMP / "g5-skills"
        (root / "g5-bound").mkdir(parents=True, exist_ok=True)
        (root / "g5-bad-bind").mkdir(exist_ok=True)
        skill_file = root / "g5-bound" / "SKILL.md"
        skill_file.write_text("---\nid: g5-bound\ndescription: Bound\nagents: [claude]\nrisk: prompt-only\n"
                              "bind: [role:g5-bound-role, node:g5-node]\n---\nFirst body.\n")
        (root / "g5-bad-bind" / "SKILL.md").write_text(
            "---\nid: g5-bad-bind\ndescription: Bad\nagents: [claude]\nrisk: prompt-only\nbind: [nowhere]\n---\nBody.\n")
        self._forget_g5(skill_ids=("g5-bound", "g5-bad-bind"))
        try:
            role = {"scope": "role", "scope_id": "g5-bound-role"}
            node = {"scope": "node", "scope_id": "g5-node"}
            first = controller.fleet_skills.import_repo(root)
            self.assertEqual([row["id"] for row in first], ["g5-bound"])  # an invalid bind rejects the skill
            self.assertEqual(first[0]["bindings"], [{"skill_id": "g5-bound", **role}, {"skill_id": "g5-bound", **node}])
            current = next(row for row in self.c.get("/api/skills").json() if row["id"] == "g5-bound")
            self.assertCountEqual(current["bindings"], [role, node])

            self.assertEqual(self.c.request("DELETE", "/api/skills/g5-bound/bindings", json=role,
                                            headers=W).status_code, 200)
            skill_file.write_text(skill_file.read_text().replace("First body.", "Second body."))
            second = controller.fleet_skills.import_repo(root)
            self.assertEqual([(row["id"], row["version"], row["bindings"]) for row in second],
                             [("g5-bound", 2, [])])
            current = next(row for row in self.c.get("/api/skills").json() if row["id"] == "g5-bound")
            self.assertEqual(current["bindings"], [node])
        finally:
            self._forget_g5(skill_ids=("g5-bound", "g5-bad-bind"))

    def test_g5_first_import_commits_skill_and_bindings_together(self):
        root = TMP / "g5-atomic"
        (root / "g5-atomic").mkdir(parents=True, exist_ok=True)
        (root / "g5-atomic" / "SKILL.md").write_text(
            "---\nid: g5-atomic\ndescription: Atomic\nagents: [claude]\nrisk: prompt-only\n"
            "bind: [role:g5-atomic-a, role:g5-atomic-b]\n---\nBody.\n")
        self._forget_g5(skill_ids=("g5-atomic",))
        with db() as c:  # interrupt the first import after the skill row and the first binding
            c.execute("CREATE TRIGGER g5_interrupt BEFORE INSERT ON skill_bindings "
                      "WHEN NEW.scope_id='g5-atomic-b' BEGIN SELECT RAISE(ABORT, 'interrupted'); END")
        try:
            self.assertEqual(controller.fleet_skills.import_repo(root), [])
            with db() as c:
                self.assertIsNone(c.execute("SELECT 1 FROM skills WHERE id='g5-atomic'").fetchone())
                self.assertIsNone(c.execute("SELECT 1 FROM skill_bindings WHERE skill_id='g5-atomic'").fetchone())
                c.execute("DROP TRIGGER g5_interrupt")
            retried = controller.fleet_skills.import_repo(root)
            self.assertEqual([(r["version"], [b["scope_id"] for b in r["bindings"]]) for r in retried],
                             [(1, ["g5-atomic-a", "g5-atomic-b"])])
            current = next(row for row in self.c.get("/api/skills").json() if row["id"] == "g5-atomic")
            self.assertCountEqual(current["bindings"], [{"scope": "role", "scope_id": "g5-atomic-a"},
                                                        {"scope": "role", "scope_id": "g5-atomic-b"}])
        finally:
            with db() as c:
                c.execute("DROP TRIGGER IF EXISTS g5_interrupt")
            self._forget_g5(skill_ids=("g5-atomic",))

    def test_g5_bind_applies_on_first_repo_import_after_operator_version(self):
        root = TMP / "g5-operator-first"
        (root / "g5-operator").mkdir(parents=True, exist_ok=True)
        (root / "g5-operator" / "SKILL.md").write_text(
            "---\nid: g5-operator\ndescription: Repo\nagents: [claude]\nrisk: prompt-only\n"
            "bind: [role:g5-operator-role]\n---\nRepo body.\n")
        self._forget_g5(skill_ids=("g5-operator",))
        try:
            made = self.c.put("/api/skills/g5-operator", json={
                "description": "Operator", "agents": ["claude"], "risk": "prompt-only", "body": "Operator body.",
            }, headers=W)
            self.assertEqual(made.status_code, 200, made.text)
            role = {"scope": "role", "scope_id": "g5-operator-role"}
            imported = controller.fleet_skills.import_repo(root)
            self.assertEqual([(r["version"], r["author"], r["bindings"]) for r in imported],
                             [(2, "repo", [{"skill_id": "g5-operator", **role}])])
            self.c.request("DELETE", "/api/skills/g5-operator/bindings", json=role, headers=W)
            (root / "g5-operator" / "SKILL.md").write_text(
                (root / "g5-operator" / "SKILL.md").read_text().replace("Repo body.", "Repo body two."))
            again = controller.fleet_skills.import_repo(root)
            self.assertEqual([(r["version"], r["bindings"]) for r in again], [(3, [])])
        finally:
            self._forget_g5(skill_ids=("g5-operator",))

    def test_g5_repo_skill_files_parse_and_validate(self):
        expected = {"grounded-answer": ["role:fast", "agent:ollama"], "reviewer": ["role:reviewer"]}
        for skill_id, binds in expected.items():
            with self.subTest(skill=skill_id):
                path = controller.fleet_skills.DEFAULT_DIR / skill_id / "SKILL.md"
                meta, body = controller.fleet_skills._frontmatter(path.read_text(encoding="utf-8"))
                self.assertEqual(meta["id"], skill_id)
                controller.fleet_skills.validate(skill_id, meta["description"], meta["agents"], meta["risk"], body)
                self.assertEqual(meta["bind"], binds)
                self.assertEqual(controller.fleet_skills._bindings(meta["bind"]),
                                 [tuple(b.split(":", 1)) for b in binds])
                self.assertTrue(15 <= len(body.strip().splitlines()) <= 30, len(body.strip().splitlines()))
        reviewer = (controller.fleet_skills.DEFAULT_DIR / "reviewer" / "SKILL.md").read_text()
        examples = [json.loads(line) for line in reviewer.splitlines()
                    if line.startswith('{"verdict":"') and "|" not in line]  # skip the schema line
        self.assertEqual([e["verdict"] for e in examples], ["request_changes", "abstain"])
        issue = examples[0]["issues"][0]
        self.assertEqual(set(issue), {"severity", "file", "problem", "evidence", "expected", "actual", "fix"})
        self.assertIn(issue["evidence"], reviewer.replace('\\"', '"'))
        self.assertNotEqual(issue["fix"], issue["evidence"])
        self.assertEqual(examples[1]["issues"], [])

    def test_g5_reviewer_preview_includes_skill_and_fleet_instruction(self):
        skill_ids, scopes = ("grounded-answer", "reviewer"), [("fleet", "*")]
        self._forget_g5(skill_ids=skill_ids, instruction_scopes=scopes)
        try:
            controller.fleet_skills.import_repo(controller.fleet_skills.DEFAULT_DIR)
            controller.fleet_instructions.import_repo(controller.fleet_instructions.DEFAULT_DIR)
            preview = self.c.post("/api/policies/preview", json={
                "node": "master", "agent": "claude", "role": "reviewer"}, headers=W)
            self.assertEqual(preview.status_code, 200, preview.text)
            preview = preview.json()
            self.assertEqual([s["id"] for s in preview["skills"]], ["reviewer"])
            self.assertIn("## skill: reviewer v1", preview["instructions"])
            self.assertTrue(preview["instructions"].startswith("## fleet\n- Say \"I don't know\""))
            fast = self.c.post("/api/policies/preview", json={
                "node": "master", "agent": "ollama", "role": "fast"}, headers=W).json()
            self.assertEqual([s["id"] for s in fast["skills"]], ["grounded-answer"])
        finally:
            self._forget_g5(skill_ids=skill_ids, instruction_scopes=scopes)

    def test_skill_snapshot_filters_agents_and_records_versions(self):
        skill_id = "phase7-snapshot"
        created = self.c.put(f"/api/skills/{skill_id}", json={
            "description": "Snapshot", "agents": ["claude"], "risk": "prompt-only",
            "body": "Use the phase seven checklist.",
        }, headers=W).json()
        task = self.task(skills=[skill_id])
        saved = self.c.get(f"/api/tasks/{task['id']}").json()
        self.assertIn(f"## skill: {skill_id} v{created['version']}", saved["instructions"])
        self.assertEqual(saved["skills"], [{"id": skill_id, "version": created["version"],
                                             "digest": created["digest"]}])
        sent = [body for path, body in FakeRunner.calls if path == "/jobs" and body["id"] == task["id"]][0]
        self.assertEqual(sent["instructions"], saved["instructions"])

        binding = {"scope": "role", "scope_id": "phase7-skilled"}
        self.c.post(f"/api/skills/{skill_id}/bindings", json=binding, headers=W)
        bound = self.task(role="phase7-skilled")
        self.assertIn("Use the phase seven checklist.", bound["instructions"])
        preview = self.c.post("/api/policies/preview", json={
            "node": "master", "agent": "claude", "role": "phase7-skilled",
        }, headers=W).json()
        self.assertEqual(preview["skills"][0]["id"], skill_id)
        self.c.request("DELETE", f"/api/skills/{skill_id}/bindings", json=binding, headers=W)

        other = self.c.put("/api/skills/ollama-only", json={
            "description": "Only local models", "agents": ["ollama"], "risk": "prompt-only", "body": "Local only.",
        }, headers=W).json()
        filtered = self.task(skills=[other["id"]])
        filtered_saved = self.c.get(f"/api/tasks/{filtered['id']}").json()
        self.assertNotIn("Local only.", filtered_saved["instructions"])
        self.assertEqual(filtered_saved["skills"], [])
        self.c.post(f"/api/tasks/{task['id']}/cancel", headers=W)
        self.c.post(f"/api/tasks/{bound['id']}/cancel", headers=W)
        self.c.post(f"/api/tasks/{filtered['id']}/cancel", headers=W)

    def test_evals_queue_scores_catalog_and_recommendations(self):
        bad = self.c.post("/api/evals", json={"model": "not-installed:1b", "node": "master"}, headers=W)
        self.assertEqual(bad.status_code, 409)
        first = self.c.post("/api/evals", json={"model": "fake-model:1b", "node": "master",
                                                  "roles": ["coder", "reviewer"]}, headers=W).json()
        second = self.c.post("/api/evals", json={"model": "fake-model:1b", "node": "master",
                                                   "roles": ["fast"]}, headers=W).json()

        def states():
            rows = {row["id"]: row for row in self.c.get("/api/evals").json()["runs"]}
            return rows[first["id"]]["state"], rows[second["id"]]["state"]

        wait_for(lambda: "running" in states())
        self.assertEqual(states().count("running"), 1)
        self.assertIn("queued", states())
        wait_for(lambda: states() == ("done", "done"), timeout=20)
        payload = self.c.get("/api/evals").json()
        done = {row["id"]: row for row in payload["runs"]}
        self.assertEqual(done[first["id"]]["scores"], {"coder": 91.0, "reviewer": 91.0})
        self.assertTrue(any(row["model"] == "fake-model:1b" for row in payload["leaderboard"]))
        catalog = next(row for row in self.c.get("/api/models").json() if row["reference"] == "fake-model:1b")
        self.assertEqual(catalog["evals"], {"fast": 91.0})
        recs = self.c.get("/api/evals/recommendations").json()
        self.assertTrue(any(row == {"node": "master", "role": "fast", "model": "fake-model:1b",
                                        "score": 91.0, "installed": True} for row in recs))

    def test_pipeline_end_to_end_resolution_ready_escalation_cancel_and_events(self):
        original_call = controller.pipeline_model_call
        FakeRunner.agents["codex"] = "/x/codex"
        self.c.post("/api/refresh", headers=W)
        issue = {"severity": "high", "file": "app.py", "problem": "Greeting regression",
                 "evidence": "hello", "expected": "the expected greeting",
                 "actual": "the greeting is hello", "fix": "restore the expected greeting"}
        answers = [json.dumps({"verdict": "request_changes", "issues": [issue]}),
                   json.dumps({"verdict": "approve", "issues": []})]
        controller.pipeline_model_call = lambda role, spec, diff: answers.pop(0)

        def settle(pipeline_id, ready):
            # Drive the pipeline ourselves (see settle_pipeline) instead of waiting for the background pollers.
            return settle_pipeline(self.c, pipeline_id, ready)
        # The default budget rules keep Codex for security and final reviews (one round); this pipeline exercises
        # Codex as an ordinary two-round reviewer, so its rules allow that.
        import budget
        rules = json.loads(json.dumps(budget.DEFAULT_RULES))
        rules["models"]["codex"]["kinds"] = ["*"]
        rules["models"]["claude:*"]["max_concurrent"] = 10000  # earlier tests leave unfinished Claude tasks running
        rules["reviews"]["max_codex_rounds"] = 2
        self.assertEqual(self.c.put("/api/budget/rules", json={"body": rules}, headers=W).status_code, 200)
        try:
            body = {"title": "Phase 7 pipeline", "spec": "Keep the greeting correct", "repo": "/repo",
                    "gate": "pytest", "mode": "auto-edit", "builder": {"agent": "claude"},
                    "reviewers": [{"kind": "role", "role": "reviewer"},
                                  {"kind": "agent", "agent": "codex"}], "max_rounds": 2}
            response = self.c.post("/api/pipelines", json=body, headers=W)
            self.assertEqual(response.status_code, 200, response.text)
            pipeline_id = response.json()["id"]
            build_id = response.json()["build_task"]
            FakeRunner.jobs[build_id].update(state="exited", exit_code=0, result="built",
                                             gate={"state": "passed", "exit_code": 0})
            reviewing = settle(pipeline_id, lambda p: p if p["state"] == "reviewing" and
                               p["reviews"].get("1", {}).get("task_id") else None)
            review_id = reviewing["reviews"]["1"]["task_id"]
            review_body = [sent for path, sent in FakeRunner.calls if path == "/jobs" and sent["id"] == review_id][0]
            # Read-only, inside the build's own worktree (so any file it reads is current), not the original repo.
            self.assertEqual((review_body["mode"], review_body["worktree"], review_body["repo"], review_body["cwd"]),
                             ("read-only", False, None, "/w"))
            FakeRunner.jobs[review_id].update(state="exited", exit_code=0,
                                              result=json.dumps({"verdict": "request_changes", "issues": [issue]}))
            building = settle(pipeline_id, lambda p: p if p["state"] == "building" and p["round"] == 2 else None)
            # The state is saved before the controller sends the builder its message; wait for the message so it
            # can't land after (and undo) the job update below.
            wait_for(lambda: any(path == f"/jobs/{build_id}/message" for path, _ in FakeRunner.calls))
            messages = [sent["text"] for path, sent in FakeRunner.calls if path == f"/jobs/{build_id}/message"]
            self.assertEqual(messages[-1].count("Greeting regression"), 1)

            FakeRunner.jobs[build_id].update(state="exited", exit_code=0, result="fixed",
                                             gate={"state": "passed", "exit_code": 0})
            second_review = settle(pipeline_id, lambda p: p if p["state"] == "reviewing" and
                                   p["reviews"].get("1", {}).get("task_id") else None)
            second_review_id = second_review["reviews"]["1"]["task_id"]
            FakeRunner.jobs[second_review_id].update(
                state="exited", exit_code=0, result=json.dumps({"verdict": "approve", "issues": []}))
            ready = settle(pipeline_id, lambda p: p if p["state"] == "ready" else None)
            self.assertEqual(ready["build_task"], build_id)

            controller.pipeline_model_call = lambda role, spec, diff: json.dumps(
                {"verdict": "request_changes", "issues": [issue]})
            escalated = self.c.post("/api/pipelines", json={**body, "title": "Escalate", "reviewers": [
                {"kind": "role", "role": "reviewer"}], "max_rounds": 1}, headers=W).json()
            FakeRunner.jobs[escalated["build_task"]].update(state="exited", exit_code=0,
                                                            gate={"state": "passed", "exit_code": 0})
            settle(escalated["id"], lambda p: p["state"] == "escalated")

            cancelled = self.c.post("/api/pipelines", json={**body, "title": "Cancel me",
                "reviewers": [{"kind": "role", "role": "reviewer"}]}, headers=W).json()
            cancelled = self.c.post(f"/api/pipelines/{cancelled['id']}/cancel", headers=W).json()
            self.assertEqual(cancelled["state"], "cancelled")
            refused = self.c.post("/api/pipelines", json={**body,
                "reviewers": [{"kind": "agent", "agent": "claude"}]}, headers=W)
            self.assertEqual(refused.status_code, 400)
            types = [event["type"] for event in self.c.get("/api/events/list?limit=1000").json()
                     if event["data"].get("id") == pipeline_id]
            self.assertIn("pipeline.reviewing", types)
            self.assertIn("pipeline.resolving", types)
            self.assertIn("pipeline.ready", types)
        finally:
            controller.pipeline_model_call = original_call
            FakeRunner.agents["codex"] = None
            unmetered_claude_rules()  # back to this suite's rules
            self.c.post("/api/refresh", headers=W)

    def test_agent_reviewer_runs_read_only_in_the_build_worktree(self):
        # docs/SPEC-paid-budget.md 3.4 replaced "review in the original repo": that tree holds the code before the
        # change, so any file the reviewer read there was stale. It now runs read-only in the build's worktree.
        FakeRunner.agents["codex"] = "/x/codex"
        self.c.post("/api/refresh", headers=W)
        try:
            body = {"title": "Review placement", "spec": "do it", "repo": "/r/project", "gate": "pytest", "mode": "auto-edit",
                    "builder": {"agent": "codex"}, "reviewers": [{"kind": "agent", "agent": "claude"}], "max_rounds": 1}
            p = self.c.post("/api/pipelines", json=body, headers=W).json()
            build = p["build_task"]
            FakeRunner.jobs[build].update(state="exited", exit_code=0, gate={"state": "passed", "exit_code": 0})
            wait_for(lambda: self.c.get(f"/api/pipelines/{p['id']}").json()["reviews"].get("0", {}).get("task_id"), timeout=20)
            rid = self.c.get(f"/api/pipelines/{p['id']}").json()["reviews"]["0"]["task_id"]
            sent = [b for path, b in FakeRunner.calls if path == "/jobs" and b["id"] == rid][0]
            self.assertEqual((sent["repo"], sent["cwd"], sent["worktree"]), (None, "/w", False))
            self.assertEqual(sent["mode"], "read-only")
            self.assertIn("Review only the diff below.", sent["prompt"])
            self.assertNotIn("Read surrounding files", sent["prompt"])
            self.assertIn('"verdict":"approve|request_changes|abstain"', sent["prompt"])
            self.assertIn('"expected":"","actual":""', sent["prompt"])
            self.c.post(f"/api/pipelines/{p['id']}/cancel", headers=W)
        finally:
            FakeRunner.agents["codex"] = None
            self.c.post("/api/refresh", headers=W)

    def test_pipeline_loop_isolates_a_failing_pipeline(self):
        p1 = self.c.post("/api/pipelines", json={
            "title": "A", "spec": "x", "repo": "/r/a", "builder": {"agent": "claude"}, "max_rounds": 1,
            "reviewers": [{"kind": "agent", "agent": "claude", "model": "claude-fable-5-1"}]}, headers=W).json()
        p2 = self.c.post("/api/pipelines", json={
            "title": "B", "spec": "x", "repo": "/r/b", "builder": {"agent": "claude"}, "max_rounds": 1,
            "reviewers": [{"kind": "agent", "agent": "claude", "model": "claude-fable-5-1"}]}, headers=W).json()
        seen = []
        real_poll, real_secs = controller.poll_pipeline, controller.POLL_SECONDS

        async def flaky(pipeline_id):
            seen.append(pipeline_id)
            if pipeline_id == p1["id"]:
                raise RuntimeError("boom")

        async def run_loop():
            controller.poll_pipeline, controller.POLL_SECONDS = flaky, 0.01
            task = asyncio.create_task(controller.pipeline_loop())
            deadline = time.monotonic() + 5
            while p2["id"] not in seen and time.monotonic() < deadline:
                await asyncio.sleep(0.01)
            task.cancel()
        try:
            self.c.portal.call(run_loop)
        finally:
            controller.poll_pipeline, controller.POLL_SECONDS = real_poll, real_secs
            for p in (p1, p2):
                self.c.post(f"/api/pipelines/{p['id']}/cancel", headers=W)
        self.assertIn(p1["id"], seen)
        self.assertIn(p2["id"], seen)

    def test_agent_reviewer_model_is_passed_to_the_review_task(self):
        body = {"title": "Model review", "spec": "do it", "repo": "/r/project", "gate": "pytest", "mode": "auto-edit",
                "builder": {"agent": "claude"}, "max_rounds": 1,
                "reviewers": [{"kind": "agent", "agent": "claude", "model": "claude-fable-5-1"}]}
        refused = self.c.post("/api/pipelines", json=dict(
            body, reviewers=[{"kind": "agent", "agent": "claude"}]), headers=W)
        self.assertEqual(refused.status_code, 400)
        for bad in (["x"], "", 7):
            r = self.c.post("/api/pipelines", json=dict(body, builder={"agent": "claude", "model": bad}), headers=W)
            self.assertEqual(r.status_code, 400 if bad != "" else 200, bad)
            if r.status_code == 200:
                self.assertNotIn("model", r.json()["builder"])
                self.c.post(f"/api/pipelines/{r.json()['id']}/cancel", headers=W)
        p = self.c.post("/api/pipelines", json=body, headers=W).json()
        try:
            self.assertEqual(p["reviewers"], body["reviewers"])
            FakeRunner.jobs[p["build_task"]].update(state="exited", exit_code=0, gate={"state": "passed", "exit_code": 0})
            for _ in range(200):
                current = self.c.get(f"/api/pipelines/{p['id']}").json()
                if current["reviews"].get("0", {}).get("task_id"):
                    break
                self.c.portal.call(controller.tasks.poll_task, p["build_task"])
                self.c.portal.call(controller.poll_pipeline, p["id"])
            rid = current["reviews"]["0"]["task_id"]
            self.assertEqual(controller.tasks.get_task(rid)["model"], "claude-fable-5-1")
            sent = [b for path, b in FakeRunner.calls if path == "/jobs" and b["id"] == rid][0]
            self.assertEqual(sent["model"], "claude-fable-5-1")
        finally:
            self.c.post(f"/api/pipelines/{p['id']}/cancel", headers=W)

    def test_trusted_reviewer_threshold(self):
        import evaluations

        def row(precision, recall, split="holdout"):
            return {"model": "m", "roles": {"reviewer_real": {"score": 50.0, "metrics": {
                split: {"precision": precision, "recall": recall}}}}}
        self.assertTrue(evaluations.is_trusted_reviewer(row(0.9, 0.5)))
        self.assertTrue(evaluations.is_trusted_reviewer(row(1.0, 1.0)))
        self.assertFalse(evaluations.is_trusted_reviewer(row(0.89, 1.0)))
        self.assertFalse(evaluations.is_trusted_reviewer(row(1.0, 0.49)))
        self.assertFalse(evaluations.is_trusted_reviewer(row(None, 1.0)))  # raised nothing: no evidence
        self.assertFalse(evaluations.is_trusted_reviewer(row(1.0, 1.0, split="dev")))  # holdout decides
        self.assertFalse(evaluations.is_trusted_reviewer({"model": "m", "roles": {"reviewer": {"score": 100}}}))
        self.assertFalse(evaluations.is_trusted_reviewer({"model": "m"}))
        original = os.environ.get("FLEET_TRUSTED_PRECISION")
        try:
            os.environ["FLEET_TRUSTED_PRECISION"] = "0.8"
            self.assertTrue(evaluations.is_trusted_reviewer(row(0.85, 0.5)))
            os.environ["FLEET_TRUSTED_PRECISION"] = "0.95"
            self.assertFalse(evaluations.is_trusted_reviewer(row(0.9, 0.5)))
        finally:
            if original is None:
                os.environ.pop("FLEET_TRUSTED_PRECISION", None)
            else:
                os.environ["FLEET_TRUSTED_PRECISION"] = original
        # The newest reviewer_real evaluation per model decides, by that role's own timestamp; "x" == "x:latest".
        def stamped(model, precision, role_at, row_at, other=None):
            roles = {"reviewer_real": {"metrics": {"holdout": {"precision": precision, "recall": 1.0}},
                                       "evaluated_at": role_at}}
            return {"model": model, "evaluated_at": row_at, "roles": roles | (other or {})}
        rows = [stamped("x", 1.0, 100, 100), stamped("x:latest", 0.2, 200, 200)]
        self.assertEqual(evaluations.trusted_reviewer_references(rows), set())
        self.assertEqual(evaluations.trusted_reviewer_references(rows[::-1]), set())
        rows = [stamped("x", 0.2, 100, 100), stamped("x:latest", 1.0, 200, 200)]
        self.assertEqual(evaluations.trusted_reviewer_references(rows), {"x:latest"})
        # A later run of another role (fresher row timestamp) doesn't make a stale reviewer_real result newest.
        rows = [stamped("y:latest", 0.2, 300, 300),
                stamped("y", 1.0, 100, 900, other={"fast": {"score": 90.0, "evaluated_at": 900}})]
        self.assertEqual(evaluations.trusted_reviewer_references(rows), set())
        legacy = {"model": "z:1b", "evaluated_at": 50, "roles": {"reviewer_real": {
            "metrics": {"holdout": {"precision": 1.0, "recall": 1.0}}}}}  # no per-role stamp: row time
        self.assertEqual(evaluations.trusted_reviewer_references([legacy]), {"z:1b"})
        self.assertEqual(evaluations.trusted_reviewer_references([legacy, stamped("z:1b", 0.1, 60, 60)]), set())
        self.assertEqual(evaluations.trusted_reviewer_references([{"model": "w", "roles": {"fast": {}}}]), set())

        catalog = [{"id": "good-1b", "reference": "good:1b"}, {"id": "bad-1b", "reference": "bad:1b"}]
        assignments = [{"role": "r1", "model": "good:1b", "catalog_id": "good-1b"},
                       {"role": "r2", "model": "good:1b", "catalog_id": "good-1b"},
                       {"role": "r2", "model": "bad:1b", "catalog_id": "bad-1b"},
                       {"role": "r3", "model": "good", "catalog_id": None}]
        self.assertEqual(evaluations.trusted_roles(assignments, catalog, {"good:1b", "good:latest"}), ["r1", "r3"])
        self.assertEqual(evaluations.trusted_roles([], catalog, {"good:1b"}), [])

    def test_trust_follows_the_routed_model_not_the_catalog(self):
        import evaluations
        trusted = {"good:1b"}
        catalog = [{"id": "good-1b", "reference": "good:1b"}, {"id": "bad-1b", "reference": "bad:1b"},
                   {"id": "relabelled", "reference": "good:1b"}]

        def roles(*assignments):
            return evaluations.trusted_roles([{"role": "r", **a} for a in assignments], catalog, trusted)
        # Matching catalog_id/model pair with a trusted model: trusted.
        self.assertEqual(roles({"model": "good:1b", "catalog_id": "good-1b", "params": "{}"}), ["r"])
        # Derived (params) assignment of a trusted base model routes fleet-good-1b, a different model that was never
        # benchmarked: the base model's trust doesn't carry over.
        self.assertEqual(roles({"model": "good:1b", "catalog_id": "good-1b", "params": '{"temperature": 0.1}'}), [])
        self.assertEqual(roles({"model": "good:1b", "catalog_id": "good-1b", "params": "not json"}), [])
        self.assertEqual(roles({"model": "good:1b", "catalog_id": "good-1b", "params": "[1]"}), [])
        # Empty or missing settings are the base model itself.
        self.assertEqual(roles({"model": "good:1b", "catalog_id": "good-1b", "params": None}), ["r"])
        self.assertEqual(roles({"model": "good:1b", "catalog_id": "good-1b", "params": {}}), ["r"])
        # Mismatched pair: the catalog row says a trusted model, but bad:1b is what's assigned and routed.
        self.assertEqual(roles({"model": "bad:1b", "catalog_id": "relabelled", "params": "{}"}), [])
        self.assertEqual(roles({"model": "bad:1b", "catalog_id": "relabelled", "params": '{"num_ctx": 4096}'}), [])
        # ...and the other way round: a trusted model filed under an untrusted catalog row is still a mismatch.
        self.assertEqual(roles({"model": "good:1b", "catalog_id": "bad-1b", "params": "{}"}), [])
        # Missing catalog row: untrusted even though the assigned model is trusted.
        self.assertEqual(roles({"model": "good:1b", "catalog_id": "gone", "params": "{}"}), [])
        self.assertEqual(roles({"model": "good:1b", "catalog_id": "gone", "params": '{"temperature": 0}'}), [])
        # One bad assignment poisons the role; a missing model is untrusted.
        self.assertEqual(roles({"model": "good:1b", "catalog_id": "good-1b"},
                               {"model": "bad:1b", "catalog_id": "relabelled"}), [])
        self.assertEqual(roles({"model": None, "catalog_id": "good-1b"}), [])
        # Untagged and tagged forms of the same model are a match, not a mismatch.
        self.assertEqual(evaluations.trusted_roles(
            [{"role": "r", "model": "plain", "catalog_id": "p"}], [{"id": "p", "reference": "plain:latest"}],
            {"plain:latest"}), ["r"])

    def test_trust_follows_the_concrete_routed_model(self):
        import evaluations

        def result(model, precision, at=100):
            return {"model": model, "evaluated_at": at, "roles": {"reviewer_real": {"metrics": {"holdout": {
                "precision": precision, "recall": 1.0}}}}}
        catalog = [{"id": "good-1b", "reference": "good:1b"}]
        base = {"role": "r", "model": "good:1b", "catalog_id": "good-1b", "params": "{}"}
        derived = dict(base, params=json.dumps({"temperature": 0.1, "num_ctx": 4096}))
        self.assertEqual(evaluations.routed_model(base, {"good-1b": "good:1b"}), "good:1b")
        self.assertEqual(evaluations.routed_model(derived, {"good-1b": "good:1b"}), "fleet-good-1b")

        # Only the base model was benchmarked: the base assignment is trusted, the derived one isn't.
        refs = evaluations.trusted_reviewer_references([result("good:1b", 1.0)])
        self.assertEqual(refs, {"good:1b"})
        self.assertEqual(evaluations.trusted_roles([base], catalog, refs), ["r"])
        self.assertEqual(evaluations.trusted_roles([derived], catalog, refs), [])
        # Benchmarking the model that actually serves the role (run_evals --models fleet-good-1b): trusted. The
        # evaluator records the untagged name it was given; it's the same model as fleet-good-1b:latest.
        refs = evaluations.trusted_reviewer_references([result("fleet-good-1b", 1.0)])
        self.assertEqual(refs, {"fleet-good-1b:latest"})
        self.assertEqual(evaluations.trusted_roles([derived], catalog, refs), ["r"])
        # ...and the derived model's results don't stand in for the base model.
        self.assertEqual(evaluations.trusted_roles([base], catalog, refs), [])
        # A failing or newer failing evaluation of the derived model: untrusted, whatever the base scored.
        refs = evaluations.trusted_reviewer_references([result("good:1b", 1.0), result("fleet-good-1b", 0.5)])
        self.assertEqual(evaluations.trusted_roles([derived], catalog, refs), [])
        refs = evaluations.trusted_reviewer_references([result("fleet-good-1b", 1.0, at=100),
                                                        result("fleet-good-1b:latest", 0.2, at=200)])
        self.assertEqual(evaluations.trusted_roles([derived], catalog, refs), [])
        # Settings without a catalog model aren't a benchmarked model: untrusted even if the base is trusted.
        refs = {"good:1b", "fleet-good-1b:latest"}
        self.assertEqual(evaluations.trusted_roles([dict(derived, catalog_id=None)], catalog, refs), [])
        # The derived name comes from the catalog_id only when that row matches the assigned model.
        self.assertEqual(evaluations.trusted_roles(
            [dict(derived, catalog_id="good-1b", model="bad:1b")], catalog, refs), [])

    def test_trust_uses_exact_precision_not_a_rounded_one(self):
        import evaluations

        def row(**holdout):
            return {"model": "m", "roles": {"reviewer_real": {"metrics": {"holdout": holdout}}}}
        # 8999/10000 = 0.8999 rounds to 0.900 at 3 places; a row stored that way must still not pass a 0.9 bar.
        just_under = {"matched": 8999, "serious": 10000, "found": 5, "bug_cases": 5}
        self.assertFalse(evaluations.is_trusted_reviewer(row(precision=0.9, recall=1.0, **just_under), 0.9))
        self.assertTrue(evaluations.is_trusted_reviewer(row(precision=0.9, recall=1.0, matched=9, serious=10,
                                                            found=5, bug_cases=5), 0.9))
        # Recall comes from counts too: 4999/10000 < 0.5 even though it would round to 0.500.
        self.assertFalse(evaluations.is_trusted_reviewer(row(precision=1.0, recall=0.5, matched=1, serious=1,
                                                             found=4999, bug_cases=10000), 0.9))
        # Counts that make no sense don't fall back to the stored ratio.
        self.assertFalse(evaluations.is_trusted_reviewer(row(precision=1.0, recall=1.0, matched=0, serious=0,
                                                             found=5, bug_cases=5), 0.9))
        self.assertFalse(evaluations.is_trusted_reviewer(row(precision=1.0, recall=1.0, matched=3, serious=2,
                                                             found=5, bug_cases=5), 0.9))
        # Rows without counts (older results) still use the stored ratios.
        self.assertTrue(evaluations.is_trusted_reviewer(row(precision=0.95, recall=0.6), 0.9))

    def test_auto_trusted_roles_reject_mismatched_catalog_rows(self):
        from store import db
        saved = EVAL_RESULTS.read_text() if EVAL_RESULTS.exists() else None
        rows = (json.loads(saved) if saved else []) + [{
            "model": "g2m-good:1b", "errors": [], "evaluated_at": time.time(),
            "roles": {"reviewer_real": {"metrics": {"holdout": {"precision": 1.0, "recall": 1.0}}}}}]
        EVAL_RESULTS.parent.mkdir(parents=True, exist_ok=True)
        EVAL_RESULTS.write_text(json.dumps(rows))
        with db() as c:
            for ident, ref in (("g2m-good-1b", "g2m-good:1b"), ("g2m-bad-1b", "g2m-bad:1b")):
                c.execute("INSERT OR REPLACE INTO model_catalog (id,reference,display_name,source,created_at) "
                          "VALUES (?,?,?, 'test', ?)", (ident, ref, ref, time.time()))
            c.executemany("INSERT OR REPLACE INTO assignments (role,node,model,state,catalog_id,params) "
                          "VALUES (?, 'master', ?, 'active', ?, ?)", [
                              ("g2m-match", "g2m-good:1b", "g2m-good-1b", "{}"),
                              ("g2m-derived", "g2m-good:1b", "g2m-good-1b", '{"temperature": 0.1}'),
                              ("g2m-mismatch", "g2m-bad:1b", "g2m-good-1b", "{}"),
                              ("g2m-missing", "g2m-good:1b", "g2m-gone", "{}")])
        try:
            got = {role for role in controller.auto_trusted_roles() if role.startswith("g2m-")}
            self.assertEqual(got, {"g2m-match"})  # the derived fleet-g2m-good-1b model was never benchmarked
            model = self.models()["g2m-good-1b"]
            self.assertEqual((model["trusted_reviewer"], model["derived_model"], model["derived_trusted_reviewer"]),
                             (True, "fleet-g2m-good-1b", False))

            # Benchmark the model that actually serves the derived role, as run_evals records it.
            EVAL_RESULTS.write_text(json.dumps(rows + [{
                "model": "fleet-g2m-good-1b", "errors": [], "evaluated_at": time.time(),
                "roles": {"reviewer_real": {"metrics": {"holdout": {"precision": 1.0, "recall": 1.0}}}}}]))
            got = {role for role in controller.auto_trusted_roles() if role.startswith("g2m-")}
            self.assertEqual(got, {"g2m-match", "g2m-derived"})
            model = self.models()["g2m-good-1b"]
            self.assertEqual((model["trusted_reviewer"], model["derived_trusted_reviewer"]), (True, True))
            self.assertFalse(self.models()["g2m-bad-1b"]["derived_trusted_reviewer"])
        finally:
            with db() as c:
                c.execute("DELETE FROM assignments WHERE role LIKE 'g2m-%'")
                c.execute("DELETE FROM model_catalog WHERE id IN ('g2m-good-1b','g2m-bad-1b')")
            if saved is None:
                EVAL_RESULTS.unlink(missing_ok=True)
            else:
                EVAL_RESULTS.write_text(saved)

    def test_pipeline_auto_trusts_role_held_only_by_trusted_reviewers(self):
        from store import db
        original_call = controller.pipeline_model_call
        saved = EVAL_RESULTS.read_text() if EVAL_RESULTS.exists() else None

        def result(model, precision, recall):
            return {"model": model, "roles": {"reviewer_real": {"score": 80.0, "metrics": {
                "holdout": {"precision": precision, "recall": recall}, "dev": {}, "all": {}}}},
                "errors": [], "evaluated_at": time.time()}
        rows = (json.loads(saved) if saved else []) + [result("g2-good:1b", 1.0, 0.8), result("g2-bad:1b", 0.5, 1.0),
                                                       result("g2-plain:latest", 1.0, 1.0), result("g2-tagless", 1.0, 1.0)]
        EVAL_RESULTS.parent.mkdir(parents=True, exist_ok=True)
        EVAL_RESULTS.write_text(json.dumps(rows))
        with db() as c:
            # Untagged catalog reference vs tagged result, and the other way round: the same model either way.
            for ident, ref in (("g2-good-1b", "g2-good:1b"), ("g2-bad-1b", "g2-bad:1b"),
                               ("g2-plain", "g2-plain"), ("g2-tagless-latest", "g2-tagless:latest")):
                c.execute("INSERT OR REPLACE INTO model_catalog (id,reference,display_name,source,created_at) "
                          "VALUES (?,?,?, 'test', ?)", (ident, ref, ref, time.time()))
            c.execute("INSERT OR REPLACE INTO assignments (role,node,model,state,catalog_id,params) "
                      "VALUES ('g2role','master','g2-good:1b','active','g2-good-1b','{}')")
        controller.pipeline_model_call = lambda role, spec, diff: json.dumps({"verdict": "approve", "issues": []})
        body = {"title": "Auto-trusted review", "spec": "s", "repo": "/repo", "gate": "pytest", "mode": "auto-edit",
                "builder": {"agent": "claude"}, "reviewers": [{"kind": "role", "role": "g2role"}], "max_rounds": 1}

        def run_pipeline(title):
            p = self.c.post("/api/pipelines", json=dict(body, title=title), headers=W).json()
            def ready(x):
                # Re-apply the builder's exit until the pipeline moves on, so no earlier write can undo it.
                if x["state"] == "building":
                    FakeRunner.jobs[p["build_task"]].update(state="exited", exit_code=0, gate={"state": "passed", "exit_code": 0})
                return x if x["state"] in ("ready", "escalated") else None
            return settle_pipeline(self.c, p["id"], ready)
        try:
            models = self.models()
            self.assertTrue(models["g2-good-1b"]["trusted_reviewer"])
            self.assertFalse(models["g2-bad-1b"]["trusted_reviewer"])
            self.assertTrue(models["g2-plain"]["trusted_reviewer"])
            self.assertTrue(models["g2-tagless-latest"]["trusted_reviewer"])
            self.assertTrue(all(m["trusted_reviewer"] is False for k, m in models.items() if not k.startswith("g2-")))
            self.assertEqual(controller.auto_trusted_roles().count("g2role"), 1)

            done = run_pipeline("Trusted by benchmark")
            self.assertEqual(done["state"], "ready")
            self.assertTrue(done["reviews"]["0"]["trusted"])
            self.assertEqual(done["reviews"]["0"]["trusted_by"], "benchmark")
            self.assertEqual(done["trusted_roles"], [])

            # One untrusted model anywhere on the role is enough to lose the automatic trust.
            with db() as c:
                c.execute("INSERT OR REPLACE INTO assignments (role,node,model,state,catalog_id,params) "
                          "VALUES ('g2role','other-node','g2-bad:1b','active','g2-bad-1b','{}')")
            self.assertNotIn("g2role", controller.auto_trusted_roles())
            escalated = run_pipeline("Mixed role")
            self.assertEqual(escalated["state"], "escalated")
            self.assertFalse(escalated["reviews"]["0"]["trusted"])
        finally:
            controller.pipeline_model_call = original_call
            with db() as c:
                c.execute("DELETE FROM assignments WHERE role='g2role'")
                c.execute("DELETE FROM model_catalog WHERE id IN ('g2-good-1b','g2-bad-1b','g2-plain','g2-tagless-latest')")
            if saved is None:
                EVAL_RESULTS.unlink(missing_ok=True)
            else:
                EVAL_RESULTS.write_text(saved)

    def test_role_review_falls_back_and_unusable_reviews_say_why(self):
        from store import db
        original = controller.pipeline_model_call
        with db() as c:
            c.execute("INSERT OR REPLACE INTO roles (name, fallback, description) VALUES ('slowrole', 'fastrole', NULL)")
        calls = []

        def fake(role, spec, diff):
            calls.append(role)
            if role == "slowrole":
                raise TimeoutError("took too long")
            return json.dumps({"verdict": "approve", "issues": []})
        controller.pipeline_model_call = fake
        FakeRunner.agents["codex"] = "/x/codex"
        self.c.post("/api/refresh", headers=W)

        def finish_build(pipeline):
            # Settle the fake task and drive the production build-to-review
            # transition on the app's event loop, without waiting for either
            # background poller to be scheduled.
            self.c.portal.call(controller.tasks.poll_task, pipeline["build_task"])
            task_state = controller.tasks.get_task(pipeline["build_task"])["state"]
            self.assertEqual(task_state, "review")
            self.c.portal.call(controller.advance_pipeline, pipeline["id"],
                               {"type": "build", "state": task_state})

        try:
            body = {"title": "Fallback review", "spec": "s", "repo": "/repo", "gate": "pytest", "mode": "auto-edit",
                    "builder": {"agent": "codex"}, "reviewers": [{"kind": "role", "role": "slowrole"}],
                    "max_rounds": 1, "require_trusted": False}
            p = self.c.post("/api/pipelines", json=body, headers=W).json()
            FakeRunner.jobs[p["build_task"]].update(state="exited", exit_code=0, gate={"state": "passed", "exit_code": 0})
            finish_build(p)
            done = wait_for(lambda: (lambda x: x if x["state"] == "ready" else None)(
                self.c.get(f"/api/pipelines/{p['id']}").json()), timeout=20)
            self.assertEqual(calls, ["slowrole", "fastrole"])
            self.assertIn("fallback role fastrole", done["reviews"]["0"]["reason"])
            # Both roles failing: escalated, with the reason, and a retry runs the reviews again.
            controller.pipeline_model_call = lambda role, spec, diff: (_ for _ in ()).throw(ConnectionError("down"))
            p2 = self.c.post("/api/pipelines", json=dict(body, title="No reviewer"), headers=W).json()
            FakeRunner.jobs[p2["build_task"]].update(state="exited", exit_code=0, gate={"state": "passed", "exit_code": 0})
            finish_build(p2)
            esc = wait_for(lambda: (lambda x: x if x["state"] == "escalated" else None)(
                self.c.get(f"/api/pipelines/{p2['id']}").json()), timeout=20)
            self.assertIn("ConnectionError", esc["history"][-1]["note"])
            self.assertEqual(self.c.post(f"/api/pipelines/{p['id']}/retry", headers=W).status_code, 409)
            controller.pipeline_model_call = lambda role, spec, diff: json.dumps({"verdict": "approve", "issues": []})
            self.assertEqual(self.c.post(f"/api/pipelines/{p2['id']}/retry", headers=W).status_code, 200)
            wait_for(lambda: self.c.get(f"/api/pipelines/{p2['id']}").json()["state"] == "ready", timeout=20)
        finally:
            controller.pipeline_model_call = original
            FakeRunner.agents["codex"] = None
            self.c.post("/api/refresh", headers=W)

    def test_large_role_review_diff_is_split_on_file_boundaries(self):
        original_call = controller.pipeline_model_call
        original_limit = controller.FLEET_REVIEW_MAX_CHARS
        first = "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-old_a = 0\n+new_a = 1\n"
        second = "diff --git a/b.py b/b.py\n--- a/b.py\n+++ b/b.py\n@@ -1 +1 @@\n-old_b = 0\n+new_b = 1\n"
        calls = []

        def fake(role, spec, diff):
            calls.append(diff)
            return json.dumps({"verdict": "approve", "issues": []})

        controller.pipeline_model_call = fake
        controller.FLEET_REVIEW_MAX_CHARS = max(len(first), len(second))
        try:
            raw, reason, fallback = asyncio.run(controller.role_review("chunkrole", "spec", first + second))
            self.assertEqual(json.loads(raw)["verdict"], "approve")
            self.assertIsNone(reason)
            self.assertFalse(fallback)
            self.assertEqual(calls, [first, second])
        finally:
            controller.pipeline_model_call = original_call
            controller.FLEET_REVIEW_MAX_CHARS = original_limit

    def test_large_role_review_merges_chunk_issues_and_verdicts(self):
        original_call = controller.pipeline_model_call
        original_limit = controller.FLEET_REVIEW_MAX_CHARS
        first = "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-old_a = 0\n+new_a = 1\n"
        second = "diff --git a/b.py b/b.py\n--- a/b.py\n+++ b/b.py\n@@ -1 +1 @@\n-safe_b = True\n+dangerous_b = True\n"
        issue = {"severity": "high", "file": "b.py", "problem": "Dangerous mode enabled",
                 "evidence": "dangerous_b = True", "expected": "safe mode", "actual": "dangerous mode",
                 "fix": "set dangerous_b to False"}

        def fake(role, spec, diff):
            return (json.dumps({"verdict": "request_changes", "issues": [issue]}) if "b/b.py" in diff
                    else json.dumps({"verdict": "approve", "issues": []}))

        controller.pipeline_model_call = fake
        controller.FLEET_REVIEW_MAX_CHARS = max(len(first), len(second))
        try:
            raw, reason, fallback = asyncio.run(controller.role_review("chunkrole", "spec", first + second))
            merged = json.loads(raw)
            self.assertEqual(merged, {"verdict": "request_changes", "issues": [issue]})
            self.assertIsNone(reason)
            self.assertFalse(fallback)
        finally:
            controller.pipeline_model_call = original_call
            controller.FLEET_REVIEW_MAX_CHARS = original_limit

    def test_invalid_large_diff_chunk_invalidates_review_and_names_file(self):
        original_call = controller.pipeline_model_call
        original_limit = controller.FLEET_REVIEW_MAX_CHARS
        first = "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-old_a = 0\n+new_a = 1\n"
        second = "diff --git a/b.py b/b.py\n--- a/b.py\n+++ b/b.py\n@@ -1 +1 @@\n-old_b = 0\n+new_b = 1\n"
        controller.pipeline_model_call = lambda role, spec, diff: ("not json" if "b/b.py" in diff else
                                                                    json.dumps({"verdict": "approve", "issues": []}))
        controller.FLEET_REVIEW_MAX_CHARS = max(len(first), len(second))
        try:
            raw, reason, fallback = asyncio.run(controller.role_review("chunkrole", "spec", first + second))
            self.assertEqual(raw, "")
            self.assertIn("b.py", reason)
            self.assertFalse(fallback)
        finally:
            controller.pipeline_model_call = original_call
            controller.FLEET_REVIEW_MAX_CHARS = original_limit

    # ---------- phase 5b: catalog, installs, roles that wait ----------

    def models(self):
        r = self.c.get("/api/models")
        self.assertEqual(r.status_code, 200, r.text)
        return {m["id"]: m for m in r.json()}

    def wait_install(self, model_id, state, node="master"):
        return wait_for(lambda: (self.models().get(model_id, {}).get("nodes", {}).get(node, {}).get("state") == state)
                        and self.models()[model_id]["nodes"][node], timeout=20)

    def test_catalog_add_validates_against_registry(self):
        r = self.c.post("/api/models", json={"reference": "qwen3:8b", "display_name": "Qwen3 8B",
                                              "defaults": {"num_ctx": 8192, "think": False}}, headers=W)
        self.assertEqual(r.status_code, 200, r.text)
        m = self.models()["qwen3-8b"]
        self.assertEqual(m["reference"], "qwen3:8b")
        self.assertAlmostEqual(m["download_gb"], 5.2, places=1)
        self.assertEqual(m["defaults"]["num_ctx"], 8192)
        self.assertIn(m["nodes"]["master"]["fit"], ("fits", "tight", "wont_fit", "unknown"))
        r = self.c.post("/api/models", json={"reference": "ghost:9b"}, headers=W)
        self.assertEqual(r.status_code, 400)
        self.assertIn("isn't in the registry", r.text)
        for bad in ("Qwen3:8b", "qwen3:8b; rm -rf /", "../x"):
            self.assertIn(self.c.post("/api/models", json={"reference": bad}, headers=W).status_code, (400, 422), bad)

    def test_install_on_node_and_remove(self):
        self.c.post("/api/models", json={"reference": "tiny:1b"}, headers=W)
        FakeRunner.calls.clear()
        r = self.c.put("/api/nodes/master/models/tiny-1b", json={"desired": "present", "params": {"temperature": 0.1}},
                       headers=W)
        self.assertEqual(r.status_code, 200, r.text)
        row = self.wait_install("tiny-1b", "ready")
        self.assertEqual(row["ownership"], "fleet-managed")
        sent = [b for p, b in FakeRunner.calls if p == "/models/ensure"]
        self.assertEqual(sent[0]["ref"], "tiny:1b")
        self.assertEqual(sent[0]["derived"], "fleet-tiny-1b")
        self.assertEqual(sent[0]["params"], {"temperature": 0.1})
        types = [e["type"] for e in self.c.get("/api/events/list?limit=1000").json()]
        self.assertIn("model.state", types)
        # Can't drop it from the catalog while a node should have it.
        self.assertEqual(self.c.delete("/api/models/tiny-1b", headers=W).status_code, 409)
        self.c.put("/api/nodes/master/models/tiny-1b", json={"desired": "absent"}, headers=W)
        self.wait_install("tiny-1b", "removed")
        self.assertTrue([b for p, b in FakeRunner.calls if p == "/models/remove" and b["ref"] == "tiny:1b"])
        self.assertEqual(self.c.delete("/api/models/tiny-1b", headers=W).status_code, 200)

    def test_blocked_install_shows_reason(self):
        self.c.post("/api/models", json={"reference": "qwen3:8b"}, headers=W)
        FakeRunner.op_script = ["blocked"]
        try:
            FakeRunner.ops.clear()
            self.c.put("/api/nodes/master/models/qwen3-8b", json={"desired": "present"}, headers=W)
            self.wait_install("qwen3-8b", "blocked")
        finally:
            FakeRunner.op_script = []
            self.c.put("/api/nodes/master/models/qwen3-8b", json={"desired": "absent"}, headers=W)

    def test_role_waits_for_install(self):
        self.c.post("/api/models", json={"reference": "qwen3:8b"}, headers=W)
        FakeRunner.models.pop("qwen3:8b", None)
        FakeRunner.op_script = ["pulling", "pulling", "verifying", "ready"]
        try:
            r = self.c.post("/api/roles", json={"role": "reviewer", "node": "master", "model": "qwen3:8b"}, headers=W)
            self.assertEqual(r.status_code, 400)  # old behaviour without ensure_model
            r = self.c.post("/api/roles", json={"role": "reviewer", "node": "master", "model": "qwen3:8b",
                                                 "ensure_model": True}, headers=W)
            self.assertEqual(r.status_code, 200, r.text)
            a = [x for x in r.json()["assignments"] if x["role"] == "reviewer" and x["model"] == "qwen3:8b"][0]
            self.assertEqual(a["state"], "pending_install")
            wait_for(lambda: [x for x in self.c.get("/api/fleet").json()["assignments"]
                              if x["role"] == "reviewer" and x["model"] == "qwen3:8b"][0]["state"] == "active", timeout=20)
        finally:
            FakeRunner.op_script = []

    def test_catalog_is_seeded_from_evals(self):
        evals = Path(__file__).resolve().parent.parent / "evals" / "results" / "results.json"
        if not evals.exists():
            self.skipTest("no eval results")
        first = json.loads(evals.read_text())[0]
        ref = first["model"] if ":" in first["model"] else first["model"] + ":latest"
        found = [m for m in self.models().values() if m["reference"] == ref]
        self.assertTrue(found, ref)
        self.assertTrue(found[0]["evals"])

    def test_second_request_during_an_install_is_refused(self):
        self.c.post("/api/models", json={"reference": "tiny:1b"}, headers=W)
        FakeRunner.models.pop("tiny:1b", None)
        FakeRunner.op_script = ["pulling"] * 50
        try:
            self.c.put("/api/nodes/master/models/tiny-1b", json={"desired": "present"}, headers=W)
            self.wait_install("tiny-1b", "pulling")
            r = self.c.put("/api/nodes/master/models/tiny-1b", json={"desired": "present"}, headers=W)
            self.assertEqual(r.status_code, 409)
            self.assertIn("in progress", r.text)
        finally:
            FakeRunner.op_script = []
            for op in FakeRunner.ops.values():
                op["script"] = ["ready"]
            self.wait_install("tiny-1b", "ready")
            self.c.put("/api/nodes/master/models/tiny-1b", json={"desired": "absent"}, headers=W)
            self.wait_install("tiny-1b", "removed")

    def test_preexisting_stays_preexisting_after_params_install(self):
        self.c.post("/api/models", json={"reference": "tiny:1b"}, headers=W)
        FakeRunner.models["tiny:1b"] = False
        try:
            self.c.post("/api/refresh", headers=W)
            wait_for(lambda: self.models()["tiny-1b"]["nodes"]["master"].get("installed"))
            self.c.put("/api/nodes/master/models/tiny-1b", json={"desired": "present", "params": {"temperature": 0}}, headers=W)
            row = self.wait_install("tiny-1b", "ready")
            self.assertEqual(row["ownership"], "pre-existing")
        finally:
            FakeRunner.models.pop("tiny:1b", None)
            FakeRunner.models.pop("fleet-tiny-1b", None)

    def test_params_on_installed_model_are_not_ready_until_derived_exists(self):
        self.c.post("/api/models", json={"reference": "tiny:1b"}, headers=W)
        FakeRunner.models["tiny:1b"] = True
        FakeRunner.models.pop("fleet-tiny-1b", None)
        FakeRunner.op_script = ["configuring"] * 50
        try:
            self.c.post("/api/refresh", headers=W)
            r = self.c.put("/api/nodes/master/models/tiny-1b", json={"desired": "present", "params": {"temperature": 0}}, headers=W)
            self.assertNotEqual(r.json()["state"], "ready")
            self.wait_install("tiny-1b", "configuring")
        finally:
            FakeRunner.op_script = []
            for op in FakeRunner.ops.values():
                op["script"] = ["ready"]
            self.wait_install("tiny-1b", "ready")
            FakeRunner.models.pop("tiny:1b", None)
            FakeRunner.models.pop("fleet-tiny-1b", None)

    def test_assigning_a_role_keeps_a_running_install(self):
        self.c.post("/api/models", json={"reference": "qwen3:8b"}, headers=W)
        FakeRunner.models.pop("qwen3:8b", None)
        FakeRunner.op_script = ["pulling"] * 50
        try:
            self.c.put("/api/nodes/master/models/qwen3-8b", json={"desired": "present"}, headers=W)
            row = self.wait_install("qwen3-8b", "pulling")
            ensures = len([1 for p, _ in FakeRunner.calls if p == "/models/ensure"])
            r = self.c.post("/api/roles", json={"role": "fast", "node": "master", "model": "qwen3:8b", "ensure_model": True}, headers=W)
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(self.models()["qwen3-8b"]["nodes"]["master"]["state"], "pulling")
            time.sleep(0.6)
            self.assertEqual(ensures, len([1 for p, _ in FakeRunner.calls if p == "/models/ensure"]))  # no second op
        finally:
            FakeRunner.op_script = []
            for op in FakeRunner.ops.values():
                op["script"] = ["ready"]
            wait_for(lambda: [x for x in self.c.get("/api/fleet").json()["assignments"]
                              if x["role"] == "fast" and x["model"] == "qwen3:8b"][0]["state"] == "active", timeout=20)
            self.c.request("DELETE", "/api/roles", json={"role": "fast", "node": "master", "model": "qwen3:8b"}, headers=W)
            self.c.put("/api/nodes/master/models/qwen3-8b", json={"desired": "absent"}, headers=W)
            self.wait_install("qwen3-8b", "removed")

    def test_request_while_an_operation_is_being_started_is_refused(self):
        from store import db
        self.c.post("/api/models", json={"reference": "tiny:1b"}, headers=W)
        with db() as c:
            c.execute("INSERT OR REPLACE INTO node_models (node,catalog_id,desired,state,progress,params,updated_at) "
                      "VALUES ('master','tiny-1b','present','starting','{}','{}',?)", (time.time(),))
        r = self.c.put("/api/nodes/master/models/tiny-1b", json={"desired": "absent"}, headers=W)
        self.assertEqual(r.status_code, 409)
        with db() as c:
            c.execute("DELETE FROM node_models WHERE node='master' AND catalog_id='tiny-1b'")

    def test_role_with_settings_waits_for_the_derived_model(self):
        self.c.post("/api/models", json={"reference": "tiny:1b", "defaults": {"temperature": 0.1}}, headers=W)
        FakeRunner.models["tiny:1b"] = True
        FakeRunner.models.pop("fleet-tiny-1b", None)
        FakeRunner.op_script = ["configuring"] * 50
        try:
            self.c.post("/api/refresh", headers=W)
            r = self.c.post("/api/roles", json={"role": "fast", "node": "master", "model": "tiny:1b", "ensure_model": True}, headers=W)
            self.assertEqual(r.status_code, 200, r.text)
            a = [x for x in r.json()["assignments"] if x["role"] == "fast" and x["model"] == "tiny:1b"][0]
            self.assertEqual(a["state"], "pending_install")
            # A different setting while that install runs is refused rather than silently ignored.
            self.wait_install("tiny-1b", "configuring")
            r = self.c.post("/api/roles", json={"role": "coder", "node": "master", "model": "tiny:1b", "ensure_model": True,
                                                 "params": {"temperature": 0.9}}, headers=W)
            self.assertEqual(r.status_code, 409, r.text)
        finally:
            FakeRunner.op_script = []
            for op in FakeRunner.ops.values():
                op["script"] = ["ready"]
            self.wait_install("tiny-1b", "ready")
            self.c.request("DELETE", "/api/roles", json={"role": "fast", "node": "master", "model": "tiny:1b"}, headers=W)
            self.c.put("/api/nodes/master/models/tiny-1b", json={"desired": "absent", "confirm_preexisting": True}, headers=W)
            self.wait_install("tiny-1b", "removed")

    def test_preexisting_model_is_marked(self):
        self.c.post("/api/models", json={"reference": "tiny:1b"}, headers=W)
        FakeRunner.models["tiny:1b"] = False  # on the node before the fleet managed it
        try:
            self.c.post("/api/refresh", headers=W)
            row = wait_for(lambda: self.models()["tiny-1b"]["nodes"]["master"].get("installed")
                           and self.models()["tiny-1b"]["nodes"]["master"])
            self.assertEqual(row["ownership"], "pre-existing")
        finally:
            FakeRunner.models.pop("tiny:1b", None)

    def test_requested_gate_must_pass(self):
        t = self.task(gate="pytest")
        FakeRunner.jobs[t["id"]].update(state="exited", exit_code=0, gate={"state": "skipped"})
        wait_for(lambda: self.state(t["id"]) == "failed")

    def test_wait_all_times_out_if_some_still_active(self):
        a, b = self.task(), self.task()
        FakeRunner.jobs[a["id"]].update(state="exited", exit_code=0)
        wait_for(lambda: self.state(a["id"]) == "review")
        r = self.c.get(f"/api/tasks/wait?ids={a['id']},{b['id']}&timeout=1").json()
        self.assertTrue(r["timed_out"])
        r = self.c.get(f"/api/tasks/wait?ids={a['id']},{b['id']}&timeout=1&mode=any").json()
        self.assertFalse(r["timed_out"])

    def test_start_adopts_existing_runner_job(self):
        import tasks as T
        t = self.task(mode="supervised")
        # Simulate a crash after the runner accepted the job but before the controller recorded it.
        FakeRunner.jobs[t["id"]] = {"id": t["id"], "state": "running", "exit_code": None, "session_id": "s-9",
                                    "branch": "b", "cwd": "/w", "denials": [], "gate": None, "result": None}
        T.update(t["id"], state="starting")
        wait_for(lambda: self.state(t["id"]) == "working")
        self.assertEqual(self.c.get(f"/api/tasks/{t['id']}").json()["session_id"], "s-9")

    def test_complete_cant_override_running_task(self):
        t = self.task()
        self.assertEqual(self.c.post(f"/api/tasks/{t['id']}/complete", headers=W).status_code, 409)

    def test_agent_time_stops_when_agent_exits_not_after_gate(self):
        import datetime
        t = self.task(gate="pytest")
        started = self.c.get(f"/api/tasks/{t['id']}").json()["run_started_at"]
        agent_end = datetime.datetime.fromtimestamp(started + 2, datetime.timezone.utc).isoformat()
        FakeRunner.jobs[t["id"]].update(state="exited", exit_code=0, ended_at=agent_end,
                                        gate={"state": "running"})
        wait_for(lambda: self.c.get(f"/api/tasks/{t['id']}").json()["run_started_at"] is None)
        FakeRunner.jobs[t["id"]]["gate"] = {"state": "passed"}
        wait_for(lambda: self.state(t["id"]) == "review")
        self.assertAlmostEqual(self.c.get(f"/api/tasks/{t['id']}").json()["agent_seconds"], 2, delta=0.01)

    def test_reject_sets_ended_at(self):
        t = self.task(mode="supervised")
        self.assertIsNotNone(self.c.post(f"/api/tasks/{t['id']}/reject", headers=W).json()["ended_at"])

    def test_usage(self):
        self.task()
        rows = self.c.get("/api/usage").json()
        self.assertTrue(any(r["node_id"] == "master" and r["agent"] == "claude" for r in rows))

    # ---------- phase 8: operations ----------

    def test_me_reports_port_scope_and_identity(self):
        self.assertEqual(self.c.get("/api/me").json(), {"scope": "admin", "label": "local", "local": True})
        remote = TestClient(controller.app, base_url="https://master.tail.ts.net:8701")
        for scope in ("view", "operate", "admin"):
            token = auth.create_token(scope, "p8-" + scope)
            me = remote.get("/api/me", headers={"Authorization": f"Bearer {token}"}).json()
            self.assertEqual(me, {"scope": scope, "label": "p8-" + scope, "local": False})
            exported = remote.get("/api/export", headers={"Authorization": f"Bearer {token}"})
            self.assertEqual(exported.status_code, 200 if scope == "admin" else 403)

    def test_routing_truth_without_litellm(self):
        response = self.c.post("/api/roles", json={"role": "p8-route", "node": "master",
                                                    "model": "fake-model:1b"}, headers=W)
        self.assertEqual(response.status_code, 200, response.text)
        assignment = next(row for row in response.json()["assignments"] if row["role"] == "p8-route")
        self.assertIs(assignment["routed"], False)
        self.assertEqual(assignment["route_reason"], "litellm not configured")

    def test_enrollment_single_use_expiry_binding_audit_and_rate_limit(self):
        original = controller.now
        clock = [1_900_000_000.0]
        controller.now = lambda: clock[0]
        controller._enroll_failures.clear()
        try:
            made = self.c.post("/api/enroll", json={"name": "p8-node"}, headers=W)
            self.assertEqual(made.status_code, 200, made.text)
            enrollment = made.json()
            self.assertEqual(len(enrollment["code"]), 10)
            self.assertEqual(enrollment["expires_at"], clock[0] + 1800)
            # The command asks for the code on the node's terminal; the code is never part of it.
            self.assertNotIn(enrollment["code"], enrollment["command"])
            self.assertRegex(enrollment["command"],
                             r"^curl -fsS (\S+)/enroll/onboard\.sh \| bash -s -- --enroll --master \1$")

            wrong = self.c.post("/api/enroll/complete", json={"code": "AAAAAAAAAA", "hostname": "p8-host",
                                                                "runner_token": "secret-runner-token"})
            self.assertEqual(wrong.status_code, 403)
            audit = self.c.get("/api/events/list?limit=1000").json()
            self.assertTrue(any(event["type"] == "enrollment.failed" for event in audit))

            completed = self.c.post("/api/enroll/complete", json={"code": enrollment["code"],
                                                                    "hostname": "p8-host.example",
                                                                    "runner_token": "secret-runner-token"})
            self.assertEqual(completed.status_code, 200, completed.text)
            self.assertEqual(completed.json()["status"], "pending")
            self.assertEqual(self.c.post("/api/enroll/complete", json={"code": enrollment["code"],
                                                                        "hostname": "p8-host",
                                                                        "runner_token": "other"}).status_code, 403)
            with db() as c:
                c.execute("INSERT OR IGNORE INTO nodes (id,name,status) VALUES ('p8-node-id','p8-host','pending')")
                controller._bind_enrollment(c, "p8-node-id", "p8-host")
                node = c.execute("SELECT status,runner_token FROM nodes WHERE id='p8-node-id'").fetchone()
            self.assertEqual((node["status"], node["runner_token"]), ("pending", "secret-runner-token"))

            expiring = self.c.post("/api/enroll", json={}, headers=W).json()
            clock[0] += 1801
            self.assertEqual(self.c.post("/api/enroll/complete", json={"code": expiring["code"],
                                                                        "hostname": "late", "runner_token": "x"}).status_code, 403)
            controller._enroll_failures.clear()
            codes = [{"code": "BADCODE%02d" % i, "hostname": "bad", "runner_token": "x"} for i in range(5)]
            statuses = [self.c.post("/api/enroll/complete", json=body).status_code for body in codes]
            self.assertEqual(statuses, [403, 403, 403, 403, 429])
        finally:
            controller.now = original
            controller._enroll_failures.clear()

    def test_power_eligibility_blocks_agents_not_ollama(self):
        node = {"status": "approved", "runner": {"agents": {"claude": "x", "codex": "x", "ollama": "x"},
                                                        "power": {"on_battery": True, "battery_pct": 23, "ok": False},
                                                        "max_mode": "auto-edit"},
                "runner_fails": 0, "models": ["fake-model:1b"], "ceiling": "auto-edit"}
        self.assertEqual(tasks.eligible(node, "claude", None, "auto-edit"), "on battery (23%)")
        self.assertEqual(tasks.eligible(node, "codex", None, "auto-edit"), "on battery (23%)")
        self.assertIsNone(tasks.eligible(node, "ollama", "fake-model:1b", "auto-edit"))

    def test_role_fallbacks_reject_cycles_and_appear_in_fleet(self):
        self.assertEqual(self.c.put("/api/roles/p8-primary", json={"fallback": "p8-secondary",
                                                                   "description": "primary"}, headers=W).status_code, 200)
        cycle = self.c.put("/api/roles/p8-secondary", json={"fallback": "p8-primary"}, headers=W)
        self.assertEqual(cycle.status_code, 400)
        info = next(row for row in self.c.get("/api/fleet").json()["roles"] if row["name"] == "p8-primary")
        self.assertEqual(info, {"name": "p8-primary", "fallback": "p8-secondary", "description": "primary"})

    def test_model_call_stats(self):
        for ms, ok, fallback in ((10, True, None), (20, False, None), (30, True, "old-role"), (100, True, None)):
            response = self.c.post("/api/usage/calls", json={"role": "p8-stat", "model": "m", "ms": ms,
                                                               "ok": ok, "fallback_from": fallback}, headers=W)
            self.assertEqual(response.status_code, 200, response.text)
        row = next(item for item in self.c.get("/api/usage/calls?days=7").json() if item["role"] == "p8-stat")
        self.assertEqual((row["count"], row["error_rate"], row["p50_ms"], row["p95_ms"], row["fallbacks"]),
                         (4, 0.25, 20, 100, 1))
        self.assertEqual((row["grounded"], row["abstain_rate"], row["escalation_rate"], row["logprob_mean"]),
                         (0, None, None, None))

    def test_grounded_call_outcomes_aggregate_per_role_and_model(self):
        calls = [("answered", 0.9), ("answered", 0.7), ("abstained", None), ("escalated", None),
                 ("check_failed", 0.2), (None, None)]
        for outcome, mean in calls:
            body = {"role": "g4-stat", "model": "m1", "ms": 5, "ok": True}
            if outcome:
                body["outcome"] = outcome
            if mean is not None:
                body |= {"logprob_mean": mean, "logprob_min": mean / 2}
            response = self.c.post("/api/usage/calls", json=body, headers=W)
            self.assertEqual(response.status_code, 200, response.text)
        self.c.post("/api/usage/calls", json={"role": "g4-stat", "model": "m2", "ms": 5, "ok": True,
                                              "outcome": "abstained"}, headers=W)
        for bad in ({"outcome": "guessed"}, {"logprob_mean": 1.5}):
            response = self.c.post("/api/usage/calls", json={"role": "g4-stat", "model": "m1", "ms": 5, "ok": True,
                                                              **bad}, headers=W)
            self.assertEqual(response.status_code, 422, response.text)
        rows = {item["model"]: item for item in self.c.get("/api/usage/calls?days=7").json()
                if item["role"] == "g4-stat"}
        m1 = rows["m1"]
        self.assertEqual((m1["count"], m1["grounded"]), (6, 5))
        self.assertEqual(m1["outcomes"], {"answered": 2, "abstained": 1, "escalated": 1, "check_failed": 1})
        self.assertEqual((m1["abstain_rate"], m1["escalation_rate"], m1["check_failed_rate"]), (0.2, 0.2, 0.2))
        self.assertAlmostEqual(m1["logprob_mean"], 0.6)
        self.assertEqual(rows["m2"]["outcomes"]["abstained"], 1)
        self.assertEqual(rows["m2"]["abstain_rate"], 1.0)

    def test_notification_defaults_toggle_and_test_event(self):
        rules = self.c.get("/api/notify-rules").json()
        self.assertTrue({"task.awaiting_approval", "task.blocked", "pipeline.escalated", "node.offline"}
                        <= {rule["event_glob"] for rule in rules})
        rule = rules[0]
        toggled = self.c.put(f"/api/notify-rules/{rule['id']}", json={"enabled": not rule["enabled"]}, headers=W)
        self.assertEqual(toggled.status_code, 200, toggled.text)
        tested = self.c.post(f"/api/notify-rules/{rule['id']}/test", headers=W)
        self.assertEqual(tested.status_code, 200, tested.text)
        self.assertTrue(any(event["type"] == "notify.test" for event in self.c.get("/api/events/list?limit=1000").json()))

    def test_retention_cleans_worktrees_prunes_events_and_only_marks_models_idle(self):
        task = self.task()
        self.c.post(f"/api/tasks/{task['id']}/cancel", headers=W)
        original = controller.now
        clock = [time.time()]
        controller.now = lambda: clock[0]
        try:
            with db() as c:
                c.execute("UPDATE tasks SET updated_at=?,cleaned_at=NULL WHERE id=?", (clock[0] - 8 * 86400, task["id"]))
                c.execute("INSERT INTO events (ts,task_id,type,data) VALUES (?,?,?,?)",
                          (clock[0] - 91 * 86400, None, "p8.old", "{}"))
                c.execute("INSERT OR REPLACE INTO model_catalog (id,reference,engine,display_name,defaults,suggested_roles,evals,source,created_at) "
                          "VALUES ('p8-idle','fake-model:1b','ollama','idle','{}','[]','{}','test',?)", (clock[0],))
                c.execute("INSERT OR REPLACE INTO node_models (node,catalog_id,desired,state,ownership,params,updated_at) "
                          "VALUES ('master','p8-idle','present','ready','fleet-managed','{}',?)",
                          (clock[0] - 15 * 86400,))
            result = asyncio.run(controller.retention_once())
            self.assertEqual((result["cleaned"], result["models_idle"]), (1, 1))
            with db() as c:
                kept = c.execute("SELECT cleaned_at FROM tasks WHERE id=?", (task["id"],)).fetchone()
                old = c.execute("SELECT 1 FROM events WHERE type='p8.old'").fetchone()
                state = c.execute("SELECT state FROM node_models WHERE node='master' AND catalog_id='p8-idle'").fetchone()[0]
            self.assertIsNotNone(kept["cleaned_at"])
            self.assertIsNone(old)
            self.assertEqual(state, "idle")
        finally:
            controller.now = original

    def test_export_has_no_secrets_and_import_versions_without_implicit_prune(self):
        self.c.put("/api/roles/p8-export-keep", json={"fallback": None, "description": "keep"}, headers=W)
        exported = self.c.get("/api/export")
        self.assertEqual(exported.status_code, 200, exported.text)
        text = exported.text
        self.assertNotIn("tok-master-123", text)
        self.assertNotIn("secret-runner-token", text)
        doc = exported.json()
        doc["instructions"].append({"scope": "role", "scope_id": "p8-import", "text": "one"})
        preview = self.c.post("/api/import?dry_run=1", json=doc, headers=W)
        self.assertEqual(preview.status_code, 200, preview.text)
        self.assertIn("instructions", preview.json()["changes"])
        self.assertEqual(self.c.post("/api/import", json=doc, headers=W).status_code, 200)
        doc["instructions"][-1]["text"] = "two"
        self.assertEqual(self.c.post("/api/import", json=doc, headers=W).status_code, 200)
        history = self.c.get("/api/instructions/role/p8-import/history").json()
        self.assertEqual([row["text"] for row in history[:2]], ["two", "one"])
        self.assertTrue(any(row["name"] == "p8-export-keep" for row in self.c.get("/api/fleet").json()["roles"]))
        pruned = self.c.get("/api/export").json()
        pruned["roles"] = [row for row in pruned["roles"] if row["name"] != "p8-export-keep"]
        pruned["prune"] = True
        self.assertEqual(self.c.post("/api/import", json=pruned, headers=W).status_code, 200)
        self.assertFalse(any(row["name"] == "p8-export-keep" for row in self.c.get("/api/fleet").json()["roles"]))

    def test_runner_digest_update_and_dashboard_redirects(self):
        node = next(row for row in self.c.get("/api/fleet").json()["nodes"] if row["id"] == "master")
        self.assertFalse(node["update_available"])
        FakeRunner.digest = "outdated"
        try:
            wait_for(lambda: next(row for row in self.c.get("/api/fleet").json()["nodes"]
                                  if row["id"] == "master")["update_available"])
        finally:
            FakeRunner.digest = hashlib.sha256(Path(controller.RUNNER_SOURCE).read_bytes()).hexdigest()
        self.assertEqual(self.c.get("/", follow_redirects=False).headers["location"], "/app/")
        self.assertIn("AI Fleet", self.c.get("/classic").text)
        self.assertEqual(self.c.get("/login", follow_redirects=False).headers["location"], "/app/")
        public = TestClient(controller.app, base_url="https://master.tail.ts.net:8701")
        self.assertEqual(public.get("/enroll/onboard.sh").status_code, 200)


if __name__ == "__main__":
    unittest.main()
