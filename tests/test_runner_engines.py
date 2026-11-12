"""Networking spec §14: local engine adapters, using only fake HTTP engines."""
import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("fleet_runner_engines", ROOT / "nodes" / "runner.py")
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class FakeResponse:
    def __init__(self, body):
        self.body = json.dumps(body).encode()
        self.status = 200

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self, *_args):
        return self.body


FIXTURES = ROOT / "tests" / "fixtures" / "dmr"


class FakeOpenAIEngine:
    calls = []
    use_fixture = False

    @classmethod
    def urlopen(cls, request, timeout=None):
        if isinstance(request, str):
            cls.calls.append(("GET", request, None))
            return FakeResponse({"models": []})
        body = json.loads(request.data) if request.data else None
        cls.calls.append((request.get_method(), request.full_url, body))
        if request.full_url.endswith("/models"):
            if cls.use_fixture:
                return FakeResponse(json.loads((FIXTURES / "models.json").read_text()))
            return FakeResponse({"data": [{"id": "ai/qwen3:latest"}, {"id": "shared-name"}]})
        if request.full_url.endswith("/chat/completions"):
            return FakeResponse({"choices": [{"message": {"role": "assistant",
                                                            "content": "dmr: " + body["messages"][-1]["content"]}}]})
        raise AssertionError(request.full_url)


class EngineAdapterTests(unittest.TestCase):
    base_url = "http://127.0.0.1:12434/engines/v1"

    def setUp(self):
        FakeOpenAIEngine.calls = []
        FakeOpenAIEngine.use_fixture = False
        mocked = patch.object(runner, "loopback_urlopen", FakeOpenAIEngine.urlopen)
        mocked.start()
        self.addCleanup(mocked.stop)

    def manager(self, config=None):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        (root / "runner.json").write_text(json.dumps(config or {}))
        manager = runner.JobManager(root, "http://127.0.0.1:1")
        manager.agents["ollama"] = "http://127.0.0.1:1"
        for adapter in manager.engine_adapters.values():
            adapter.docker_run = lambda *_args, **_kwargs: None
        return manager

    def test_dmr_config_refuses_non_loopback_base_urls(self):
        for url in ("http://192.168.1.2:12434/engines/v1", "http://0.0.0.0:12434/v1", "https://example.test/v1"):
            with self.subTest(url=url), self.assertRaisesRegex(ValueError, "loopback"):
                self.manager({"engines": {"dmr": {"enabled": True, "base_url": url}}})
        manager = self.manager({"engines": {"dmr": {"enabled": True}}})
        self.assertEqual(runner.DMR_DEFAULT_URL, manager.engine_adapters["dmr"].base_url)
        self.assertEqual("http://[::1]:12434/v1", runner.loopback_base_url("http://[::1]:12434/v1"))

    def test_dmr_catalog_health_and_namespaces_are_distinct(self):
        manager = self.manager({"engines": {"dmr": {"enabled": True, "base_url": self.base_url}}})
        catalog = manager.engine_catalog()
        self.assertEqual(["ai/qwen3:latest", "shared-name"], [m["reference"] for m in catalog["dmr"]["models"]])
        self.assertEqual("dmr", catalog["dmr"]["models"][0]["engine"])
        self.assertTrue(catalog["dmr"]["reachable"])
        self.assertEqual("unknown", catalog["dmr"]["models"][0]["warm"])
        self.assertNotEqual(("dmr", "shared-name"), ("ollama", "shared-name"))
        self.assertFalse(any(method == "POST" for method, _path, _body in FakeOpenAIEngine.calls),
                         "health/catalog must never make an inference call")

    def test_docker_model_ls_parser_contains_bad_or_unknown_fields(self):
        output = '{"Model":"ai/qwen3:latest","ID":"sha256:abc"}\n{"Model":"shared-name"}\nnot-json\n'
        self.assertEqual([
            {"engine": "dmr", "reference": "ai/qwen3:latest", "digest": "sha256:abc", "size": None,
             "warm": "unknown", "warm_source": None},
            {"engine": "dmr", "reference": "shared-name", "digest": None, "size": None,
             "warm": "unknown", "warm_source": None},
        ], runner.parse_docker_model_ls(output))
        self.assertEqual([], runner.parse_docker_model_ls(None))

    def test_real_dmr_fixtures_models_endpoint_and_docker_table(self):
        FakeOpenAIEngine.use_fixture = True
        manager = self.manager({"engines": {"dmr": {"enabled": True, "base_url": self.base_url}}})
        table = (FIXTURES / "docker_model_ls.txt").read_text()
        for adapter in manager.engine_adapters.values():
            adapter.docker_run = lambda *_a, **_k: table
        catalog = manager.engine_catalog()["dmr"]
        self.assertTrue(catalog["reachable"])
        self.assertEqual(["ai/deepseek-r1-distill-llama", "ai/gemma3:4B-Q4_0"],
                         [m["reference"] for m in catalog["models"]])
        parsed = runner.parse_docker_model_ls(table)
        self.assertEqual([("ai/deepseek-r1-distill-llama", "88795a9b69fc"), ("ai/gemma3:4B-Q4_0", "11951c7273a7")],
                         [(m["reference"], m["digest"]) for m in parsed])

    def test_dmr_job_uses_openai_chat_endpoint_and_unknown_engine_is_refused(self):
        manager = self.manager({"engines": {"dmr": {"enabled": True, "base_url": self.base_url}}})
        body = {"id": "dmr-job", "agent": "ollama", "engine": "dmr", "model": "ai/qwen3:latest",
                "prompt": "hello", "mode": "read-only", "repo": None, "worktree": False, "cwd": None,
                "gate": None, "allowed_tools": [], "resume_session": None}
        original_launch = manager._launch
        manager._launch = lambda *_args: None
        job = manager.create(body)
        manager.get("dmr-job").update(state="running", ended_at=None, pid=os.getpid())
        health_model = runner.health_report(manager)["engines"]["dmr"]["models"][0]
        self.assertEqual((True, "active_runner_job"), (health_model["warm"], health_model["warm_source"]))
        command = manager._command(manager.get("dmr-job"), "hello", [], False)
        self.assertEqual("--openai-worker", command[-2])
        request = json.loads(Path(command[-1]).read_text())
        with patch("builtins.print") as output:
            self.assertEqual(0, runner.openai_worker(command[-1]))
        event = json.loads(output.call_args.args[0])
        self.assertEqual(("dmr", "dmr: hello"), (job["engine"], event["text"]))
        self.assertTrue(any(path.endswith("/engines/v1/chat/completions") for method, path, _ in FakeOpenAIEngine.calls
                            if method == "POST"))
        manager._launch = original_launch
        body["id"], body["engine"] = "bad-engine", "surprise"
        with self.assertRaisesRegex(runner.HttpError, "unknown engine"):
            manager.create(body)

    def test_dmr_does_not_make_the_ollama_agent_available(self):
        manager = self.manager({"engines": {"dmr": {"enabled": True, "base_url": self.base_url}}})
        manager.engine_catalog()
        with patch.object(runner, "find_agents", lambda *_a, **_k: {"claude": None, "codex": None, "ollama": None}):
            report = runner.health_report(manager)
        self.assertIsNone(report["agents"]["ollama"])
        self.assertTrue(report["engines"]["dmr"]["reachable"])

    def test_docker_cli_failure_keeps_the_http_catalog(self):
        manager = self.manager({"engines": {"dmr": {"enabled": True, "base_url": self.base_url}}})

        def broken(*_args, **_kwargs):
            raise OSError("docker exploded")
        for adapter in manager.engine_adapters.values():
            adapter.docker_run = broken
        catalog = manager.engine_catalog()["dmr"]
        self.assertTrue(catalog["reachable"])
        self.assertEqual(["ai/qwen3:latest", "shared-name"], [m["reference"] for m in catalog["models"]])

    def test_job_submit_reuses_a_fresh_catalog_snapshot(self):
        manager = self.manager({"engines": {"dmr": {"enabled": True, "base_url": self.base_url}}})
        manager.engine_catalog()  # refresh once
        FakeOpenAIEngine.calls = []
        manager._launch = lambda *_args: None
        body = {"id": "j1", "agent": "ollama", "engine": "dmr", "model": "ai/qwen3:latest", "prompt": "x",
                "mode": "read-only", "repo": None, "worktree": False, "cwd": None, "gate": None,
                "allowed_tools": [], "resume_session": None}
        manager.create(body)
        self.assertEqual([], [c for c in FakeOpenAIEngine.calls if c[1].endswith("/models")], "no second refresh")

    def test_worker_refuses_a_non_loopback_url(self):
        path = Path(tempfile.mkdtemp()) / "request.json"
        path.write_text(json.dumps({"url": "http://192.168.1.2:12434/engines/v1", "model": "m", "prompt": "secret"}))
        with patch("builtins.print") as output:
            self.assertEqual(1, runner.openai_worker(str(path)))
        self.assertIn("loopback", output.call_args.args[0])
        self.assertEqual([], FakeOpenAIEngine.calls)


