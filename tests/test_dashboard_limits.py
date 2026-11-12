"""Part F requirements 6 and 11: one limit helper for /api/budget/status, /api/fleet, /api/resources, /api/live and
admission; Codex limits from the runner's `rate_limits` record (docs/SPEC-dashboard-truth.md).
Run: uv run --with fastapi --with uvicorn --with httpx --with 'mcp<2' python -m unittest -v tests/test_dashboard_limits.py
"""
import time
import unittest

from tests.test_budget import rules
from tests.test_controller import FakeRunner, W, wait_for
from tests.test_limit_handoff import OPUS, SONNET
from tests.test_live import LiveBase

import budget
import tasks
from store import db

FLEET_ACCOUNT = "claude/owner-max"  # PROFILES: claude "fleet" is labelled owner-max, "spare" is "spare"
SPARE_ACCOUNT = "claude/spare"
CODEX_ACCOUNT = "codex/owner-plus"


def window(used, resets_in, minutes):
    return {"used_percent": used, "resets_at": time.time() + resets_in, "window_minutes": minutes}


class LimitBase(LiveBase):
    def set_rules(self, **changes):
        r = self.c.put("/api/budget/rules", json={"body": rules(**changes)}, headers=W)
        self.assertEqual(r.status_code, 200, r.text)

    def limit_row(self, account, agent, model, resets_in=3600):
        with db() as c:
            c.execute("INSERT INTO agent_usage (ts,task_id,run,agent,model,identity,outcome,resets_at,account) "
                      "VALUES (?,?,?,?,?,?,?,?,?)",
                      (time.time(), f"t-lim{time.time_ns()}", 1, agent, model, budget.identity(agent, model), "limit",
                       time.time() + resets_in, account))

    def runner_reports(self, rate_limits):
        FakeRunner.health_extra = {**FakeRunner.health_extra, "codex_rate_limits": rate_limits}
        self.c.post("/api/refresh", headers=W)
        wait_for(lambda: (next(n for n in tasks.node_rows() if n["id"] == "master")["runner"] or {})
                 .get("codex_rate_limits") == (budget.clean_codex_rate_limits(rate_limits)))

    def tearDown(self):
        FakeRunner.health_extra.pop("codex_rate_limits", None)
        self.c.post("/api/refresh", headers=W)

    # What each surface says about (account, model key); None when it isn't limited.
    def status_until(self, key):
        entry = next(m for m in self.c.get("/api/budget/status", headers=W).json()["models"] if m["key"] == key)
        return entry["limited_until"], entry["limit_scope"]

    def fleet_until(self, key):
        entry = next(m for m in self.c.get("/api/fleet", headers=W).json()["budget"]["models"] if m["key"] == key)
        return entry["limited_until"], entry["limit_scope"]

    def resource_until(self, account, key):
        rows = [r for r in self.c.get("/api/resources", headers=W).json() if r["account"] == account and r["model"] == key]
        self.assertTrue(rows, (account, key))
        return rows[0]["until"] if rows[0]["state"] == "limited" else None

    def live_until(self, account):
        return next(a for a in self.live()["accounts"] if a["account"] == account)["limited_until"]

    def admission(self, account, agent, model, kind):
        decision = budget.admit(agent=agent, model=model, kind=kind, effort=None, override=False,
                                accounts_for=lambda _agent: [account], reroute=False)
        return decision["wait"]


class OneHelperTests(LimitBase):
    SONNET_KEY = "claude:claude-sonnet-*"
    OPUS_KEY = "claude:claude-opus-*"

    def test_the_helper_honours_limit_scope(self):
        now = time.time()
        rows = [{"agent": "claude", "model": "claude-opus-5-5", "failure_reason": "limit", "resets_at": now + 600,
                 "ended_at": now - 5, "state": "failed"}]
        for scope, sonnet_limited in (("model", False), ("account", True)):
            state = budget.limit_state(rows, now, scope)
            r = rules(limit_scope=scope)
            self.assertEqual(budget.limit_for(r, state, "claude", None, self.OPUS_KEY)[0], now + 600, scope)
            until, kind = budget.limit_for(r, state, "claude", None, self.SONNET_KEY)
            self.assertEqual(until is not None, sonnet_limited, scope)
            self.assertEqual(kind, "account" if sonnet_limited else None, scope)

    def test_account_scope_limits_every_key_identically_everywhere_and_admission_refuses(self):
        self.set_rules(limit_scope="account")
        self.limit_row(FLEET_ACCOUNT, "claude", OPUS)
        for key in (self.OPUS_KEY, self.SONNET_KEY):
            seen = {self.status_until(key), self.fleet_until(key)}
            self.assertEqual(len(seen), 1, seen)
            until, scope = seen.pop()
            self.assertIsNotNone(until, key)
            self.assertEqual(scope, "account")
            self.assertAlmostEqual(self.resource_until(FLEET_ACCOUNT, key), until, delta=1)
        self.assertAlmostEqual(self.live_until(FLEET_ACCOUNT), self.status_until(self.SONNET_KEY)[0], delta=1)
        self.assertEqual(self.admission(FLEET_ACCOUNT, "claude", SONNET, "draft"), "limit")
        # the other account is not limited, on the slot list or at admission
        self.assertIsNone(self.resource_until(SPARE_ACCOUNT, self.SONNET_KEY))
        self.assertIsNone(self.admission(SPARE_ACCOUNT, "claude", SONNET, "draft"))

    def test_model_scope_limits_only_that_key_everywhere(self):
        self.set_rules(limit_scope="model")
        self.limit_row(FLEET_ACCOUNT, "claude", OPUS)
        until, scope = self.status_until(self.OPUS_KEY)
        self.assertIsNotNone(until)
        self.assertEqual(scope, "model")
        self.assertEqual(self.fleet_until(self.OPUS_KEY), (until, "model"))
        self.assertAlmostEqual(self.resource_until(FLEET_ACCOUNT, self.OPUS_KEY), until, delta=1)
        self.assertEqual(self.admission(FLEET_ACCOUNT, "claude", OPUS, "build"), "limit")
        self.assertEqual(self.status_until(self.SONNET_KEY), (None, None))
        self.assertEqual(self.fleet_until(self.SONNET_KEY), (None, None))
        self.assertIsNone(self.resource_until(FLEET_ACCOUNT, self.SONNET_KEY))
        self.assertIsNone(self.admission(FLEET_ACCOUNT, "claude", SONNET, "draft"))

    def test_an_owner_reset_clears_it_everywhere(self):
        self.set_rules(limit_scope="account")
        self.limit_row(FLEET_ACCOUNT, "claude", OPUS)
        with db() as c:
            c.execute("INSERT INTO limit_resets (account, at, actor) VALUES (?,?,?)", (FLEET_ACCOUNT, time.time() + 1, "t"))
        self.assertEqual(self.status_until(self.SONNET_KEY), (None, None))
        self.assertEqual(self.fleet_until(self.SONNET_KEY), (None, None))
        self.assertIsNone(self.resource_until(FLEET_ACCOUNT, self.SONNET_KEY))
        self.assertIsNone(self.live_until(FLEET_ACCOUNT))
        self.assertIsNone(self.admission(FLEET_ACCOUNT, "claude", SONNET, "draft"))


