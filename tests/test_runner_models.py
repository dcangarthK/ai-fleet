"""Phase 5b runner model management (docs/SPEC-phase5-models.md) against a fake Ollama and a fake registry.
Run: python3 -m unittest -v tests/test_runner_models.py
"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_runner as base  # noqa: E402

GB = 1_000_000_000


class FakeOllama(BaseHTTPRequestHandler):
    """Enough of Ollama's API for the runner: tags, ps, version, pull (streamed), create, chat, generate, delete."""
    state = {}

    def log_message(self, *a):
        pass

    def _json(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        s = FakeOllama.state
        if self.path == "/api/version":
            return self._json(200, {"version": s["version"]})
        if self.path == "/api/tags":
            return self._json(200, {"models": [{"name": n, "size": int(gb * GB), "digest": "sha256:" + n.replace(":", "")}
                                               for n, gb in s["models"].items()]})
        if self.path == "/api/ps":
            return self._json(200, {"models": [{"name": n, "size": int(gb * GB)} for n, gb in s["loaded"].items()]})
        self._json(404, {"error": "not found"})

    def do_POST(self):
        s = FakeOllama.state
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        s["calls"].append((self.path, body))
        if self.path == "/api/pull":
            name = body["model"]
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson")
            self.end_headers()
            if name in s["pull_errors"]:
                self.wfile.write((json.dumps({"error": s["pull_errors"][name]}) + "\n").encode())
                return
            for done in (0, 1, 2, 3, 4):
                self.wfile.write((json.dumps({"status": "pulling abc", "total": 4, "completed": done}) + "\n").encode())
                self.wfile.flush()
                time.sleep(s["pull_delay"])
            s["models"][name] = s["sizes"].get(name, 1.0)
            self.wfile.write((json.dumps({"status": "success"}) + "\n").encode())
            return
        if self.path == "/api/create":
            s["models"][body["model"]] = s["models"].get(body["from"], 1.0)
            return self._json(200, {"status": "success"})
        if self.path == "/api/chat":
            if body["model"] not in s["models"]:
                return self._json(404, {"error": "model not found"})
            s["loaded"][body["model"]] = s["models"][body["model"]] * 1.25
            return self._json(200, {"message": {"role": "assistant", "content": s["reply"]}})
        if self.path == "/api/generate":
            if body.get("keep_alive") in (0, "0"):
                s["loaded"].pop(body["model"], None)
            return self._json(200, {"response": ""})
        self._json(404, {"error": "not found"})

    def do_DELETE(self):
        s = FakeOllama.state
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        s["calls"].append((self.path, body))
        if self.path == "/api/delete" and body.get("model") in s["models"] and body["model"] not in s["undeletable"]:
            del s["models"][body["model"]]
            return self._json(200, {})
        self._json(404, {"error": "model not found"})


class FakeRegistry(BaseHTTPRequestHandler):
    sizes = {}  # "library/qwen3/manifests/8b" -> bytes

    def log_message(self, *a):
        pass

    def do_GET(self):
        key = self.path.split("/v2/", 1)[-1]
        if key not in FakeRegistry.sizes:
            self.send_response(404)
            self.end_headers()
            return
        data = json.dumps({"config": {"size": 100}, "layers": [{"size": FakeRegistry.sizes[key]}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(data)


def serve(handler):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


@unittest.skipUnless(base.loopback_bind_available(), "needs loopback sockets")
class RunnerModels(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ollama = serve(FakeOllama)
        cls.registry = serve(FakeRegistry)

    @classmethod
    def tearDownClass(cls):
        for s in (cls.ollama, cls.registry):
            s.shutdown()
            s.server_close()

    def setUp(self):
        FakeOllama.state = {"version": "0.17.7", "models": {"llama3.2:latest": 2.0}, "loaded": {}, "calls": [],
                            "pull_errors": {}, "pull_delay": 0.05, "sizes": {"qwen3:8b": 5.2, "tiny:1b": 0.8},
                            "reply": "ready", "undeletable": set()}
        FakeRegistry.sizes = {"library/qwen3/manifests/8b": int(5.2 * GB), "library/tiny/manifests/1b": int(0.8 * GB),
                              "library/huge/manifests/70b": int(40 * GB), "library/gemma3/manifests/12b": int(8.2 * GB)}
        self.temp = tempfile.TemporaryDirectory()
        self.fleet = Path(os.path.realpath(self.temp.name)) / "fleet"
        self.fleet.mkdir()
        self.proc = None
        self.token = "tok-" + "m" * 40

    def tearDown(self):
        self.stop()
        self.temp.cleanup()

    def start(self, models_cfg=None, ram_gb=16, free_disk_gb=500):
        cfg = {"max_mode": "auto-edit", "allowed_roots": [], "gate_profiles": {},
               "models": {"remote_management": True, "disk_reserve_gb": 0, "ram_reserve_gb": 4, "max_loaded": 1}}
        if models_cfg is not None:
            cfg["models"] = models_cfg
        (self.fleet / "runner.json").write_text(json.dumps(cfg))
        self.port = base.free_port()
        env = os.environ.copy()
        env.update({"FLEET_DIR": str(self.fleet), "RUNNER_BIND": "127.0.0.1:%d" % self.port, "RUNNER_TOKEN": self.token,
                    "OLLAMA_URL": "http://127.0.0.1:%d" % self.ollama.server_address[1],
                    "OLLAMA_REGISTRY": "http://127.0.0.1:%d/v2" % self.registry.server_address[1],
                    "FLEET_RAM_GB": str(ram_gb),  # test hooks: pretend this much RAM and free disk
                    "FLEET_FREE_DISK_GB": str(free_disk_gb)})
        self.proc = subprocess.Popen([sys.executable, str(base.RUNNER)], env=env, stdin=subprocess.DEVNULL,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        end = time.time() + 10
        while time.time() < end:
            try:
                if self.req("GET", "/health")[0] == 200:
                    return
            except OSError:
                pass
            time.sleep(0.05)
        raise RuntimeError("runner did not start")

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            self.proc.wait(timeout=5)
        self.proc = None

    def req(self, method, path, body=None):
        r = urllib.request.Request("http://127.0.0.1:%d%s" % (self.port, path),
                                   data=None if body is None else json.dumps(body).encode(), method=method,
                                   headers={"Content-Type": "application/json", "X-Runner-Token": self.token})
        try:
            with urllib.request.urlopen(r, timeout=5) as resp:
                return resp.status, json.load(resp)
        except urllib.error.HTTPError as e:
            return e.code, json.load(e)

    def wait_op(self, op_id, timeout=10):
        end = time.time() + timeout
        op = None
        while time.time() < end:
            _, op = self.req("GET", "/operations/" + op_id)
            if op["state"] in ("ready", "removed", "failed", "blocked", "cancelled"):
                return op
            time.sleep(0.05)
        self.fail("operation stuck: %r" % op)

    # ---------- tests ----------

    def test_models_listing(self):
        self.start()
        status, m = self.req("GET", "/models")
        self.assertEqual(200, status)
        self.assertEqual("ollama", m["engine"])
        self.assertEqual("0.17.7", m["version"])
        self.assertEqual([{"name": "llama3.2:latest", "size_gb": 2.0, "digest": "sha256:llama3.2latest", "managed": False}],
                         m["models"])
        self.assertIn("free_disk_gb", m)
        _, h = self.req("GET", "/health")
        self.assertTrue(h["models"]["remote_management"])

    def test_ensure_pulls_configures_verifies_and_reports_progress(self):
        self.start()
        status, op = self.req("POST", "/models/ensure", {"ref": "qwen3:8b", "derived": "fleet-qwen3-8b",
                                                         "params": {"num_ctx": 8192, "temperature": 0.2, "think": False}})
        self.assertEqual(200, status, op)
        seen = set()
        end = time.time() + 10
        while time.time() < end:
            _, cur = self.req("GET", "/operations/" + op["id"])
            seen.add(cur["state"])
            if cur["state"] == "pulling" and cur["progress"].get("total"):
                seen.add("progress")
            if cur["state"] in ("ready", "failed", "blocked"):
                break
            time.sleep(0.02)
        self.assertEqual("ready", cur["state"], cur)
        self.assertIn("progress", seen)
        self.assertAlmostEqual(5.2, cur["download_gb"], places=1)
        self.assertAlmostEqual(6.5, cur["loaded_gb"], places=1)
        calls = [c[0] for c in FakeOllama.state["calls"]]
        self.assertLess(calls.index("/api/pull"), calls.index("/api/create"))
        create = next(b for p, b in FakeOllama.state["calls"] if p == "/api/create")
        self.assertEqual({"model": "fleet-qwen3-8b", "from": "qwen3:8b", "parameters": {"num_ctx": 8192, "temperature": 0.2}},
                         {k: create[k] for k in ("model", "from", "parameters")})
        chat = next(b for p, b in FakeOllama.state["calls"] if p == "/api/chat")
        self.assertEqual("fleet-qwen3-8b", chat["model"])
        self.assertIs(False, chat["think"])
        self.assertEqual({}, FakeOllama.state["loaded"])  # unloaded after the smoke test
        _, m = self.req("GET", "/models")
        managed = {x["name"]: x["managed"] for x in m["models"]}
        self.assertTrue(managed["qwen3:8b"])
        self.assertTrue(managed["fleet-qwen3-8b"])
        self.assertFalse(managed["llama3.2:latest"])

    def test_ensure_is_idempotent_and_one_op_at_a_time(self):
        FakeOllama.state["pull_delay"] = 0.3
        self.start()
        _, op = self.req("POST", "/models/ensure", {"ref": "qwen3:8b"})
        status, body = self.req("POST", "/models/ensure", {"ref": "tiny:1b"})
        self.assertEqual(409, status)
        self.assertIn(op["id"], body["detail"])
        self.assertEqual("ready", self.wait_op(op["id"])["state"])
        pulls = sum(1 for p, _ in FakeOllama.state["calls"] if p == "/api/pull")
        _, again = self.req("POST", "/models/ensure", {"ref": "qwen3:8b"})
        self.assertEqual("ready", self.wait_op(again["id"])["state"])
        self.assertEqual(pulls, sum(1 for p, _ in FakeOllama.state["calls"] if p == "/api/pull"))

    def test_ensure_for_the_same_ref_returns_the_running_operation(self):
        FakeOllama.state["pull_delay"] = 0.3
        self.start()
        _, op = self.req("POST", "/models/ensure", {"ref": "qwen3:8b"})
        status, again = self.req("POST", "/models/ensure", {"ref": "qwen3:8b"})
        self.assertEqual(200, status)
        self.assertEqual(op["id"], again["id"])
        self.wait_op(op["id"])

    def test_refused_without_remote_management(self):
        self.start({"remote_management": False})
        self.assertEqual(403, self.req("POST", "/models/ensure", {"ref": "tiny:1b"})[0])
        self.assertEqual(403, self.req("POST", "/models/remove", {"ref": "llama3.2:latest"})[0])

    def test_bad_refs_and_params_are_rejected(self):
        self.start()
        for body in ({"ref": "qwen3:8b; rm -rf /"}, {"ref": "../etc"}, {"ref": "Qwen3:8b"}, {"ref": ""},
                     {"ref": "qwen3:8b", "params": {"num_gpu": 99}}, {"ref": "qwen3:8b", "params": {"num_ctx": 10}},
                     {"ref": "qwen3:8b", "params": {"temperature": 5}}, {"ref": "qwen3:8b", "derived": "evil"},
                     {"ref": "qwen3:8b", "surprise": 1}):
            self.assertEqual(400, self.req("POST", "/models/ensure", body)[0], body)

    def test_no_space_and_wont_fit_block_before_pulling(self):
        self.start({"remote_management": True, "disk_reserve_gb": 100000, "ram_reserve_gb": 4})
        op = self.wait_op(self.req("POST", "/models/ensure", {"ref": "tiny:1b"})[1]["id"])
        self.assertEqual("blocked", op["state"])
        self.assertTrue(op["reason"].startswith("no_space"), op)
        self.stop()
        self.start(ram_gb=16)
        op = self.wait_op(self.req("POST", "/models/ensure", {"ref": "huge:70b"})[1]["id"])
        self.assertEqual("blocked", op["state"])
        self.assertTrue(op["reason"].startswith("wont_fit"), op)
        self.assertNotIn("/api/pull", [p for p, _ in FakeOllama.state["calls"]])
        unknown = self.wait_op(self.req("POST", "/models/ensure", {"ref": "ghost:1b"})[1]["id"])
        self.assertEqual("blocked", unknown["state"])

    def test_override_fit(self):
        FakeOllama.state["sizes"]["huge:70b"] = 40
        self.start(ram_gb=16)
        op = self.wait_op(self.req("POST", "/models/ensure", {"ref": "huge:70b", "override_fit": True})[1]["id"])
        self.assertEqual("ready", op["state"], op)

    def test_engine_too_old_is_blocked_with_reason(self):
        FakeOllama.state["pull_errors"]["gemma3:12b"] = ("pull model manifest: 412: The model you are attempting to pull "
                                                         "requires a newer version of Ollama.")
        self.start()
        op = self.wait_op(self.req("POST", "/models/ensure", {"ref": "gemma3:12b"})[1]["id"])
        self.assertEqual("blocked", op["state"])
        self.assertTrue(op["reason"].startswith("engine_upgrade:"), op)
        self.assertIn("0.17.7", op["reason"])

    def test_failed_smoke_test_is_not_ready(self):
        FakeOllama.state["reply"] = ""
        self.start()
        op = self.wait_op(self.req("POST", "/models/ensure", {"ref": "tiny:1b"})[1]["id"])
        self.assertEqual("failed", op["state"])

    def test_remove_managed_and_protect_preexisting(self):
        self.start()
        self.wait_op(self.req("POST", "/models/ensure", {"ref": "tiny:1b", "derived": "fleet-tiny-1b",
                                                         "params": {"temperature": 0}})[1]["id"])
        status, body = self.req("POST", "/models/remove", {"ref": "llama3.2:latest"})
        self.assertEqual(409, status)
        self.assertIn("pre-existing", body["detail"])
        op = self.wait_op(self.req("POST", "/models/remove", {"ref": "tiny:1b", "derived": "fleet-tiny-1b"})[1]["id"])
        self.assertEqual("removed", op["state"], op)
        self.assertNotIn("tiny:1b", FakeOllama.state["models"])
        self.assertNotIn("fleet-tiny-1b", FakeOllama.state["models"])
        op = self.wait_op(self.req("POST", "/models/remove", {"ref": "llama3.2:latest", "confirm_preexisting": True})[1]["id"])
        self.assertEqual("removed", op["state"])

    def test_failed_delete_is_not_removed(self):
        self.start()
        self.wait_op(self.req("POST", "/models/ensure", {"ref": "tiny:1b"})[1]["id"])
        FakeOllama.state["undeletable"].add("tiny:1b")
        op = self.wait_op(self.req("POST", "/models/remove", {"ref": "tiny:1b"})[1]["id"])
        self.assertEqual("failed", op["state"])

    def test_restart_mid_pull_resumes(self):
        FakeOllama.state["pull_delay"] = 0.4
        self.start()
        _, op = self.req("POST", "/models/ensure", {"ref": "qwen3:8b"})
        time.sleep(0.5)
        self.stop()
        self.start()
        done = self.wait_op(op["id"], timeout=15)
        self.assertEqual("ready", done["state"])
        _, ops = self.req("GET", "/operations")
        self.assertEqual(1, sum(1 for o in ops if o["ref"] == "qwen3:8b"))
        self.assertEqual(0o600, os.stat(self.fleet / "operations" / (op["id"] + ".json")).st_mode & 0o777)

    def test_cancel_pull(self):
        FakeOllama.state["pull_delay"] = 0.5
        self.start()
        _, op = self.req("POST", "/models/ensure", {"ref": "qwen3:8b"})
        time.sleep(0.3)
        self.assertEqual(200, self.req("POST", "/operations/%s/cancel" % op["id"], {})[0])
        self.assertEqual("cancelled", self.wait_op(op["id"])["state"])


if __name__ == "__main__":
    unittest.main()
