"""Networking phase N2, controller side: engine state and evals come through the runner (no 11434/11435 reach), the
/health firewall field is cleaned, Live shows the firewall badge data and separates engine failures from
runner/network/firewall failures, and "check now" only asks the runner for a read-only refresh. Fakes only. Run:
uv run --with fastapi --with uvicorn --with httpx --with 'mcp<2' python -m unittest -v tests/test_networking_n2.py
"""
import asyncio
import json
import time
import unittest
from unittest import mock

from tests.test_controller import FakeRunner, W

from fastapi.testclient import TestClient

import controller
import evaluations
import events
import live
import tasks
from store import db, init_db

GOOD = {"checked_at": 1000.0, "age_s": 12.0, "status": "pass", "backend": "macos-pf", "runner_tailnet_only": True,
        "engine_loopback_only": True, "ipv6_wildcard": False, "details": ["pf: enabled"]}


def node(**over):
    base = {"id": "n1", "name": "laptop2", "ip": "100.1.1.2", "engine": "ollama", "status": "approved",
            "runner_fails": 0, "heartbeat_at": None, "polled_at": 1000.0, "runner_age_s": 1.0,
            "runner": {"metrics": {"ollama": {"up": True, "loaded": []}}, "firewall": GOOD}}
    base.update(over)
    return base


