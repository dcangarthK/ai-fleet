"""Runner side of Part D (docs/SPEC-live-monitor.md 4.1, 12.1): background metrics served from a cache, and signed
heartbeats. Run: python3 -m unittest -v tests/test_runner_live.py
"""
import hashlib
import hmac
import importlib.util
import json
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))
spec = importlib.util.spec_from_file_location("fleet_runner_live", ROOT / "nodes" / "runner.py")
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)

VM_STAT = """Mach Virtual Memory Statistics: (page size of 16384 bytes)
Pages free:                               10000.
Pages active:                            500000.
Pages inactive:                          400000.
Pages wired down:                        200000.
Pages occupied by compressor:            100000.
"""


class Parsing(unittest.TestCase):
    def test_vm_stat_uses_the_header_page_size(self):
        self.assertAlmostEqual(runner.parse_vm_stat(VM_STAT), 800000 * 16384 / 1e9)
        self.assertIsNone(runner.parse_vm_stat("Pages active: 5."))  # no header: no guess
        self.assertIsNone(runner.parse_vm_stat(None))

    def test_swapusage(self):
        text = "total = 2048.00M  used = 1024.00M  free = 1024.00M  (encrypted)"
        self.assertAlmostEqual(runner.parse_swapusage(text), 1024 * 1024 * 1024 / 1e9)
        self.assertIsNone(runner.parse_swapusage("garbage"))

    def test_meminfo(self):
        total, used, swap = runner.parse_meminfo("MemTotal: 1000000 kB\nMemAvailable: 400000 kB\n"
                                                 "SwapTotal: 200000 kB\nSwapFree: 50000 kB\n")
        self.assertAlmostEqual(total, 1000000 * 1024 / 1e9)
        self.assertAlmostEqual(used, 600000 * 1024 / 1e9)
        self.assertAlmostEqual(swap, 150000 * 1024 / 1e9)


class Collection(unittest.TestCase):
    def fake_run(self, outputs):
        def run(argv, timeout=3):
            return outputs.get(argv[-1])
        return run

    def test_macos_collection_and_nulls(self):
        with tempfile.TemporaryDirectory() as d:
            out = runner.collect_metrics(d, "http://127.0.0.1:1", platform="darwin",
                                         run=self.fake_run({"hw.memsize": "48000000000\n", "vm_stat": VM_STAT,
                                                            "vm.swapusage": "used = 512.00M"}),
                                         ollama=lambda url: {"up": True, "loaded": ["qwen3:8b"]})
            self.assertEqual(out["mem_total_gb"], 48.0)
            self.assertIsNotNone(out["mem_used_gb"])
            self.assertIsNotNone(out["disk_free_gb"])
            self.assertEqual(out["ollama"], {"up": True, "loaded": ["qwen3:8b"]})
            failing = runner.collect_metrics(d, "http://127.0.0.1:1", platform="darwin", run=lambda argv, timeout=3: None,
                                             ollama=lambda url: {"up": False, "loaded": []})
            self.assertEqual((failing["mem_total_gb"], failing["mem_used_gb"], failing["swap_used_gb"]), (None, None, None))
            windows = runner.collect_metrics(d, "http://127.0.0.1:1", platform="win32", run=lambda argv, timeout=3: "x",
                                             ollama=lambda url: {"up": False, "loaded": []})
            self.assertEqual((windows["mem_total_gb"], windows["swap_used_gb"]), (None, None))

    def test_a_stalled_command_is_killed_at_its_timeout(self):
        start = time.monotonic()
        out = runner.run_capped([sys.executable, "-c", "import time; time.sleep(30)"], timeout=0.5)
        self.assertIsNone(out)
        self.assertLess(time.monotonic() - start, 5)

    def test_health_serves_the_cache_while_a_collection_is_stalled(self):
        release = threading.Event()
        calls = []

        def slow():
            calls.append(1)
            if len(calls) > 1:
                release.wait(10)  # a swapping machine: vm_stat hangs
            return {"mem_total_gb": 16.0, "ollama": {"up": True, "loaded": []}}

        collector = runner.MetricsCollector("/tmp", "http://127.0.0.1:1", collect=slow, interval=0.01)
        collector.once()
        collector.start()
        time.sleep(0.1)  # the thread is now stuck in the second collection
        start = time.monotonic()
        snap = collector.snapshot()
        self.assertLess(time.monotonic() - start, 0.05)
        self.assertEqual(snap["mem_total_gb"], 16.0)
        self.assertGreaterEqual(snap["age_s"], 0)
        release.set()
        self.assertIsNone(runner.MetricsCollector("/tmp", "x", collect=slow).snapshot())  # never collected: no data


