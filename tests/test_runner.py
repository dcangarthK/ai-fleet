import json
import io
import importlib.util
import os
import signal
import socket
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
from types import SimpleNamespace
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "nodes" / "runner.py"


FAKE_AGENT = r'''#!{python}
import json
import os
import sys
import time

name = os.path.basename(sys.argv[0])
args = sys.argv[1:]
resume = "--resume" in args or (name == "codex" and "resume" in args)
prompt = sys.stdin.read()  # the runner sends prompts on stdin, never argv
if "-" in args:
    args.remove("-")
if prompt and prompt in sys.argv:
    sys.exit("prompt leaked into argv")
session = "claude-session-1"
if name == "claude" and "--resume" in args:
    session = args[args.index("--resume") + 1]

def emit(value):
    print(json.dumps(value), flush=True)

if name == "claude":
    emit({{"type": "system", "subtype": "init", "session_id": session}})
    emit({{"type": "assistant", "message": {{"content": [
        {{"type": "text", "text": "working on " + prompt}},
        {{"type": "tool_use", "name": "Write", "input": {{"file": "untracked.txt"}}}}
    ]}}}})
else:
    session = args[-2] if resume and len(args) > 1 else "codex-thread-1"
    emit({{"type": "thread.started", "thread_id": session}})
    emit({{"type": "item.started", "item": {{"type": "command_execution", "command": "fake command"}}}})

if "sleep" in prompt:
    time.sleep(30)

if "edit" in prompt:
    with open("tracked.txt", "a", encoding="utf-8") as handle:
        handle.write("changed by agent\n")
    with open("untracked.txt", "w", encoding="utf-8") as handle:
        handle.write("new file\n")

if name == "claude":
    denials = [{{"tool_name": "Bash", "tool_input": {{"command": "danger"}}}}] if "deny" in prompt else []
    result = ("resumed: " if resume else "finished: ") + prompt
    emit({{"type": "result", "subtype": "success", "session_id": session,
          "result": result, "permission_denials": denials}})
else:
    result = ("resumed: " if resume else "finished: ") + prompt
    emit({{"type": "item.completed", "item": {{"type": "agent_message", "text": result}}}})
sys.exit(2 if "agent-fail" in prompt else 0)
'''


class OllamaHandler(BaseHTTPRequestHandler):
    def log_message(self, _format, *_args):
        return

    def do_GET(self):
        if self.path == "/api/tags":
            self._json({"models": [{"name": "fake-model"}]})
        else:
            self.send_error(404)

    def do_POST(self):
        if self.path != "/api/chat":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", "0"))
        request = json.loads(self.rfile.read(length))
        prompt = request["messages"][0]["content"]
        self._json({"message": {"role": "assistant", "content": "ollama: " + prompt}})

    def _json(self, value):
        data = json.dumps(value).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def free_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def loopback_bind_available():
    sock = socket.socket()
    try:
        sock.bind(("127.0.0.1", 0))
        return True
    except PermissionError:
        return False
    finally:
        sock.close()


class RunnerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.base = Path(cls.temp.name)
        cls.fleet = cls.base / "fleet"
        cls.bin = cls.base / "bin"
        cls.bin.mkdir()
        fake = FAKE_AGENT.format(python=sys.executable)
        for name in ("claude", "codex"):
            path = cls.bin / name
            path.write_text(fake, encoding="utf-8")
            path.chmod(0o755)

        cls.http_enabled = loopback_bind_available()
        cls.ollama = None
        if cls.http_enabled:
            cls.ollama_port = free_port()
            cls.ollama = ThreadingHTTPServer(("127.0.0.1", cls.ollama_port), OllamaHandler)
            cls.ollama_thread = threading.Thread(target=cls.ollama.serve_forever, daemon=True)
            cls.ollama_thread.start()
        else:
            cls.ollama_port = 1
            spec = importlib.util.spec_from_file_location("fleet_runner_under_test", RUNNER)
            cls.runner_module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(cls.runner_module)

            def fake_agents(_url):
                return {
                    "claude": str(cls.bin / "claude"),
                    "codex": str(cls.bin / "codex"),
                    "ollama": "http://fake-ollama.test",
                }
            cls.runner_module.find_agents = fake_agents

        cls.repo = cls.base / "repo"
        cls.repo.mkdir()
        cls.git("init", "-b", "main")
        cls.git("config", "user.email", "runner@example.test")
        cls.git("config", "user.name", "Runner Test")
        (cls.repo / "tracked.txt").write_text("original\n", encoding="utf-8")
        cls.git("add", "tracked.txt")
        cls.git("commit", "-m", "initial")

        cls.port = free_port() if cls.http_enabled else 0
        cls.token = "test-secret"
        cls.process = None
        cls.start_runner()

    @classmethod
    def tearDownClass(cls):
        # Best-effort cleanup for a failed test that left a detached fake running.
        for state_path in cls.fleet.glob("jobs/*/state.json"):
            try:
                state = json.loads(state_path.read_text())
                if state.get("state") == "running" and state.get("pid") and os.name != "nt":
                    os.killpg(state["pid"], signal.SIGKILL)
            except (OSError, ValueError):
                pass
        cls.stop_runner()
        if cls.ollama:
            cls.ollama.shutdown()
            cls.ollama.server_close()
        cls.temp.cleanup()

    @classmethod
    def git(cls, *args):
        return subprocess.run(
            ["git", *args], cwd=cls.repo, check=True, capture_output=True, text=True,
        )

    @classmethod
    def start_runner(cls):
        if not cls.http_enabled:
            cls.manager = cls.runner_module.JobManager(cls.fleet, "http://fake-ollama.test")
            original_command = cls.manager._command

            def command(job, prompt, allowed_tools, resume):
                if job["agent"] == "ollama":
                    code = ('import json; print(json.dumps({"type":"ollama.result",'
                            '"text":%r}))' % ("ollama: " + prompt))
                    return [sys.executable, "-c", code]
                return original_command(job, prompt, allowed_tools, resume)
            cls.manager._command = command
            cls.process = None
            return
        env = os.environ.copy()
        env.update({
            "FLEET_DIR": str(cls.fleet),
            "RUNNER_BIND": "127.0.0.1:%d" % cls.port,
            "RUNNER_TOKEN": cls.token,
            "OLLAMA_URL": "http://127.0.0.1:%d" % cls.ollama_port,
            "PATH": str(cls.bin) + os.pathsep + env.get("PATH", ""),
        })
        cls.process = subprocess.Popen(
            [sys.executable, str(RUNNER)], env=env,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                status, _ = cls.request("GET", "/health")
                if status == 200:
                    return
            except OSError:
                pass
            if cls.process.poll() is not None:
                raise RuntimeError("runner exited during startup")
            time.sleep(0.05)
        raise RuntimeError("runner did not start")

    @classmethod
    def stop_runner(cls):
        if not cls.http_enabled:
            cls.manager = None
            return
        if cls.process and cls.process.poll() is None:
            cls.process.terminate()
            try:
                cls.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                cls.process.kill()
                cls.process.wait(timeout=5)

    @classmethod
    def request(cls, method, path, body=None, token=True):
        if not cls.http_enabled:
            if not token:
                return 401, {"detail": "invalid runner token"}
            parsed = urlparse(path)
            encoded = b"" if body is None else json.dumps(body).encode()
            handler = object.__new__(cls.runner_module.Handler)
            handler.path = path
            handler.server = SimpleNamespace(manager=cls.manager, runner_token=cls.token)
            handler.headers = {
                "Content-Type": "application/json",
                "Content-Length": str(len(encoded)),
                "X-Runner-Token": cls.token,
            }
            handler.rfile = io.BytesIO(encoded)
            try:
                return 200, handler._route(method)
            except cls.runner_module.HttpError as exc:
                return exc.status, {"detail": exc.detail}
        data = None if body is None else json.dumps(body).encode()
        headers = {"Content-Type": "application/json"}
        if token:
            headers["X-Runner-Token"] = cls.token
        request = urllib.request.Request(
            "http://127.0.0.1:%d%s" % (cls.port, path), data=data,
            headers=headers, method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as exc:
            return exc.code, json.load(exc)

    def create(self, job_id, **overrides):
        body = {
            "id": job_id, "agent": "claude", "prompt": "hello",
            "mode": "auto-edit", "repo": None, "worktree": False,
            "cwd": None, "model": None, "gate": None,
            "allowed_tools": [], "resume_session": None,
        }
        body.update(overrides)
        return self.request("POST", "/jobs", body)

    def wait_job(self, job_id, predicate=None, timeout=10):
        predicate = predicate or (lambda job: job["state"] != "running")
        deadline = time.time() + timeout
        latest = None
        while time.time() < deadline:
            status, latest = self.request("GET", "/jobs/" + job_id)
            self.assertEqual(200, status)
            if predicate(latest):
                return latest
            time.sleep(0.05)
        self.fail("job %s did not reach expected state: %r" % (job_id, latest))

    def test_01_health_token_mode_ceiling_and_validation(self):
        status, body = self.request("GET", "/health", token=False)
        self.assertEqual(401, status)
        self.assertIn("detail", body)

        status, health = self.request("GET", "/health")
        self.assertEqual(200, status)
        self.assertTrue(health["ok"])
        self.assertEqual("1", health["version"])
        self.assertEqual("auto-edit", health["max_mode"])
        self.assertEqual(str(self.bin / "claude"), health["agents"]["claude"])
        self.assertEqual(str(self.bin / "codex"), health["agents"]["codex"])
        self.assertTrue(health["agents"]["ollama"].startswith("http://"))

        status, body = self.create("too-powerful", mode="full")
        self.assertEqual(403, status)
        self.assertIn("max_mode", body["detail"])
        status, _ = self.create("bad-agent", agent="other")
        self.assertEqual(400, status)

    def test_02_spawn_log_result_denials_gate_diff_and_resume(self):
        gate = '%s -c "print(\'gate passed\')"' % sys.executable
        status, job = self.create(
            "claude-main", prompt="edit deny", repo=str(self.repo),
            worktree=True, gate=gate,
        )
        self.assertEqual(200, status)
        self.assertEqual("running", job["state"])
        self.assertEqual("fleet/claude-main", job["branch"])
        self.assertEqual(1, job["runs"])

        job = self.wait_job(
            "claude-main",
            lambda item: item["state"] == "exited" and item["gate"]["state"] in ("passed", "failed"),
        )
        self.assertEqual(0, job["exit_code"])
        self.assertEqual("passed", job["gate"]["state"])
        self.assertIn("gate passed", job["gate"]["output_tail"])
        self.assertEqual("claude-session-1", job["session_id"])
        self.assertEqual("finished: edit deny", job["result"])
        self.assertEqual([{"tool": "Bash", "input": {"command": "danger"}}], job["denials"])

        status, log = self.request("GET", "/jobs/claude-main/log?offset=0")
        self.assertEqual(200, status)
        self.assertIn("working on edit deny", log["text"])
        self.assertIn("tool Write", log["text"])
        status, tail = self.request("GET", "/jobs/claude-main/log?offset=%d" % log["offset"])
        self.assertEqual({"offset": log["offset"], "text": ""}, tail)

        status, diff = self.request("GET", "/jobs/claude-main/diff")
        self.assertEqual(200, status)
        self.assertIn("tracked.txt", diff["stat"])
        self.assertIn("untracked.txt", diff["stat"])
        self.assertIn("changed by agent", diff["diff"])
        self.assertIn("new file", diff["diff"])

        status, resumed = self.request("POST", "/jobs/claude-main/message", {
            "text": "follow up", "allowed_tools": ["Bash"], "mode": "supervised",
        })
        self.assertEqual(200, status)
        self.assertEqual(2, resumed["runs"])
        self.assertEqual("running", resumed["state"])
        job = self.wait_job("claude-main")
        self.assertEqual("resumed: follow up", job["result"])
        self.assertEqual("claude-session-1", job["session_id"])
        self.assertEqual([], job["denials"])

        status, duplicate = self.create("claude-main")
        self.assertEqual(409, status)
        self.assertIn("duplicate", duplicate["detail"])

    def test_03_codex_session_resume_and_failed_gate(self):
        gate = '%s -c "import sys; print(\'gate failed\'); sys.exit(7)"' % sys.executable
        status, _ = self.create("codex-main", agent="codex", prompt="codex work", gate=gate)
        self.assertEqual(200, status)
        job = self.wait_job(
            "codex-main",
            lambda item: item["state"] == "exited" and item["gate"]["state"] == "failed",
        )
        self.assertEqual("codex-thread-1", job["session_id"])
        self.assertEqual("finished: codex work", job["result"])
        self.assertEqual(7, job["gate"]["exit_code"])
        self.assertIn("gate failed", job["gate"]["output_tail"])

        status, _ = self.request("POST", "/jobs/codex-main/message", {
            "text": "codex again", "allowed_tools": [], "mode": "read-only",
        })
        self.assertEqual(200, status)
        job = self.wait_job("codex-main")
        self.assertEqual(2, job["runs"])
        self.assertEqual("resumed: codex again", job["result"])

    def test_04_ollama_and_non_git_diff(self):
        status, _ = self.create(
            "ollama-main", agent="ollama", model="fake-model", prompt="local request",
            mode="read-only",
        )
        self.assertEqual(200, status)
        job = self.wait_job("ollama-main")
        self.assertEqual(0, job["exit_code"])
        self.assertEqual("ollama: local request", job["result"])
        self.assertIsNone(job["session_id"])
        status, _ = self.request("GET", "/jobs/ollama-main/diff")
        self.assertEqual(400, status)

    def test_05_cancel_cleanup_and_branch_is_kept(self):
        status, _ = self.create(
            "cancel-main", prompt="sleep", repo=str(self.repo), worktree=True,
        )
        self.assertEqual(200, status)
        status, _ = self.request("POST", "/jobs/cancel-main/cleanup", {})
        self.assertEqual(409, status)
        status, job = self.request("POST", "/jobs/cancel-main/cancel", {})
        self.assertEqual(200, status)
        self.assertEqual("cancelled", job["state"])
        worktree = Path(job["cwd"])
        status, _ = self.request("POST", "/jobs/cancel-main/cleanup", {})
        self.assertEqual(200, status)
        self.assertFalse(worktree.exists())
        branches = self.git("branch", "--list", "fleet/cancel-main").stdout
        self.assertIn("fleet/cancel-main", branches)

    def test_05b_cleanup_keeps_uncommitted_work_and_gates_need_write_modes(self):
        status, _ = self.create("keep-main", prompt="edit", repo=str(self.repo), worktree=True)
        self.assertEqual(200, status)
        self.wait_job("keep-main")
        status, _ = self.request("POST", "/jobs/keep-main/cleanup", {})
        self.assertEqual(200, status)
        files = self.git("show", "--stat", "--format=%s", "fleet/keep-main").stdout
        self.assertIn("fleet: work from task keep-main", files)
        self.assertIn("untracked.txt", files)

        status, body = self.create("gate-readonly", mode="read-only", gate="true")
        self.assertEqual(400, status)
        self.assertIn("gate", body["detail"])

    def test_06_detached_survives_restart_then_lost_is_detected(self):
        status, _ = self.create("restart-main", prompt="sleep")
        self.assertEqual(200, status)
        state_path = self.fleet / "jobs" / "restart-main" / "state.json"
        deadline = time.time() + 5
        state = None
        while time.time() < deadline:
            state = json.loads(state_path.read_text())
            if state.get("pid"):
                break
            time.sleep(0.05)
        self.assertTrue(state["pid"])
        pid = state["pid"]

        self.stop_runner()
        if os.name != "nt":
            os.kill(pid, 0)  # Detached process survived its runner parent.
        self.start_runner()
        status, job = self.request("GET", "/jobs/restart-main")
        self.assertEqual(200, status)
        self.assertEqual("running", job["state"])

        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
        else:
            os.killpg(pid, signal.SIGKILL)
        job = self.wait_job("restart-main", lambda item: item["state"] == "lost")
        self.assertEqual("lost", job["state"])
        self.assertIsNone(job["exit_code"])

        status, jobs = self.request("GET", "/jobs")
        self.assertEqual(200, status)
        self.assertTrue(any(item["id"] == "restart-main" for item in jobs))


if __name__ == "__main__":
    unittest.main()