class NoRawEngineProbe(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()

    def test_controller_has_no_engine_probe_or_inventory_fetch(self):
        for name in ("PROBES", "INFO_PORT", "probe", "fetch_inventory"):
            self.assertFalse(hasattr(controller, name), name)

    def test_engine_state_comes_from_the_runner_health(self):
        up = {"metrics": {"ollama": {"up": True}}}
        self.assertEqual(controller.real_probe_from_runner(up), ("ollama", 11434, []))
        for health in (None, {}, {"metrics": {}}, {"metrics": {"ollama": {"up": False}}},
                       {"metrics": {"ollama": {"up": "yes"}}}):
            self.assertIsNone(controller.real_probe_from_runner(health))

    def test_model_names_still_merge_from_the_models_report(self):
        found = controller.real_probe_from_runner({"metrics": {"ollama": {"up": True}}})
        merged = controller.merge_probe_models(found, {"models": [{"name": "b:1"}, {"name": "a:1"}]})
        self.assertEqual(merged, ("ollama", 11434, ["a:1", "b:1"]))

    def test_no_http_call_to_an_engine_or_inventory_port_when_refreshing(self):
        urls = []

        class Spy:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return None

            async def get(self, url, **k):
                urls.append(url)
                raise controller.httpx.ConnectError("no network in tests")
        with mock.patch.object(controller.httpx, "AsyncClient", Spy), \
                mock.patch.object(controller, "tailnet_nodes", return_value=[
                    {"id": "9", "name": "w", "ip": "100.9.9.9", "os": "linux", "online": True}]):
            asyncio.run(controller.refresh_nodes())
        self.assertTrue(urls)
        for url in urls:
            for port in ("11434", "11435", "8000", "1234", "8080"):
                self.assertNotIn(":" + port, url)


class HealthInventory(unittest.TestCase):
    def test_hardware_from_health_is_merged_and_bad_values_ignored(self):
        with db() as c:
            c.execute("INSERT OR REPLACE INTO nodes (id, name, ip, status, inventory) VALUES "
                      "('inv1','inv1','100.1.1.9','approved','{\"hardware\": {\"cpu\": \"M2\"}}')")
            controller.merge_health_inventory(c, "inv1", {"inventory": {"hardware": {"ram_gb": 32, "cores": "x"}}})
            controller.merge_health_inventory(c, "inv1", None)
            stored = json.loads(c.execute("SELECT inventory FROM nodes WHERE id='inv1'").fetchone()["inventory"])
        self.assertEqual(stored["hardware"], {"cpu": "M2", "ram_gb": 32})


class CleanFirewall(unittest.TestCase):
    def test_heartbeat_health_keeps_a_cleaned_firewall(self):
        out = tasks.clean_health({"ok": True, "firewall": dict(GOOD, status="pass"), "inventory": {
            "hardware": {"ram_gb": 16, "cores": 8, "secret": "x"}}})
        self.assertEqual(out["firewall"]["status"], "pass")
        self.assertEqual(out["inventory"], {"hardware": {"ram_gb": 16.0, "cores": 8.0}})

    def test_bad_values_become_unknown_or_null(self):
        out = tasks.clean_firewall({"status": "great", "backend": "iptables", "age_s": -5, "checked_at": "now",
                                    "runner_tailnet_only": "yes", "ipv6_wildcard": 1, "details": ["ok", 5, "x" * 500]})
        self.assertEqual((out["status"], out["backend"], out["age_s"], out["checked_at"]), ("unknown", "unknown", None, None))
        self.assertEqual((out["runner_tailnet_only"], out["engine_loopback_only"], out["ipv6_wildcard"]),
                         (None, None, None))
        self.assertEqual(out["details"], ["ok", "x" * 160])
        self.assertEqual(tasks.clean_firewall("junk")["status"], "unknown")

    def test_details_are_capped(self):
        self.assertEqual(len(tasks.clean_firewall({"details": ["d"] * 50})["details"]), 12)


class LiveMachine(unittest.TestCase):
    NOW = 1010.0

    def row(self, **over):
        return live.machine_row(node(**over), self.NOW)

    def test_badge_data_with_age_on_the_controllers_clock(self):
        fw = self.row()["firewall"]
        self.assertEqual((fw["status"], fw["backend"]), ("pass", "macos-pf"))
        self.assertEqual(fw["age_s"], 22)  # 10 s since the poll + the runner's own 12 s
        self.assertEqual(fw["details"], ["pf: enabled"])

    def test_missing_posture_is_unknown_never_a_guess(self):
        runner = {"metrics": {"ollama": {"up": True}}}
        fw = self.row(runner=runner)["firewall"]
        self.assertEqual((fw["status"], fw["age_s"]), ("unknown", None))

    def test_stale_observation_reads_unknown(self):
        row = live.machine_row(node(), self.NOW + live.FIREWALL_STALE + 100)
        self.assertEqual(row["firewall"]["status"], "unknown")

    def test_healthy_node_has_no_failure(self):
        row = self.row()
        self.assertEqual((row["failures"], row["failure"]), ([], None))

    def test_engine_down_is_an_engine_failure_not_a_network_one(self):
        runner = {"metrics": {"ollama": {"up": False, "loaded": []}}, "firewall": GOOD}
        row = self.row(runner=runner)
        self.assertEqual((row["failures"], row["failure"], row["offline"]), (["engine"], "engine", None))

    def test_node_without_an_engine_is_not_an_engine_failure(self):
        runner = {"metrics": {"ollama": {"up": False}}, "firewall": GOOD}
        self.assertEqual(self.row(runner=runner, engine=None)["failures"], [])

    def test_a_reachable_dmr_engine_means_no_engine_failure(self):
        runner = {"metrics": {"ollama": {"up": False}}, "firewall": GOOD, "engines": {"dmr": {"reachable": True}}}
        self.assertEqual(self.row(runner=runner)["failures"], [])

    def test_firewall_fail_is_a_firewall_failure(self):
        runner = {"metrics": {"ollama": {"up": True}}, "firewall": dict(GOOD, status="fail")}
        row = self.row(runner=runner)
        self.assertEqual((row["failures"], row["failure"]), (["firewall"], "firewall"))

    def test_unreachable_runner_is_a_runner_network_failure_and_hides_the_engine(self):
        runner = {"metrics": {"ollama": {"up": False}}, "firewall": GOOD}
        row = self.row(runner=runner, runner_fails=3)
        self.assertEqual((row["failures"], row["failure"]), (["runner_network"], "runner_network"))
        self.assertTrue(row["offline"])

    def test_firewall_and_engine_failures_are_both_listed_firewall_first(self):
        runner = {"metrics": {"ollama": {"up": False}}, "firewall": dict(GOOD, status="fail")}
        self.assertEqual(self.row(runner=runner)["failures"], ["firewall", "engine"])


class CheckNow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ctx = TestClient(controller.app, base_url="http://127.0.0.1")
        cls.c = cls.ctx.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.ctx.__exit__(None, None, None)

    def setUp(self):
        FakeRunner.calls.clear()
        FakeRunner.posture = dict(GOOD, status="warn", details=["no host firewall enabled"])

    def test_check_now_asks_for_a_read_only_refresh_and_returns_the_cleaned_object(self):
        deadline = time.time() + 5
        while time.time() < deadline and not tasks.node_rows():
            time.sleep(0.05)
        r = self.c.post("/api/nodes/master/posture/check", headers=W)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["firewall"]["status"], "warn")
        self.assertEqual(FakeRunner.calls, [("/posture/check", {})])
        with db() as c:
            stored = json.loads(c.execute("SELECT runner FROM nodes WHERE id='master'").fetchone()["runner"] or "{}")
        self.assertEqual(stored.get("firewall", {}).get("status"), "warn")

    def test_no_endpoint_applies_a_change(self):
        paths = {route.path for route in controller.app.routes if hasattr(route, "path")}
        for path in paths:
            for word in ("firewall", "pf", "remediate", "apply"):
                self.assertNotIn("/" + word, path)

    def test_unknown_node_is_404(self):
        self.assertEqual(self.c.post("/api/nodes/nope/posture/check", headers=W).status_code, 404)

    def test_runner_unreachable_is_an_error_not_a_pass(self):
        with mock.patch.object(tasks, "runner_url", return_value="http://127.0.0.1:1"):
            r = self.c.post("/api/nodes/master/posture/check", headers=W)
        self.assertEqual(r.status_code, 502)


