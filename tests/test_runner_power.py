"""Phase 8 runner additions (docs/SPEC-phase8-operations.md §4, §10): power state and runner digest in /health.
Run: python3 -m unittest -v tests/test_runner_power.py
"""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_runner as base  # noqa: E402


@unittest.skipUnless(base.loopback_bind_available(), "needs loopback sockets")
class RunnerPower(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.fleet = Path(self.temp.name) / "fleet"
        self.fleet.mkdir()
        self.proc = None

    def tearDown(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            self.proc.wait(timeout=5)
        self.temp.cleanup()

    def health(self, config=None, fake_power=None):
        (self.fleet / "runner.json").write_text(json.dumps(config or {}))
        port = base.free_port()
        env = os.environ.copy()
        env.update({"FLEET_DIR": str(self.fleet), "RUNNER_BIND": "127.0.0.1:%d" % port, "RUNNER_TOKEN": "t" * 40,
                    "OLLAMA_URL": "http://127.0.0.1:1"})
        env.pop("FLEET_FAKE_POWER", None)
        if fake_power is not None:
            env["FLEET_FAKE_POWER"] = json.dumps(fake_power)  # test hook
        self.proc = subprocess.Popen([sys.executable, str(base.RUNNER)], env=env, stdin=subprocess.DEVNULL,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        end = time.time() + 10
        while time.time() < end:
            try:
                req = urllib.request.Request("http://127.0.0.1:%d/health" % port, headers={"X-Runner-Token": "t" * 40})
                with urllib.request.urlopen(req, timeout=2) as r:
                    body = json.load(r)
                self.proc.terminate()
                self.proc.wait(timeout=5)
                return body
            except OSError:
                time.sleep(0.05)
        self.fail("runner did not start")

    def test_power_block_from_the_system(self):
        p = self.health()["power"]
        self.assertEqual({"on_battery", "battery_pct", "min_battery_pct", "ok"}, set(p))
        self.assertIn(p["on_battery"], (True, False, None))
        self.assertTrue(p["battery_pct"] is None or 0 <= p["battery_pct"] <= 100)
        self.assertEqual(30, p["min_battery_pct"])

    def test_power_ok_rules(self):
        self.assertFalse(self.health(fake_power={"on_battery": True, "battery_pct": 20})["power"]["ok"])
        self.assertTrue(self.health(fake_power={"on_battery": True, "battery_pct": 80})["power"]["ok"])
        self.assertTrue(self.health(fake_power={"on_battery": False, "battery_pct": 5})["power"]["ok"])
        self.assertTrue(self.health(fake_power={"on_battery": None, "battery_pct": None})["power"]["ok"])
        p = self.health({"power": {"min_battery_pct": 50}}, {"on_battery": True, "battery_pct": 40})["power"]
        self.assertEqual((50, False), (p["min_battery_pct"], p["ok"]))
        p = self.health({"power": {"min_battery_pct": "lots"}}, {"on_battery": True, "battery_pct": 40})["power"]
        self.assertEqual(30, p["min_battery_pct"])  # invalid → default

    def test_digest_is_the_runner_file_hash(self):
        want = hashlib.sha256(Path(base.RUNNER).read_bytes()).hexdigest()
        self.assertEqual(want, self.health()["digest"])


if __name__ == "__main__":
    unittest.main()
