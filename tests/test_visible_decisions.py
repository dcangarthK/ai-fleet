"""Part G requirements 7 (wiring) and 8 (visible decisions): docs/SPEC-capacity-placement.md 3, 3a, 5.
Run: uv run --with fastapi --with uvicorn --with httpx --with 'mcp<2' python -m unittest -v tests/test_visible_decisions.py
"""
import asyncio
import importlib.util
import inspect
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.test_budget import events_of

import live
import loopheal
import tasks
import tokens
from events import emit
from store import db, init_db

NOW = 1_800_000_000.0
ROOT = Path(__file__).resolve().parents[1]


def load_runner():
    spec = importlib.util.spec_from_file_location("runner_under_test", ROOT / "nodes/runner.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def node(**kw):
    return {"id": "n1", "status": "approved", "heartbeat_at": NOW, "runner": {"metrics": {}}} | kw


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()

    def setUp(self):
        loopheal._ensure()
        loopheal._DOWN_SINCE.clear()
        with db() as c:
            c.execute("DELETE FROM service_heal")


class ServiceWiringTests(Base):
    def test_services_come_from_heartbeats(self):
        stale = node(heartbeat_at=NOW - 3 * tasks.HEARTBEAT_TTL - 1)
        rows = loopheal.collect_services([stale], NOW)
        self.assertEqual([(r["service"], r["missed_beats"]) for r in rows], [("runner", 3)])
        self.assertIsNotNone(loopheal.needs_restart(rows[0], NOW))
        fresh = loopheal.collect_services([node()], NOW)
        self.assertIsNone(loopheal.needs_restart(fresh[0], NOW))

    def test_ollama_and_claude_mem_report_down(self):
        n = node(runner={"metrics": {"ollama": {"up": False}}, "services": {"claude-mem": {"up": False}}})
        first = {r["service"]: r for r in loopheal.collect_services([n], NOW)}
        self.assertEqual(set(first), {"runner", "ollama", "claude-mem"})
        self.assertIsNone(loopheal.needs_restart(first["ollama"], NOW))  # just went down
        later = {r["service"]: r for r in loopheal.collect_services([n], NOW + 130)}
        self.assertIn("failing", loopheal.needs_restart(later["ollama"], NOW + 130))
        self.assertIn("failing", loopheal.needs_restart(later["claude-mem"], NOW + 130))

    def test_services_field_is_cleaned(self):
        out = tasks.clean_health({"ok": True, "services": {"claude-mem": {"up": 1, "x": "y"}, "bad": 3}})
        self.assertEqual(out["services"], {"claude-mem": {"up": True}})

    def test_tick_runs_heal_off_the_event_loop_and_restarts(self):
        stale = node(heartbeat_at=NOW - 200)
        calls = []
        with patch.object(tasks, "node_rows", return_value=[stale]), \
                patch.object(tasks, "in_startup_grace", return_value=False):
            out = asyncio.run(loopheal.tick(NOW, restart=lambda n, s: calls.append((n, s)), health=lambda *a: True))
        self.assertEqual((out[0]["action"], calls), ("restarted", [("n1", "runner")]))

    def test_tick_waits_out_startup_grace(self):
        calls = []
        with patch.object(tasks, "node_rows", return_value=[node(heartbeat_at=NOW - 200)]), \
                patch.object(tasks, "in_startup_grace", return_value=True):
            out = asyncio.run(loopheal.tick(NOW, restart=lambda *a: calls.append(a), health=lambda *a: True))
        self.assertEqual((out[0]["action"], calls), ("grace", []))

    def test_poll_loop_calls_the_tick(self):
        self.assertIn("loopheal.tick()", inspect.getsource(tasks.poll_loop))


class RunnerRestartTests(unittest.TestCase):
    def test_service_manager_commands(self):
        r = load_runner()
        self.assertEqual(r.service_restart_command("runner", "darwin", 501),
                         ["launchctl", "kickstart", "-k", "gui/501/local.ai-fleet.runner"])
        self.assertEqual(r.service_restart_command("ollama", "linux", 1000),
                         ["systemctl", "--user", "restart", "ollama"])
        self.assertEqual(r.service_restart_command("claude-mem", "linux", 1000),
                         ["systemctl", "--user", "restart", "ai-fleet-claude-mem"])

    def test_unknown_service_is_refused_and_route_exists(self):
        r = load_runner()
        with self.assertRaises(r.HttpError):
            r.restart_service("sshd")
        self.assertIn('parts == ["services", "restart"]', (ROOT / "nodes/runner.py").read_text())


class AlertTests(Base):
    def test_service_events_are_alert_types_and_resolve(self):
        self.assertIn("service.alert", live.ALERT_TYPES)
        self.assertIn("service.restart_failed", live.ALERT_TYPES)
        svc = f"svc{time.time_ns()}"
        emit(None, "service.restart_failed", {"node": "nx", "service": svc, "reason": "missed 3 heartbeats"})
        emit(None, "service.alert", {"node": "nx", "service": svc, "reason": "x"})
        mine = [a for a in live.alerts() if a["node"] == "nx"]
        self.assertEqual({a["type"] for a in mine}, {"service.restart_failed", "service.alert"})
        emit(None, "service.restarted", {"node": "nx", "service": svc, "reason": "ok"})
        self.assertEqual([a for a in live.alerts() if a["node"] == "nx"], [])


class VisibleDecisionTests(Base):
    def test_restart_and_bounce_events_carry_a_reason(self):
        loopheal.heal_tick([{"node": "n9", "service": "runner", "missed_beats": 3, "unhealthy_since": None}],
                           lambda *a: None, lambda *a: False, NOW, grace=False)
        loopheal.emit_bounce("t-x", "title", 1)
        for kind in ("service.restart_failed", "gate.bounced"):
            self.assertTrue(events_of(kind)[-1]["data"].get("reason"), kind)

    def test_other_decision_sources_name_a_reason(self):
        root = ROOT / "master/controller"
        for name, needle in (("capacity.py", '"placement.moved"'), ("capacity.py", '"task.requeued"'),
                             ("tasks.py", '"task.loop_cut"'), ("tasks.py", '"task.allocated"')):
            src = (root / name).read_text()
            at = src.index(needle)
            self.assertIn('"reason"', src[at:at + 700], f"{needle} lacks a reason")

    def test_spend_report_counts_decisions(self):
        start = time.time() - 1
        emit("t-y", "placement.moved", {"reason": "spare is free"})
        emit("t-y", "task.requeued", {})
        emit(None, "service.restarted", {"node": "n", "service": "s", "reason": "r"})
        counts = tokens.decision_counts(start)
        self.assertEqual(counts["placement.moved"], {"count": 1, "with_reason": 1})
        self.assertEqual(counts["task.requeued"], {"count": 1, "with_reason": 0})
        self.assertGreaterEqual(counts["service.restarted"]["count"], 1)
        self.assertIn("decisions", tokens.tokens_summary(since=start))


if __name__ == "__main__":
    unittest.main()