class CodexRateLimitTests(LimitBase):
    def test_an_exhausted_window_limits_codex_until_its_reset_everywhere(self):
        weekly = window(100.0, 7200, 10080)
        self.runner_reports({"primary": window(40.0, 1800, 300), "secondary": weekly, "seen_at": time.time()})
        until, scope = self.status_until("codex")
        self.assertAlmostEqual(until, weekly["resets_at"], delta=1)
        self.assertEqual(self.fleet_until("codex"), (until, scope))
        self.assertAlmostEqual(self.resource_until(CODEX_ACCOUNT, "codex"), until, delta=1)
        self.assertAlmostEqual(self.live_until(CODEX_ACCOUNT), until, delta=1)
        self.assertEqual(self.admission(CODEX_ACCOUNT, "codex", None, "build"), "limit")

    def test_the_budget_page_shows_both_windows(self):
        self.runner_reports({"primary": window(3.0, 1800, 300), "secondary": window(56.0, 400000, 10080),
                             "seen_at": time.time()})
        entry = next(m for m in self.c.get("/api/budget/status", headers=W).json()["models"] if m["key"] == "codex")
        self.assertEqual(entry["state"], "ok")
        self.assertEqual(entry["rate_limits"]["primary"]["used_percent"], 3.0)
        self.assertEqual(entry["rate_limits"]["secondary"]["used_percent"], 56.0)

    def test_an_error_limit_ends_at_the_next_fresh_record_with_room_left(self):
        self.limit_row(CODEX_ACCOUNT, "codex", None, resets_in=3600)
        self.assertIsNotNone(self.status_until("codex")[0])  # the error text alone limits it
        self.runner_reports({"primary": window(3.0, 1800, 300), "secondary": window(56.0, 400000, 10080),
                             "seen_at": time.time() + 1})
        self.assertEqual(self.status_until("codex"), (None, None))
        self.assertIsNone(self.resource_until(CODEX_ACCOUNT, "codex"))
        self.assertIsNone(self.admission(CODEX_ACCOUNT, "codex", None, "build"))

    def test_a_record_older_than_the_error_does_not_clear_it(self):
        self.limit_row(CODEX_ACCOUNT, "codex", None, resets_in=3600)
        self.runner_reports({"primary": window(3.0, 1800, 300), "secondary": window(56.0, 400000, 10080),
                             "seen_at": time.time() - 600})
        self.assertIsNotNone(self.status_until("codex")[0])

    def test_a_passed_reset_in_the_record_is_no_limit(self):
        self.runner_reports({"primary": {"used_percent": 100.0, "resets_at": time.time() - 60, "window_minutes": 300},
                             "secondary": window(10.0, 400000, 10080), "seen_at": time.time()})
        self.assertEqual(self.status_until("codex"), (None, None))

    def test_a_malformed_report_is_dropped(self):
        self.assertIsNone(budget.clean_codex_rate_limits("lots"))
        self.assertIsNone(budget.clean_codex_rate_limits({"primary": {"used_percent": "x"}, "secondary": None}))
        got = budget.clean_codex_rate_limits({"primary": {"used_percent": 3, "resets_at": 5, "window_minutes": 300,
                                                          "extra": "x"}, "secondary": None, "seen_at": 9, "junk": 1})
        self.assertEqual(got, {"primary": {"used_percent": 3.0, "resets_at": 5.0, "window_minutes": 300.0},
                               "secondary": None, "seen_at": 9.0})


if __name__ == "__main__":
    unittest.main()
