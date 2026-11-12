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


def record(primary, secondary, seen_at=None, profile="fleet"):
    return {"profile": profile, "primary": primary, "secondary": secondary, "seen_at": seen_at or time.time()}


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

    def runner_reports(self, *records):
        rate_limits = list(records)
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

    FABLE_KEY = "claude:claude-fable-*"

    def test_default_scope_makes_a_claude_limit_account_wide_everywhere(self):
        """Opus review HIGH 2: one fleet Sonnet limit shows Opus and Fable limited on all four endpoints."""
        self.set_rules()  # the default limit_scope
        self.assertEqual(budget.DEFAULT_RULES["limit_scope"], "auto")
        self.limit_row(FLEET_ACCOUNT, "claude", SONNET)
        for key in (self.SONNET_KEY, self.OPUS_KEY, self.FABLE_KEY):
            seen = {self.status_until(key), self.fleet_until(key)}
            self.assertEqual(len(seen), 1, (key, seen))
            until, scope = seen.pop()
            self.assertIsNotNone(until, key)
            self.assertEqual(scope, "account", key)
            self.assertAlmostEqual(self.resource_until(FLEET_ACCOUNT, key), until, delta=1)
        self.assertIsNotNone(self.live_until(FLEET_ACCOUNT))
        self.assertEqual(self.admission(FLEET_ACCOUNT, "claude", OPUS, "build"), "limit")
        self.assertEqual(self.admission(FLEET_ACCOUNT, "claude", "claude-fable-5-2", "review"), "limit")
        self.assertIsNone(self.admission(SPARE_ACCOUNT, "claude", OPUS, "build"))

    def test_auto_scope_leaves_codex_per_model(self):
        now = time.time()
        rows = [{"agent": "codex", "model": "gpt-5.6-terra", "failure_reason": "limit", "resets_at": now + 600,
                 "ended_at": now - 5, "state": "failed"}]
        self.assertEqual(budget.limit_state(rows, now, "auto")["accounts"], {})

    def test_auto_scope_keys_a_codex_record_to_the_model_it_ran(self):
        """Codex final review HIGH: an exhausted rate_limits record limits only the model the account last ran."""
        entry = budget.DEFAULT_RULES["models"]["codex"]  # one rules key per model: models of one key share a limit
        self.set_rules(**{"models__codex:gpt-5.6-terra": dict(entry), "models__codex:gpt-5.6-sol": dict(entry)})
        now = time.time()
        with db() as c:
            for ts, model in ((now - 300, "gpt-5.6-sol"), (now - 60, "gpt-5.6-terra")):
                c.execute("INSERT INTO agent_usage (ts,task_id,run,agent,model,identity,outcome,account) "
                          "VALUES (?,?,?,?,?,?,?,?)", (ts, f"t-ok{time.time_ns()}", 1, "codex", model,
                                                       budget.identity("codex", model), "ok", CODEX_ACCOUNT))
        self.runner_reports(record(window(100.0, 1800, 300), window(10.0, 400000, 10080), now))
        state = budget.limit_snapshot(budget.current_rules(), now, CODEX_ACCOUNT)
        self.assertNotIn("codex", state["accounts"])
        self.assertIn("codex:gpt-5.6-terra", state["models"])
        self.assertNotIn("codex:gpt-5.6-sol", state["models"])
        self.assertEqual(self.admission(CODEX_ACCOUNT, "codex", "gpt-5.6-terra", "final_review"), "limit")
        self.assertIsNone(self.admission(CODEX_ACCOUNT, "codex", "gpt-5.6-sol", "final_review"))

    def test_a_stored_model_scope_is_migrated_once_to_auto(self):
        import json
        with db() as c:
            version = c.execute("SELECT COALESCE(MAX(version),0) FROM budget_rules").fetchone()[0] + 1
            c.execute("DELETE FROM budget_rules WHERE author='migration:limit_scope'")
            c.execute("INSERT INTO budget_rules (version, body, author, created_at) VALUES (?,?,?,?)",
                      (version, json.dumps(rules(limit_scope="model")), "owner", time.time()))
        budget.seed_rules()
        self.assertEqual(budget.current_rules()["limit_scope"], "auto")
        history = [h for h in budget.rules_history() if h["author"] == "migration:limit_scope"]
        self.assertEqual(len(history), 1)
        budget.seed_rules()  # runs once: an owner's later explicit "model" is kept
        self.set_rules(limit_scope="model")
        budget.seed_rules()
        self.assertEqual(budget.current_rules()["limit_scope"], "model")

    def test_the_marker_is_written_even_when_nothing_needed_migrating(self):
        with db() as c:
            c.execute("DELETE FROM budget_rules WHERE author='migration:limit_scope'")
        self.set_rules(limit_scope="auto")
        budget.seed_rules()  # first run of the check: body already "auto", marker still written
        self.assertEqual(len([h for h in budget.rules_history() if h["author"] == "migration:limit_scope"]), 1)
        self.set_rules(limit_scope="model")  # the owner's later choice
        budget.seed_rules()  # a restart
        self.assertEqual(budget.current_rules()["limit_scope"], "model")

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
        self.runner_reports(record(window(40.0, 1800, 300), weekly))
        until, scope = self.status_until("codex")
        self.assertAlmostEqual(until, weekly["resets_at"], delta=1)
        self.assertEqual(self.fleet_until("codex"), (until, scope))
        self.assertAlmostEqual(self.resource_until(CODEX_ACCOUNT, "codex"), until, delta=1)
        self.assertAlmostEqual(self.live_until(CODEX_ACCOUNT), until, delta=1)
        self.assertEqual(self.admission(CODEX_ACCOUNT, "codex", None, "build"), "limit")

    def test_the_budget_page_shows_both_windows(self):
        self.runner_reports(record(window(3.0, 1800, 300), window(56.0, 400000, 10080)))
        entry = next(m for m in self.c.get("/api/budget/status", headers=W).json()["models"] if m["key"] == "codex")
        self.assertEqual(entry["state"], "ok")
        row = next(a for a in entry["accounts"] if a["account"] == CODEX_ACCOUNT)
        self.assertEqual(row["rate_limits"]["primary"]["used_percent"], 3.0)
        self.assertEqual(row["rate_limits"]["secondary"]["used_percent"], 56.0)

    def test_an_error_limit_ends_at_the_next_fresh_record_with_room_left(self):
        self.limit_row(CODEX_ACCOUNT, "codex", None, resets_in=3600)
        self.assertIsNotNone(self.status_until("codex")[0])  # the error text alone limits it
        self.runner_reports(record(window(3.0, 1800, 300), window(56.0, 400000, 10080), time.time() + 1))
        self.assertEqual(self.status_until("codex"), (None, None))
        self.assertIsNone(self.resource_until(CODEX_ACCOUNT, "codex"))
        self.assertIsNone(self.admission(CODEX_ACCOUNT, "codex", None, "build"))

    def test_a_record_older_than_the_error_does_not_clear_it(self):
        self.limit_row(CODEX_ACCOUNT, "codex", None, resets_in=3600)
        self.runner_reports(record(window(3.0, 1800, 300), window(56.0, 400000, 10080), time.time() - 600))
        self.assertIsNotNone(self.status_until("codex")[0])

    def test_a_passed_reset_in_the_record_is_no_limit(self):
        self.runner_reports(record({"used_percent": 100.0, "resets_at": time.time() - 60, "window_minutes": 300},
                                   window(10.0, 400000, 10080)))
        self.assertEqual(self.status_until("codex"), (None, None))

    def test_a_record_limits_only_its_own_account(self):
        other = record(window(100.0, 7200, 300), window(5.0, 400000, 10080), profile="other")  # codex@master/other
        self.runner_reports(other)
        self.assertEqual(self.status_until("codex")[0] is not None, True)  # aggregate: any account limited
        entry = next(m for m in self.c.get("/api/budget/status", headers=W).json()["models"] if m["key"] == "codex")
        self.assertAlmostEqual(entry["any_account_limited_until"], entry["limited_until"], delta=1)
        by_account = {a["account"]: a for a in entry["accounts"]}
        self.assertEqual(by_account[CODEX_ACCOUNT]["state"], "ok")
        self.assertIsNone(self.resource_until(CODEX_ACCOUNT, "codex"))
        self.assertIsNone(self.live_until(CODEX_ACCOUNT))
        self.assertIsNone(self.admission(CODEX_ACCOUNT, "codex", None, "build"))
        self.assertEqual(self.admission("codex@master/other", "codex", None, "build"), "limit")

    def test_a_fresh_record_for_one_account_keeps_another_accounts_error_limit(self):
        self.limit_row(CODEX_ACCOUNT, "codex", None, resets_in=3600)
        self.runner_reports(record(window(3.0, 1800, 300), window(5.0, 400000, 10080), time.time() + 1, profile="other"))
        self.assertIsNotNone(self.resource_until(CODEX_ACCOUNT, "codex"))

    def test_status_gives_a_row_per_account_and_key(self):
        self.set_rules(limit_scope="model")
        self.limit_row(FLEET_ACCOUNT, "claude", OPUS)
        entry = next(m for m in self.c.get("/api/budget/status", headers=W).json()["models"]
                     if m["key"] == "claude:claude-opus-*")
        by_account = {a["account"]: a for a in entry["accounts"]}
        self.assertEqual(by_account[FLEET_ACCOUNT]["state"], "limited")
        self.assertEqual(by_account[FLEET_ACCOUNT]["limit_scope"], "model")
        self.assertEqual(by_account[SPARE_ACCOUNT]["state"], "ok")
        self.assertIsNotNone(entry["any_account_limited_until"])


