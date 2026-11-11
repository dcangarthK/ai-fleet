"""Phase 6 runner side (docs/SPEC-phase6-policy.md): instructions delivery and narrowing-only policy.
Run: python3 -m unittest -v tests/test_runner_policy.py
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

RECORDER = r'''#!{python}
import json, os, sys, time
name = os.path.basename(sys.argv[0])
stdin = sys.stdin.read()
rec = os.environ["FAKE_RECORD"]
with open(os.path.join(rec, "%s-%d.json" % (name, time.time_ns())), "w") as f:
    json.dump({{"argv": sys.argv[1:], "stdin": stdin}}, f)
if "sleep" in stdin:
    time.sleep(30)
if name == "claude":
    print(json.dumps({{"type": "system", "session_id": "s1"}}))
    print(json.dumps({{"type": "result", "session_id": "s1", "result": "ok", "permission_denials": []}}))
else:
    print(json.dumps({{"type": "thread.started", "thread_id": "th1"}}))
    print(json.dumps({{"type": "item.completed", "item": {{"type": "agent_message", "text": "ok"}}}}))
'''


class RecordingOllama(BaseHTTPRequestHandler):
    seen = []

    def log_message(self, *a):
        pass

    def _json(self, body):
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self._json({"models": [{"name": "m:1b"}]} if self.path == "/api/tags" else {})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
        RecordingOllama.seen.append(body)
        self._json({"message": {"role": "assistant", "content": "ok"}})


@unittest.skipUnless(base.loopback_bind_available(), "needs loopback sockets")
class RunnerPolicy(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ollama = ThreadingHTTPServer(("127.0.0.1", 0), RecordingOllama)
        threading.Thread(target=cls.ollama.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.ollama.shutdown()
        cls.ollama.server_close()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(os.path.realpath(self.temp.name))
        self.fleet, self.bin, self.rec = self.base / "fleet", self.base / "bin", self.base / "rec"
        for d in (self.fleet, self.bin, self.rec):
            d.mkdir()
        for name in ("claude", "codex"):
            p = self.bin / name
            p.write_text(RECORDER.format(python=sys.executable))
            p.chmod(0o755)
        self.root = self.base / "code"
        (self.root / "proj" / "sub").mkdir(parents=True)
        (self.root / "other").mkdir()
        self.token = "tok-" + "p" * 40
        self.proc = None
        RecordingOllama.seen = []

    def tearDown(self):
        for st in self.fleet.glob("jobs/*/state.json"):
            try:
                j = json.loads(st.read_text())
                if j.get("state") == "running" and j.get("pid"):
                    os.killpg(j["pid"], 9)
            except (OSError, ValueError):
                pass
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            self.proc.wait(timeout=5)
        self.temp.cleanup()

    def start(self, extra=None):
        cfg = {"max_mode": "auto-edit", "allowed_roots": [str(self.root)], "gate_profiles": {}, "max_job_minutes": 120}
        cfg.update(extra or {})
        (self.fleet / "runner.json").write_text(json.dumps(cfg))
        self.port = base.free_port()
        env = os.environ.copy()
        env.update({"FLEET_DIR": str(self.fleet), "RUNNER_BIND": "127.0.0.1:%d" % self.port, "RUNNER_TOKEN": self.token,
                    "OLLAMA_URL": "http://127.0.0.1:%d" % self.ollama.server_address[1], "FAKE_RECORD": str(self.rec),
                    "PATH": str(self.bin) + os.pathsep + env.get("PATH", "")})
        self.proc = subprocess.Popen([sys.executable, str(base.RUNNER)], env=env, stdin=subprocess.DEVNULL,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        end = time.time() + 10
        while time.time() < end:
            try:
                if self.req("GET", "/health")[0] == 200:
                    return
            except OSError:
                time.sleep(0.05)
        raise RuntimeError("runner did not start")

    def req(self, method, path, body=None):
        r = urllib.request.Request("http://127.0.0.1:%d%s" % (self.port, path),
                                   data=None if body is None else json.dumps(body).encode(), method=method,
                                   headers={"Content-Type": "application/json", "X-Runner-Token": self.token})
        try:
            with urllib.request.urlopen(r, timeout=5) as resp:
                return resp.status, json.load(resp)
        except urllib.error.HTTPError as e:
            return e.code, json.load(e)

    def create(self, job_id, **kw):
        body = {"id": job_id, "agent": "claude", "prompt": "do the task", "mode": "auto-edit", "repo": None,
                "worktree": False, "cwd": None, "model": None, "gate": None, "allowed_tools": [], "resume_session": None}
        body.update(kw)
        return self.req("POST", "/jobs", body)

    def wait_done(self, job_id):
        end = time.time() + 10
        while time.time() < end:
            _, j = self.req("GET", "/jobs/" + job_id)
            if j["state"] != "running":
                return j
            time.sleep(0.05)
        self.fail("job stuck")

    def records(self, agent):
        out = []
        for p in sorted(self.rec.glob(agent + "-*.json")):
            out.append(json.loads(p.read_text()))
        return out

    # ---------- tests ----------

    def test_health_advertises_policy_support(self):
        self.start()
        _, h = self.req("GET", "/health")
        self.assertTrue(h["policy"])
        self.assertEqual("deny", h["network"]["agents"])

    def test_instructions_reach_claude_and_codex_on_stdin(self):
        self.start()
        for agent in ("claude", "codex"):
            status, _ = self.create("i-" + agent, agent=agent, instructions="## fleet\nAlways write tests.",
                                    policy={}, policy_hash="h" * 64)
            self.assertEqual(200, status)
            self.wait_done("i-" + agent)
            stdin = self.records(agent)[-1]["stdin"]
            self.assertTrue(stdin.startswith("<fleet-instructions>\n## fleet\nAlways write tests.\n</fleet-instructions>\n\n"), stdin)
            self.assertTrue(stdin.endswith("do the task"))
            self.assertNotIn("Always write tests", " ".join(self.records(agent)[-1]["argv"]))  # never on argv

    def test_instructions_reach_ollama_as_system_message(self):
        self.start()
        self.create("i-ollama", agent="ollama", model="m:1b", instructions="Be brief.", policy={}, policy_hash="h" * 64)
        self.wait_done("i-ollama")
        msgs = RecordingOllama.seen[-1]["messages"]
        self.assertEqual({"role": "system", "content": "Be brief."}, msgs[0])
        self.assertEqual("user", msgs[1]["role"])

    def test_resume_reuses_instructions(self):
        self.start()
        self.create("r1", instructions="Keep it small.", policy={}, policy_hash="h" * 64)
        self.wait_done("r1")
        self.assertEqual(200, self.req("POST", "/jobs/r1/message", {"text": "and also this"})[0])
        self.wait_done("r1")
        stdin = self.records("claude")[-1]["stdin"]
        self.assertIn("Keep it small.", stdin)
        self.assertTrue(stdin.endswith("and also this"))

    def test_policy_roots_narrow_but_never_widen(self):
        self.start()
        narrow = {"filesystem": {"allowed_roots": [str(self.root / "proj")]}}
        self.assertEqual(200, self.create("ok", cwd=str(self.root / "proj" / "sub"), policy=narrow, policy_hash="h" * 64)[0])
        self.assertEqual(403, self.create("narrowed", cwd=str(self.root / "other"), policy=narrow, policy_hash="h" * 64)[0])
        wide = {"filesystem": {"allowed_roots": ["/"]}}
        outside = self.base / "outside"
        outside.mkdir()
        self.assertEqual(403, self.create("widen", cwd=str(outside), policy=wide, policy_hash="h" * 64)[0])
        deny = {"filesystem": {"denied": [str(self.root / "proj")]}}
        self.assertEqual(403, self.create("denied", cwd=str(self.root / "proj" / "sub"), policy=deny, policy_hash="h" * 64)[0])

    def test_claude_tools_and_network(self):
        self.start()
        pol = {"tools": {"claude_deny": ["Bash"], "claude_allow": ["Read", "Edit"]}, "network": {"agents": "deny"}}
        self.create("tools", policy=pol, policy_hash="h" * 64, allowed_tools=["Read", "Write"])
        self.wait_done("tools")
        argv = self.records("claude")[-1]["argv"]
        denied = argv[argv.index("--disallowedTools") + 1].split(",")
        self.assertEqual(sorted(denied), ["Bash", "WebFetch", "WebSearch"])
        allowed = argv[argv.index("--allowedTools") + 1].split(",")
        self.assertEqual(allowed, ["Read"])  # request ∩ policy

    def test_codex_network_needs_both_policy_and_node(self):
        self.start()
        self.create("c1", agent="codex", policy={"network": {"agents": "allow"}}, policy_hash="h" * 64)
        self.wait_done("c1")
        self.assertNotIn("sandbox_workspace_write.network_access=true", " ".join(self.records("codex")[-1]["argv"]))
        self.proc.terminate()
        self.proc.wait(timeout=5)
        self.start({"network": {"agents": "allow"}})
        self.create("c2", agent="codex", policy={"network": {"agents": "allow"}}, policy_hash="h" * 64)
        self.wait_done("c2")
        self.assertIn("sandbox_workspace_write.network_access=true", " ".join(self.records("codex")[-1]["argv"]))
        self.create("c3", agent="codex", policy={"network": {"agents": "deny"}}, policy_hash="h" * 64)
        self.wait_done("c3")
        self.assertNotIn("sandbox_workspace_write.network_access=true", " ".join(self.records("codex")[-1]["argv"]))

    def test_task_minutes_take_the_minimum(self):
        self.start({"max_job_minutes": 120})
        self.create("slow", prompt="sleep", policy={"limits": {"task_minutes": 0.03}}, policy_hash="h" * 64)
        end = time.time() + 15
        while time.time() < end:
            _, j = self.req("GET", "/jobs/slow")
            if j["state"] != "running":
                break
            time.sleep(0.1)
        self.assertEqual(124, j["exit_code"])

    def test_policy_fields_are_validated(self):
        self.start()
        for pol in ({"filesystem": {"allowed_roots": "x"}}, {"surprise": 1}, {"network": {"agents": "maybe"}},
                    {"tools": {"claude_deny": ["rm -rf /"]}}):
            self.assertEqual(400, self.create("bad", policy=pol, policy_hash="h" * 64)[0], pol)
        self.assertEqual(400, self.create("big", instructions="x" * 40000, policy={}, policy_hash="h" * 64)[0])


if __name__ == "__main__":
    unittest.main()
