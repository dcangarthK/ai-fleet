"""Controller tests against a fake runner. Run:
uv run --with fastapi --with uvicorn --with httpx python -m unittest -v tests/test_controller.py
"""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="fleet-ctl-"))
FAKE_TS = TMP / "tailscale"
FAKE_TS.write_text("#!/bin/sh\necho '{\"Peer\":{}}'\n")
FAKE_TS.chmod(0o755)
os.environ.update({"FLEET_DB": str(TMP / "fleet.db"), "TAILSCALE_BIN": str(FAKE_TS),
                   "FLEET_POLL_SECONDS": "0.3", "FLEET_TASK_POLL_SECONDS": "0.2",
                   "FLEET_PUBLIC_HOST": "master.tail.ts.net", "FLEET_PUBLIC_PORT": "8701", "LITELLM_MASTER_KEY": ""})


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
            return self._send(200, {k: v for k, v in op.items() if k != "script"})
        if self.path == "/health":
            return self._send(200, {"ok": True, "version": "2", "max_mode": FakeRunner.max_mode, "running": 0,
                                    "gate_profiles": ["pytest"], "allow_raw_gates": False,
                                    "models": {"remote_management": True, "disk_reserve_gb": 8},
                                    "agents": {"claude": "/x/claude", "codex": None, "ollama": "http://o"}})
        jid = self.path.split("/")[2].split("?")[0]
        if jid not in self.jobs:
            return self._send(404, {"detail": "no job"})
        if self.path.endswith("/diff"):
            return self._send(200, {"stat": "1 file changed", "diff": "+hello"})
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
                   "result": None, "denials": [], "gate": None, "runs": 1}
            self.jobs[body["id"]] = job
            return self._send(200, job)
        jid = self.path.split("/")[2]
        if self.path.endswith("/message"):
            self.jobs[jid].update(state="running", denials=[], runs=self.jobs[jid]["runs"] + 1)
        elif self.path.endswith("/cancel"):
            self.jobs[jid]["state"] = "cancelled"
        return self._send(200, self.jobs[jid])


server = ThreadingHTTPServer(("127.0.0.1", 0), FakeRunner)
threading.Thread(target=server.serve_forever, daemon=True).start()
os.environ["FLEET_LOCAL_RUNNER"] = f"http://127.0.0.1:{server.server_address[1]}"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "master" / "controller"))
from fastapi.testclient import TestClient  # noqa: E402

import auth  # noqa: E402
import controller  # noqa: E402
import events  # noqa: E402

W = {"X-Fleet": "1"}


def wait_for(fn, timeout=5):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(0.05)
    raise AssertionError("condition not met in time")


class ControllerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ctx = TestClient(controller.app, base_url="http://127.0.0.1")
        cls.c = cls.ctx.__enter__()
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
        types = [e["type"] for e in self.c.get("/api/events/list").json() if e["task_id"] == t["id"]]
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
        self.c.post(f"/api/tasks/{t['id']}/approve", headers=W)
        path, body = [c for c in FakeRunner.calls if c[0] == f"/jobs/{t['id']}/message"][-1]
        self.assertEqual(body["allowed_tools"], ["Bash"])
        self.assertEqual(self.state(t["id"]), "working")

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
            t = self.task(mode="supervised")
            wait_for(lambda: out.exists() and t["id"] in out.read_text())
        finally:
            events.PLUGINS.pop()

    def test_validation_and_eligibility(self):
        bad = lambda body: self.c.post("/api/tasks", json=body, headers=W)  # noqa: E731
        self.assertEqual(bad({"agent": "claude", "prompt": "   "}).status_code, 422)
        r = bad({"agent": "claude", "prompt": "x", "mode": "read-only", "gate": "true"})
        self.assertEqual(r.status_code, 400)
        r = bad({"agent": "codex", "prompt": "x"})  # runner reports codex: null
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
        FakeRunner.tokens.clear()
        self.task()
        self.assertNotIn("tok-master-123", FakeRunner.tokens)

    # ---------- phase 5b: catalog, installs, roles that wait ----------

    def models(self):
        r = self.c.get("/api/models")
        self.assertEqual(r.status_code, 200, r.text)
        return {m["id"]: m for m in r.json()}

    def wait_install(self, model_id, state, node="master"):
        return wait_for(lambda: (self.models().get(model_id, {}).get("nodes", {}).get(node, {}).get("state") == state)
                        and self.models()[model_id]["nodes"][node], timeout=10)

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
        types = [e["type"] for e in self.c.get("/api/events/list").json()]
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
                              if x["role"] == "reviewer" and x["model"] == "qwen3:8b"][0]["state"] == "active", timeout=10)
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


if __name__ == "__main__":
    unittest.main()
