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


class FakeOpenAIEngine:
    calls = []

    @classmethod
    def urlopen(cls, request, timeout=None):
        if isinstance(request, str):
            cls.calls.append(("GET", request, None))
            return FakeResponse({"models": []})
        body = json.loads(request.data) if request.data else None
        cls.calls.append((request.get_method(), request.full_url, body))
        if request.full_url.endswith("/models"):
            return FakeResponse({"data": [{"id": "ai/qwen3:latest"}, {"id": "shared-name"}]})
        if request.full_url.endswith("/chat/completions"):
            return FakeResponse({"choices": [{"message": {"role": "assistant",
                                                            "content": "dmr: " + body["messages"][-1]["content"]}}]})
        raise AssertionError(request.full_url)


class EngineAdapterTests(unittest.TestCase):
    base_url = "http://127.0.0.1:12434/engines/v1"

    def setUp(self):
        FakeOpenAIEngine.calls = []
        mocked = patch.object(runner.urllib.request, "urlopen", FakeOpenAIEngine.urlopen)
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


if __name__ == "__main__":
    unittest.main()