class Controller(unittest.TestCase):
    def test_allowed_controller_urls(self):
        ok = ["https://master.tail.ts.net:8701", "http://127.0.0.1:8700", "http://localhost:8700",
              "http://100.101.102.103:8700", "http://master.tail1234.ts.net:8700"]
        bad = ["http://example.com:8700", "http://192.168.1.5:8700", "ftp://127.0.0.1", "http://u:p@127.0.0.1:8700",
               "not a url", ""]
        for url in ok:
            self.assertTrue(runner.allowed_controller(url), url)
        for url in bad:
            self.assertFalse(runner.allowed_controller(url), url)


class FakeManager:
    controller_url = "http://127.0.0.1:8700"
    node_id = "master"


class Heartbeats(unittest.TestCase):
    def test_signed_push_never_sends_the_token(self):
        sent = []
        beat = runner.Heartbeat(FakeManager(), "secret-token", lambda: {"ok": True, "metrics": {"age_s": 1}},
                                post=lambda url, body, headers: sent.append((url, body, headers)) or 200)
        self.assertTrue(beat.beat(now=1000))
        url, body, headers = sent[0]
        self.assertEqual(url, "http://127.0.0.1:8700/api/nodes/heartbeat")
        self.assertNotIn("secret-token", json.dumps(headers) + body.decode())
        expected = hmac.new(b"secret-token", f"master\n1000\n{hashlib.sha256(body).hexdigest()}".encode(),
                            hashlib.sha256).hexdigest()
        self.assertEqual(headers["X-Fleet-Sig"], expected)
        self.assertEqual((headers["X-Fleet-Node"], headers["X-Fleet-Ts"]), ("master", "1000"))

    def test_nothing_without_a_controller_or_an_id_and_errors_are_survived(self):
        sent = []
        for manager in (type("M", (), {"controller_url": None, "node_id": "master"})(),
                        type("M", (), {"controller_url": "http://127.0.0.1:1", "node_id": None})()):
            self.assertFalse(runner.Heartbeat(manager, "t", dict, post=lambda *a: sent.append(a) or 200).beat())
        self.assertEqual(sent, [])

        def boom(*a):
            raise OSError("controller down")

        self.assertFalse(runner.Heartbeat(FakeManager(), "t", dict, post=boom).beat())

    def test_jitter_stays_within_bounds(self):
        beat = runner.Heartbeat(FakeManager(), "t", dict)
        delays = [beat.next_delay() for _ in range(200)]
        self.assertTrue(all(8.0 <= d <= 12.0 for d in delays))
        self.assertGreater(len(set(delays)), 10)


class RunnerProcess(unittest.TestCase):
    """The node id is learned only from a token-checked request."""
    def test_node_id_only_from_an_authorised_request(self):
        import test_runner as base  # noqa: E402  (same helpers as the other runner process tests)
        if not base.loopback_bind_available():
            self.skipTest("needs loopback sockets")
        with tempfile.TemporaryDirectory() as d:
            fleet = Path(d)
            manager = runner.JobManager(fleet, "http://127.0.0.1:1")
            server = runner.RunnerServer(("127.0.0.1", 0), runner.Handler)
            server.manager, server.runner_token = manager, "t" * 40
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                port = server.server_address[1]
                bad = urllib.request.Request(f"http://127.0.0.1:{port}/health", headers={"X-Fleet-Node-Id": "evil"})
                with self.assertRaises(urllib.error.HTTPError):
                    urllib.request.urlopen(bad, timeout=5)
                self.assertIsNone(manager.node_id)
                good = urllib.request.Request(f"http://127.0.0.1:{port}/health",
                                              headers={"X-Fleet-Node-Id": "master", "X-Runner-Token": "t" * 40})
                with urllib.request.urlopen(good, timeout=10) as r:
                    body = json.load(r)
                self.assertEqual(manager.node_id, "master")
                self.assertIn("heartbeat", body)
            finally:
                server.shutdown()
                server.server_close()


if __name__ == "__main__":
    unittest.main()