class EvalsThroughRunner(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()

    def test_eval_command_targets_the_runner_not_an_engine_port(self):
        seen = {}

        async def fake_exec(*args, **kwargs):
            seen["args"], seen["env"] = list(args), kwargs.get("env") or {}
            raise OSError("not run in tests")
        run = {"id": "r-n2", "node": "master", "model": "m:1", "roles": ["fast"], "started_at": 0.0}
        with mock.patch.object(evaluations.asyncio, "create_subprocess_exec", fake_exec), \
                mock.patch.object(events, "emit"), \
                mock.patch.dict("os.environ", {"FLEET_EVAL_CMD": "python3 fake_eval.py"}):
            asyncio.run(evaluations._run(run))
        args = seen["args"]
        self.assertIn("--runner", args)
        self.assertEqual(args[args.index("--runner") + 1], tasks.LOCAL_RUNNER)
        self.assertNotIn("--ollama", args)
        self.assertFalse(any("11434" in a for a in args))

    def test_runner_token_goes_in_the_environment_not_the_command_line(self):
        seen = {}

        async def fake_exec(*args, **kwargs):
            seen["args"], seen["env"] = list(args), kwargs.get("env") or {}
            raise OSError("not run in tests")
        run = {"id": "r-n2b", "node": "master", "model": "m:1", "roles": ["fast"], "started_at": 0.0}
        with mock.patch.object(evaluations.asyncio, "create_subprocess_exec", fake_exec), \
                mock.patch.object(events, "emit"), \
                mock.patch.object(tasks, "node_runner_token", return_value="tok-n2-secret"):
            asyncio.run(evaluations._run(run))
        self.assertEqual(seen["env"].get("FLEET_EVAL_RUNNER_TOKEN"), "tok-n2-secret")
        self.assertFalse(any("tok-n2-secret" in a for a in seen["args"]))


if __name__ == "__main__":
    unittest.main()