class RedirectAndProxyTests(unittest.TestCase):
    """Real sockets, no patching: the opener must not follow a redirect or use a proxy."""

    def test_redirect_is_refused_and_target_never_sees_the_prompt(self):
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer
        seen = []

        class Target(BaseHTTPRequestHandler):
            def do_POST(self):
                seen.append(self.rfile.read(int(self.headers.get("Content-Length", 0))))
                self.send_response(200); self.end_headers(); self.wfile.write(b"{}")

            def log_message(self, *_a):
                pass
        target = HTTPServer(("127.0.0.1", 0), Target)

        class Bouncer(BaseHTTPRequestHandler):
            def do_POST(self):
                self.send_response(307)
                self.send_header("Location", f"http://127.0.0.1:{target.server_address[1]}/steal")
                self.end_headers()

            def log_message(self, *_a):
                pass
        bouncer = HTTPServer(("127.0.0.1", 0), Bouncer)
        for server in (target, bouncer):
            threading.Thread(target=server.serve_forever, daemon=True).start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
        engine = runner.OpenAIEngine(f"http://127.0.0.1:{bouncer.server_address[1]}/v1")
        with self.assertRaisesRegex(Exception, "redirect refused"):
            engine.request("POST", "/chat/completions", {"prompt": "secret"})
        self.assertEqual([], seen)

    def test_proxy_environment_is_ignored(self):
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer

        class Echo(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200); self.end_headers(); self.wfile.write(b'{"data": []}')

            def log_message(self, *_a):
                pass
        server = HTTPServer(("127.0.0.1", 0), Echo)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        # A dead proxy: a client that honoured it would fail instead of reaching the loopback server.
        with patch.dict(os.environ, {"http_proxy": "http://127.0.0.1:9", "HTTP_PROXY": "http://127.0.0.1:9",
                                     "no_proxy": "", "NO_PROXY": ""}):
            engine = runner.OpenAIEngine(f"http://127.0.0.1:{server.server_address[1]}/v1")
            self.assertEqual({"data": []}, engine.request("GET", "/models"))

if __name__ == "__main__":
    unittest.main()