class LiveScopeTests(LimitBase):
    def test_a_model_limit_is_named_and_not_shown_as_the_account(self):
        self.set_rules(limit_scope="model")
        self.limit_row(FLEET_ACCOUNT, "claude", OPUS)
        row = next(a for a in self.live()["accounts"] if a["account"] == FLEET_ACCOUNT)
        self.assertEqual(row["limit_scope"], "model")
        self.assertEqual(row["limited_model"], OPUS)
        self.assertIn(OPUS, row["status"])

    def test_an_account_scope_limit_is_the_account(self):
        self.set_rules(limit_scope="account")
        self.limit_row(FLEET_ACCOUNT, "claude", OPUS)
        row = next(a for a in self.live()["accounts"] if a["account"] == FLEET_ACCOUNT)
        self.assertEqual((row["limit_scope"], row["limited_model"]), ("account", None))
        self.assertTrue(row["status"].startswith("limited until"))


class DistinctKeyTests(unittest.TestCase):
    def test_two_models_of_one_rules_key_do_not_limit_the_account(self):
        now = time.time()
        rows = [{"agent": "claude", "model": m, "failure_reason": "limit", "resets_at": now + 600, "ended_at": now - 5,
                 "state": "failed"} for m in ("claude-opus-5-5", "claude-opus-5-6")]
        state = budget.limit_state(rows, now, "model", rules())
        self.assertEqual(state["accounts"], {})
        rows.append(dict(rows[0], model="claude-sonnet-5-5"))
        self.assertEqual(set(budget.limit_state(rows, now, "model", rules())["accounts"]), {"claude"})


class CleanerTests(unittest.TestCase):
    def test_a_malformed_report_is_dropped(self):
        self.assertIsNone(budget.clean_codex_rate_limits("lots"))
        self.assertIsNone(budget.clean_codex_rate_limits([{"profile": "x", "primary": {"used_percent": "x"}}]))
        self.assertIsNone(budget.clean_codex_rate_limits([{"profile": "bad profile!", "primary": {"used_percent": 1, "resets_at": 2}}]))
        got = budget.clean_codex_rate_limits([
            {"profile": "fleet", "primary": {"used_percent": 3, "resets_at": 5, "window_minutes": 300, "extra": "x"},
             "secondary": None, "seen_at": 9, "junk": 1}, "junk"])
        self.assertEqual(got, [{"profile": "fleet", "seen_at": 9.0, "secondary": None,
                                "primary": {"used_percent": 3.0, "resets_at": 5.0, "window_minutes": 300.0}}])


if __name__ == "__main__":
    unittest.main()
