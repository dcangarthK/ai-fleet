"""Paid-model budget, tiered reviews and the Budget page API (docs/SPEC-paid-budget.md section 8).
Run: uv run --with fastapi --with uvicorn --with httpx --with 'mcp<2' python -m unittest -v tests/test_budget.py
"""
import asyncio
import copy
import json
import re
import time
import unittest
from pathlib import Path

# Reuse the controller test harness (fake runner, temp database, environment).
from tests.test_controller import FakeRunner, W, settle_pipeline, unmetered_claude_rules, wait_for  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

import auth  # noqa: E402
import budget  # noqa: E402
import controller  # noqa: E402
import pipeline as machine  # noqa: E402
import tasks  # noqa: E402
from store import db  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def rules(**changes) -> dict:
    body = copy.deepcopy(budget.DEFAULT_RULES)
    for path, value in changes.items():
        target = body
        keys = path.split("__")
        for key in keys[:-1]:
            target = target[key]
        target[keys[-1]] = value
    return body


def events_of(kind: str) -> list[dict]:
    with db() as c:
        return [dict(r) | {"data": json.loads(r["data"] or "{}")}
                for r in c.execute("SELECT * FROM events WHERE type=? ORDER BY id", (kind,))]


def section(path: str, body: str) -> str:
    return (f"diff --git a/{path} b/{path}\nindex 1111111..2222222 100644\n--- a/{path}\n+++ b/{path}\n"
            f"@@ -1,1 +1,2 @@\n {body}\n+{body} changed\n")


# ---------------------------------------------------------------- pure rules ----

class RulesValidationTests(unittest.TestCase):
    def test_defaults_are_valid(self):
        self.assertEqual(budget.validate_rules(copy.deepcopy(budget.DEFAULT_RULES)), budget.DEFAULT_RULES)

    def test_each_bad_field_is_refused(self):
        bad = [
            dict(rules(), surprise=1),
            rules(models__codex__tier="gold"),
            rules(models__codex__kinds=["deploy"]),
            rules(models__codex__max_concurrent=-1),
            rules(models__codex__max_concurrent=0),
            rules(models__codex__daily_tokens_soft=-5),
            rules(models__codex__default_effort={"build": "extreme"}),
            rules(models__codex__default_effort={"deploy": "low"}),
            dict(rules(), models={"gpt:x": {"tier": "cheap"}}),          # unknown agent
            dict(rules(), models={"claude:[bad": {"tier": "cheap"}}),     # bad glob
            rules(models__codex__colour="red"),
            rules(unlisted="maybe"),
            rules(prompt__max_kb=-1),
            rules(prompt__hard_max_kb=10),                                # below max_kb
            rules(prompt__exclude_globs=["/etc/passwd"]),
            rules(prompt__exclude_globs=["../secret/**"]),
            rules(prompt__exclude_globs=[""]),
            rules(limits__on_limit="panic"),
            rules(limits__fallbacks={"codex": ["claude:claude-*"]}),       # a fallback must be a concrete model
            rules(effort_ladder={"build": ["medium", "huge"]}),
            rules(effort_ladder={"deploy": ["low"]}),
            rules(limit_scope="galaxy"),
            rules(review_exec__tools=["rm -rf /"]),
            rules(review_exec__max_turns=0),
            rules(review_exec__task_minutes=-3),
            rules(reviews__stages=["paid", "local", "cloud"]),
            rules(reviews__max_paid_rounds=-1),
            rules(reviews__security_paths=["/abs/**"]),
            rules(reviews__paid_skip_when={"max_changed_lines": -2}),
            rules(reviews__paid_skip_when={"paths_only": ["docs/**"], "colour": 1}),
        ]
        for body in bad:
            with self.assertRaises(ValueError, msg=json.dumps(body)[:200]):
                budget.validate_rules(body)

    def test_defaults_do_not_re_add_a_removed_effort_ladder(self):
        body = rules()
        del body["effort_ladder"]["re_review"]
        filled = budget.with_defaults(body)
        self.assertNotIn("re_review", filled["effort_ladder"])           # the owner's removal is kept
        self.assertEqual(filled["effort_ladder"]["build"], budget.DEFAULT_RULES["effort_ladder"]["build"])
        missing = rules()
        del missing["effort_ladder"]
        self.assertEqual(budget.with_defaults(missing)["effort_ladder"], budget.DEFAULT_RULES["effort_ladder"])

    def test_skip_rule_can_be_turned_on(self):
        body = rules(reviews__paid_skip_when={"max_changed_lines": 40, "paths_only": ["docs/**", "tests/**"]})
        self.assertEqual(budget.validate_rules(body)["reviews"]["paid_skip_when"]["max_changed_lines"], 40)


class MatchingTests(unittest.TestCase):
    R = budget.DEFAULT_RULES

    def test_exact_glob_and_agent_keys(self):
        self.assertEqual(budget.match_key(self.R, "codex", None), "codex")
        self.assertEqual(budget.match_key(self.R, "codex", "gpt-whatever"), "codex")
        self.assertEqual(budget.match_key(self.R, "claude", "claude-opus-5-5"), "claude:claude-opus-*")
        self.assertEqual(budget.match_key(self.R, "claude", "claude-sonnet-5-5"), "claude:claude-sonnet-*")

    def test_most_specific_wins(self):
        body = rules()
        body["models"]["claude:claude-opus-5-5"] = {"tier": "premium", "max_concurrent": 1, "kinds": ["build"]}
        self.assertEqual(budget.match_key(body, "claude", "claude-opus-5-5"), "claude:claude-opus-5-5")
        self.assertEqual(budget.match_key(body, "claude", "claude-opus-5-1"), "claude:claude-opus-*")

    def test_unlisted_and_claude_without_a_model(self):
        # The seeded claude:* rule governs a Claude run whose model is unknown (and unknown Claude models).
        self.assertEqual(budget.match_key(self.R, "claude", "claude-mystery-1"), "claude:*")
        self.assertEqual(budget.match_key(self.R, "claude", None), "claude:*")
        self.assertEqual(budget.match_key(self.R, "claude", "claude-sonnet-5-5"), "claude:claude-sonnet-*")
        body = rules()
        del body["models"]["claude:*"]
        self.assertIsNone(budget.match_key(body, "claude", None))  # a stored body without it: admission falls back
        self.assertIsNone(budget.match_key(self.R, "ollama", "qwen3:8b"))

    def test_re_review_is_admitted_against_the_review_it_continues(self):
        codex = {"tier": "scarce", "kinds": ["final_review", "security_review", "build"]}
        self.assertFalse(budget.kind_allowed(codex, "re_review"))  # no origin proven: only an explicit listing counts
        self.assertFalse(budget.kind_allowed(codex, "re_review", "review"))
        self.assertTrue(budget.kind_allowed(codex, "re_review", "security_review"))
        self.assertTrue(budget.kind_allowed(codex, "re_review", "final_review"))
        self.assertFalse(budget.kind_allowed(codex, "re_review", "build"))  # not a review kind
        self.assertTrue(budget.kind_allowed({"kinds": ["re_review"]}, "re_review"))
        self.assertTrue(budget.kind_allowed({"kinds": ["*"]}, "re_review"))

    def test_the_generic_rule_is_never_the_suggested_alternative(self):
        body = rules()
        del body["models"]["claude:claude-haiku-*"]  # "claude:*" sorts first alphabetically, but is the last resort
        self.assertEqual(budget.cheapest_for(body, "merge"), "claude:claude-sonnet-*")
        del body["models"]["claude:claude-sonnet-*"]
        self.assertEqual(budget.cheapest_for(body, "merge"), "claude:*")  # still named when nothing else allows it

    def test_a_limit_on_the_bare_agent_holds_every_model_of_it(self):
        state = {"models": {"codex": 2000.0}, "accounts": {}}
        self.assertEqual(budget.limited_for(state, "codex", "codex:gpt-5")[0], 2000.0)
        self.assertEqual(budget.limited_for(state, "codex", "codex")[0], 2000.0)
        self.assertIsNone(budget.limited_for(state, "claude", "claude:claude-sonnet-5-5")[0])
        state = {"models": {"claude:claude-opus-5-5": 2000.0}, "accounts": {}}
        self.assertEqual(budget.limited_for(state, "claude", "claude")[0], 2000.0)  # and the other way round

    def test_unresolved_identity_is_limited_when_any_model_of_the_agent_is(self):
        state = {"models": {"claude:claude-opus-5-5": 2000.0}, "accounts": {}}
        self.assertEqual(budget.limited_for(state, "claude", "claude")[0], 2000.0)
        self.assertIsNone(budget.limited_for(state, "claude", "claude:claude-sonnet-5-5")[0])
        self.assertIsNone(budget.limited_for(state, "codex", "codex")[0])

    def test_default_effort_and_ladder(self):
        R = self.R
        self.assertEqual(budget.resolve_effort(R, "codex", "final_review", None, 0), "high")
        self.assertEqual(budget.resolve_effort(R, "codex", "security_review", None, 0), "high")
        self.assertEqual(budget.resolve_effort(R, "codex", "merge", None, 0), "medium")  # "*"
        self.assertIsNone(budget.resolve_effort(R, "claude:claude-fable-*", "review", None, 0))
        # The ladder: cheap attempts first, high only after failures.
        self.assertEqual(budget.resolve_effort(R, "codex", "build", None, 0), "medium")
        self.assertEqual(budget.resolve_effort(R, "codex", "build", None, 1), "medium")
        self.assertEqual(budget.resolve_effort(R, "codex", "build", None, 2), "high")
        self.assertEqual(budget.resolve_effort(R, "codex", "build", None, 9), "high")
        self.assertEqual(budget.resolve_effort(R, "claude:claude-sonnet-*", "fix", None, 0), "low")
        self.assertEqual(budget.resolve_effort(R, "claude:claude-sonnet-*", "re_review", None, 0), "low")
        self.assertEqual(budget.resolve_effort(R, "codex", "build", "low", 5), "low")  # explicit wins
        self.assertIsNone(budget.resolve_effort(R, None, "draft", None, 0))

    def test_cheapest_alternative(self):
        self.assertEqual(budget.cheapest_for(self.R, "merge"), "claude:claude-haiku-*")
        body = rules()
        del body["models"]["claude:claude-haiku-*"]
        self.assertEqual(budget.cheapest_for(body, "merge"), "claude:claude-sonnet-*")


class HygieneTests(unittest.TestCase):
    GLOBS = budget.DEFAULT_RULES["prompt"]

    def test_generated_sections_are_removed_and_the_rest_kept_byte_for_byte(self):
        a = section("app.py", "x = 1")
        dist = section("master/controller/ui/dist/assets/index-x.js", "var a=1;" * 40)
        b = section("tests/test_app.py", "assert x")
        prompt = "Review this.\n\nDIFF:\n" + a + dist + b
        out = budget.strip_prompt(prompt, self.GLOBS)
        self.assertIn(a, out["text"])
        self.assertIn(b, out["text"])
        self.assertNotIn("var a=1;", out["text"])
        self.assertIn("[omitted generated file master/controller/ui/dist/assets/index-x.js]", out["text"])
        self.assertEqual(out["omitted"], ["master/controller/ui/dist/assets/index-x.js"])
        self.assertEqual(out["stripped_bytes"], len(prompt.encode()) - len(out["text"].encode()))
        self.assertGreater(out["stripped_bytes"], 0)

    def test_other_globs(self):
        prompt = section("web/package-lock.json", "{}") + section("lib/x.min.js", "y") + section(
            "evals/results/run.json", "{}") + section("evals/results/deep/run.json", "{}")
        out = budget.strip_prompt(prompt, self.GLOBS)
        self.assertEqual(out["omitted"], ["web/package-lock.json", "lib/x.min.js", "evals/results/run.json"])
        self.assertIn("diff --git a/evals/results/deep/run.json", out["text"])  # evals/results/*.json is one level only
        self.assertIn("+{} changed", out["text"])

    def test_long_added_lines_are_replaced(self):
        long_line = "z" * 1500
        prompt = section("src/gen.py", long_line)
        out = budget.strip_prompt(prompt, self.GLOBS)
        self.assertIn("[line omitted: 1509 chars]", out["text"])  # "+" + 1500 + " changed"
        self.assertEqual(out["long_lines"], ["src/gen.py"])
        self.assertNotIn("+" + long_line, out["text"])

    def test_prompt_without_diff_is_untouched(self):
        text = "Write a function.\n" + ("q" * 5000)
        out = budget.strip_prompt(text, self.GLOBS)
        self.assertEqual(out["text"], text)
        self.assertEqual(out["stripped_bytes"], 0)
        self.assertFalse(budget.has_diff(text))

    def test_split_reuses_file_chunks_and_keeps_the_preamble(self):
        preamble = "SPEC:\nsomething\n\nDIFF:\n"
        files = [section(f"pkg/f{i}.py", "w" * 300) for i in range(6)]
        parts = budget.split_prompt(preamble + "".join(files), 800)
        self.assertGreater(len(parts), 1)
        for part in parts:
            self.assertTrue(part.startswith(preamble))
        joined = "".join(part[len(preamble):] for part in parts)
        self.assertEqual(joined, "".join(files))

    def test_diff_facts(self):
        diff = section("docs/a.md", "hello") + section("tests/t.py", "x")
        self.assertEqual(budget.changed_files(diff), ["docs/a.md", "tests/t.py"])
        self.assertEqual(budget.changed_lines(diff), 2)
        self.assertTrue(budget.touches(["master/mcp/server.py"], ["master/mcp/**"]))
        self.assertFalse(budget.touches(["docs/a.md"], budget.DEFAULT_RULES["reviews"]["security_paths"]))

    def test_paid_skip_rule(self):
        small = section("docs/a.md", "hello")
        off = budget.DEFAULT_RULES["reviews"]
        on = dict(off, paid_skip_when={"max_changed_lines": 40, "paths_only": ["docs/**", "tests/**"]})
        self.assertFalse(budget.paid_skip_ok(off, small, gate_passed=True, trusted_approved=True))
        self.assertTrue(budget.paid_skip_ok(on, small, gate_passed=True, trusted_approved=True))
        self.assertFalse(budget.paid_skip_ok(on, small, gate_passed=True, trusted_approved=False))
        self.assertFalse(budget.paid_skip_ok(on, small, gate_passed=False, trusted_approved=True))
        self.assertFalse(budget.paid_skip_ok(on, small + section("src/app.py", "y"), True, True))
        security = dict(on, paths_only=None, paid_skip_when={"max_changed_lines": 40, "paths_only": ["**"]})
        self.assertFalse(budget.paid_skip_ok(security, section("nodes/runner.py", "x"), True, True))
        big = "".join(section(f"docs/{i}.md", "l") for i in range(50))
        self.assertFalse(budget.paid_skip_ok(on, big, True, True))


class LimitStateTests(unittest.TestCase):
    NOW = 1_000_000.0

    def row(self, agent, model, reason, resets, ended):
        return {"agent": agent, "model": model, "failure_reason": reason, "resets_at": resets, "ended_at": ended,
                "state": "failed" if reason else "review"}

    def test_latest_settled_task_decides_and_success_clears(self):
        rows = [self.row("codex", None, "limit", self.NOW + 600, self.NOW - 100)]
        state = budget.limit_state(rows, self.NOW, "model")
        self.assertEqual(state["models"]["codex"], self.NOW + 600)
        rows.append(self.row("codex", None, None, None, self.NOW - 10))
        self.assertEqual(budget.limit_state(rows, self.NOW, "model")["models"], {})
        expired = [self.row("codex", None, "limit", self.NOW - 1, self.NOW - 100)]
        self.assertEqual(budget.limit_state(expired, self.NOW, "model")["models"], {})

    def test_a_later_cancel_block_or_error_does_not_clear_a_limit(self):
        limit = self.row("codex", None, "limit", self.NOW + 600, self.NOW - 100)
        for state, reason in (("cancelled", "cancelled"), ("blocked", "denied"), ("failed", "error"),
                              ("interrupted", "interrupted"), ("failed", "gate_failed")):
            later = self.row("codex", None, reason, None, self.NOW - 10) | {"state": state}
            got = budget.limit_state([limit, later], self.NOW, "model")
            self.assertEqual(got["models"], {"codex": self.NOW + 600}, state)
        done = self.row("codex", None, None, None, self.NOW - 10) | {"state": "done"}
        self.assertEqual(budget.limit_state([limit, done], self.NOW, "model")["models"], {})
        earlier_ok = self.row("codex", None, None, None, self.NOW - 500)  # a success before the limit changes nothing
        self.assertEqual(budget.limit_state([earlier_ok, limit], self.NOW, "model")["models"], {"codex": self.NOW + 600})

    def test_two_models_on_one_account_limit_the_account(self):
        rows = [self.row("claude", "claude-opus-5-5", "limit", self.NOW + 600, self.NOW - 50),
                self.row("claude", "claude-fable-5-2", "limit", self.NOW + 900, self.NOW - 40)]
        state = budget.limit_state(rows, self.NOW, "model")
        self.assertEqual(state["accounts"], {"claude": self.NOW + 900})
        one = budget.limit_state(rows[:1], self.NOW, "model")
        self.assertEqual(one["accounts"], {})
        self.assertEqual(budget.limit_state(rows[:1], self.NOW, "account")["accounts"], {"claude": self.NOW + 600})


class StageTests(unittest.TestCase):
    ISSUE = {"severity": "medium", "file": "app.py", "problem": "Off by one", "evidence": "x = 1",
             "expected": "2", "actual": "1", "fix": "x = 2"}

    def test_fix_round_text_says_the_runner_runs_the_gate(self):
        text = machine.resolution_text([self.ISSUE])
        self.assertNotIn("rerun the gate", text)
        self.assertIn("the runner runs the gate", text)
        # A gate-failed build fails the pipeline, so no fix round ever carries gate output (AC17's gate part).
        import inspect
        self.assertEqual(list(inspect.signature(machine.resolution_text).parameters), ["issues"])

    def test_hint_matching_for_agreement(self):
        same = dict(self.ISSUE, problem="An off-by-one", evidence="x = 1")
        other = dict(self.ISSUE, file="b.py", evidence="y = 3")
        self.assertTrue(budget.hint_matches(self.ISSUE, same))
        self.assertFalse(budget.hint_matches(self.ISSUE, other))
        rows = budget.agreement_rows([self.ISSUE, dict(self.ISSUE, file="c.py", evidence="z")], [same, other])
        self.assertEqual([(r["hint_id"], r["confirmed"]) for r in rows], [(0, True), (1, False), (None, False)])


# ---------------------------------------------------------------- API ----

def fresh_loop_state():
    controller._tick_lock = asyncio.Lock()
    controller._pipeline_locks.clear()
    tasks._locks.clear()


class BudgetApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fresh_loop_state()
        cls.ctx = TestClient(controller.app, base_url="http://127.0.0.1")
        cls.c = cls.ctx.__enter__()
        FakeRunner.agents["codex"] = "/x/codex"
        cls.c.post("/api/refresh", headers=W)
        wait_for(lambda: any(n["id"] == "master" and n["runner_ok"] for n in cls.c.get("/api/fleet").json()["nodes"]))

    @classmethod
    def tearDownClass(cls):
        FakeRunner.agents["codex"] = None
        unmetered_claude_rules()  # leave the shared database as the other suites expect it
        cls.ctx.__exit__(None, None, None)

    def setUp(self):
        self.put_rules(rules())
        self.settle_all()

    def tearDown(self):
        self.settle_all()
        self.put_rules(rules())

    # helpers
    def put_rules(self, body):
        r = self.c.put("/api/budget/rules", json={"body": body}, headers=W)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def settle_all(self):
        """Finish every running or waiting task so concurrency starts from zero."""
        with db() as c:
            ids = [r[0] for r in c.execute("SELECT id FROM tasks WHERE state IN "
                                           "('queued','starting','working','awaiting_approval')")]
            c.execute("UPDATE tasks SET state='done', failure_reason=NULL WHERE id IN (%s)" % ",".join("?" * len(ids)), ids)
            # Older limit failures in this database would keep models limited.
            c.execute("UPDATE tasks SET resets_at=NULL WHERE failure_reason='limit'")
            c.execute("UPDATE agent_usage SET resets_at=NULL WHERE outcome='limit'")
        for job in FakeRunner.jobs.values():
            if job["state"] == "running":
                job["state"] = "cancelled"

    def post(self, **body):
        return self.c.post("/api/tasks", json={"prompt": "do the thing"} | body, headers=W)

    def ok(self, **body):
        r = self.post(**body)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def sent(self, task_id):
        return [b for p, b in FakeRunner.calls if p == "/jobs" and b["id"] == task_id][0]

    def limited_codex(self, resets_in=3600, model=None, agent="codex"):
        """A settled task on the model that hit its usage limit."""
        t = self.ok(agent=agent, model=model, kind="security_review", mode="read-only", worktree=False)
        FakeRunner.jobs[t["id"]].update(state="exited", exit_code=1, result="You've hit your usage limit. try again at 2:30 PM.")
        self.c.portal.call(tasks.poll_task, t["id"])
        with db() as c:  # the limit evidence is the run's usage row; the task's own fields may change later
            c.execute("UPDATE tasks SET resets_at=?, ended_at=? WHERE id=?", (time.time() + resets_in, time.time(), t["id"]))
            c.execute("UPDATE agent_usage SET resets_at=?, ts=? WHERE task_id=? AND outcome='limit'",
                      (time.time() + resets_in, time.time(), t["id"]))
        return t

    def usage_row(self, task_id, agent, outcome, resets_at=None, identity=None):
        with db() as c:
            c.execute("INSERT INTO agent_usage (ts,task_id,run,agent,identity,outcome,resets_at) VALUES (?,?,?,?,?,?,?)",
                      (time.time(), task_id, 1, agent, identity or agent, outcome, resets_at))

    def held(self, **kw):
        t = self.ok(**({"agent": "codex", "kind": "build", "gate": "pytest"} | kw))
        self.assertEqual(t["state"], "queued")
        self.assertIn("limited until", t["error"])
        return t

    # --- 2. measurement ---
    def test_usage_is_recorded_per_run_and_aggregated(self):
        claude = self.ok(agent="claude", model="claude-sonnet-5-5", kind="draft")
        FakeRunner.jobs[claude["id"]].update(state="exited", exit_code=0, result="ok", num_turns=3, usage={
            "input": 1200, "output": 300, "cached": 5000, "cost_usd": 0.042, "model": "claude-sonnet-5-5"})
        codex = self.ok(agent="codex", kind="build", gate="pytest")
        FakeRunner.jobs[codex["id"]].update(state="exited", exit_code=0, gate={"state": "passed", "exit_code": 0},
                                            usage={"input": 800, "output": 200, "cached": 100, "cost_usd": None, "model": None})
        for t in (claude, codex):
            self.c.portal.call(tasks.poll_task, t["id"])
        with db() as c:
            rows = {r["task_id"]: dict(r) for r in c.execute("SELECT * FROM agent_usage WHERE task_id IN (?,?)",
                                                             (claude["id"], codex["id"]))}
        self.assertEqual((rows[claude["id"]]["input"], rows[claude["id"]]["output"], rows[claude["id"]]["cached"]),
                         (1200, 300, 5000))
        self.assertAlmostEqual(rows[claude["id"]]["cost_usd"], 0.042)
        self.assertEqual(rows[claude["id"]]["num_turns"], 3)
        self.assertEqual(rows[claude["id"]]["outcome"], "ok")
        self.assertGreater(rows[claude["id"]]["prompt_bytes"], 0)
        self.assertEqual((rows[codex["id"]]["input"], rows[codex["id"]]["cost_usd"], rows[codex["id"]]["kind"]),
                         (800, None, "build"))
        self.assertEqual(self.c.get(f"/api/tasks/{claude['id']}").json()["usage"]["input"], 1200)
        by_model = {r["model"]: r for r in self.c.get("/api/budget/usage?days=1&group=model").json()["rows"]}
        self.assertGreaterEqual(by_model["claude-sonnet-5-5"]["input"], 1200)
        self.assertGreaterEqual(by_model["codex"]["output"], 200)
        kinds = {r["kind"]: r for r in self.c.get("/api/budget/usage?days=1&group=kind").json()["rows"]}
        self.assertGreaterEqual(kinds["build"]["tasks"], 1)
        days = self.c.get("/api/budget/usage?days=7&group=day").json()["rows"]
        self.assertTrue(any(r["model"] == "claude-sonnet-5-5" for r in days))
        self.assertEqual(self.c.get("/api/budget/usage?group=colour").status_code, 400)

    def test_local_calls_fold_in_at_zero_cost(self):
        self.c.post("/api/usage/calls", json={"role": "reviewer", "model": "phi4:14b", "ms": 10, "ok": True,
                                              "tokens_in": 111, "tokens_out": 22}, headers=W)
        rows = {r["model"]: r for r in self.c.get("/api/budget/usage?days=1&group=model").json()["rows"]}
        self.assertEqual(rows["phi4:14b"]["cost_usd"], 0)
        self.assertTrue(rows["phi4:14b"]["local"])
        self.assertGreaterEqual(rows["phi4:14b"]["input"], 111)

    # --- 3.1 / 3.3 rules 1-2 ---
    def test_kind_defaults_and_validation(self):
        self.assertEqual(self.ok(agent="claude")["kind"], "draft")
        self.assertEqual(self.ok(agent="claude", gate="pytest")["kind"], "build")
        self.assertEqual(self.post(agent="claude", kind="deploy").status_code, 400)
        self.assertEqual(self.post(agent="claude", effort="extreme").status_code, 400)

    def test_codex_merge_is_refused_naming_a_cheaper_model_and_override_is_admin_only(self):
        r = self.post(agent="codex", kind="merge", gate="pytest")
        self.assertEqual(r.status_code, 400)
        self.assertIn("codex is reserved for [final_review, security_review, build, lead]", r.text)
        self.assertIn("use claude:claude-haiku-* for merge", r.text)
        t = self.ok(agent="codex", kind="merge", gate="pytest", budget_override="owner merging a hotfix")
        self.assertEqual(t["budget_override"], "owner merging a hotfix")
        self.assertEqual(t["state"], "working")
        # The orchestrator's MCP calls are operate scope: an override is never theirs to make.
        r = self.c.post("/api/tasks", json={"agent": "codex", "prompt": "x", "kind": "merge", "gate": "pytest",
                                            "budget_override": "please"}, headers=W | {"X-Fleet-Scope": "operate"})
        self.assertEqual(r.status_code, 403)
        remote = TestClient(controller.app, base_url="https://master.tail.ts.net:8701")
        op = auth.create_token("operate", "orchestrator")
        r = remote.post("/api/tasks", json={"agent": "codex", "prompt": "x", "kind": "merge", "gate": "pytest",
                                            "budget_override": "please"}, headers={"Authorization": f"Bearer {op}", **W})
        self.assertEqual(r.status_code, 403)
        decisions = self.c.get("/api/budget/decisions").json()
        self.assertTrue(any(d["decision"] == "refused" and "reserved" in d["reason"] for d in decisions))

    def test_checks_on_paid_models_are_refused(self):
        for agent in ("claude", "codex"):
            r = self.post(agent=agent, kind="check", mode="read-only", worktree=False, budget_override="x")
            self.assertEqual(r.status_code, 400, agent)
        self.assertEqual(self.post(agent="ollama", model="fake-model:1b", kind="check").status_code, 200)

    # --- 3.3 rule 3: limits ---
    def test_limit_queue_then_start_after_reset(self):
        self.limited_codex()
        t = self.ok(agent="codex", kind="build", gate="pytest")
        self.assertEqual(t["state"], "queued")
        self.assertRegex(t["error"], r"codex limited until \d{1,2}:\d\d [AP]M")
        self.assertNotIn(t["id"], FakeRunner.jobs)
        self.c.portal.call(tasks.recover)
        self.assertEqual(self.c.get(f"/api/tasks/{t['id']}").json()["state"], "queued")
        with db() as c:
            c.execute("UPDATE agent_usage SET resets_at=? WHERE outcome='limit'", (time.time() - 1,))
        self.c.portal.call(tasks.recover)
        self.assertEqual(self.c.get(f"/api/tasks/{t['id']}").json()["state"], "working")

    def test_limit_fallback_records_routed_from(self):
        self.put_rules(rules(limits__on_limit="fallback"))
        self.limited_codex()
        t = self.ok(agent="codex", kind="security_review", mode="read-only", worktree=False)
        self.assertEqual((t["agent"], t["model"], t["routed_from"]), ("claude", "claude-fable-5-2", "codex"))
        self.assertEqual(t["state"], "working")
        decisions = self.c.get("/api/budget/decisions").json()
        self.assertTrue(any(d["decision"] == "rerouted" and d.get("task_id") == t["id"] for d in decisions))

    def test_limit_refuse(self):
        self.put_rules(rules(limits__on_limit="refuse"))
        self.limited_codex()
        self.assertEqual(self.post(agent="codex", kind="build", gate="pytest").status_code, 409)

    def test_success_clears_the_limit(self):
        self.limited_codex()
        good = self.ok(agent="codex", kind="build", gate="pytest", budget_override="probe")  # queued: limited
        self.assertEqual(good["state"], "queued")
        time.sleep(0.01)
        self.usage_row("t-okcodex", "codex", "ok")  # a later Codex run succeeded (e.g. the owner's own, early reset)
        self.c.portal.call(tasks.recover)
        self.assertEqual(self.c.get(f"/api/tasks/{good['id']}").json()["state"], "working")

    def test_account_limit_skips_a_fallback_on_the_same_account(self):
        self.put_rules(rules(limits__on_limit="fallback"))
        self.limited_codex(agent="claude", model="claude-opus-5-5")  # a Claude limit holds the whole account
        t = self.ok(agent="claude", model="claude-opus-5-5", kind="build", gate="pytest")
        self.assertEqual((t["agent"], t["model"], t["state"]), ("claude", "claude-opus-5-5", "queued"))
        self.assertIn("claude account limited", t["error"])
        status = {s["key"]: s for s in self.c.get("/api/budget/status").json()["models"]}
        self.assertEqual(status["claude:claude-sonnet-*"]["state"], "limited")

    def test_a_codex_limit_on_stderr_becomes_a_limit_failure_with_its_reset_time(self):
        import datetime
        soon = datetime.datetime.now() + datetime.timedelta(hours=1)  # a passed time is never rolled to tomorrow
        if soon.date() != datetime.date.today():
            self.skipTest("less than an hour left in the day")
        clock_text = f"{soon.hour % 12 or 12}:{soon.minute:02d} {'AM' if soon.hour < 12 else 'PM'}"
        t = self.ok(agent="codex", kind="build", gate="pytest")
        FakeRunner.jobs[t["id"]].update(
            state="exited", exit_code=1, result="", gate=None,
            error=f"error: You've hit your usage limit. Upgrade to Pro, or try again at {clock_text}.")
        self.c.portal.call(tasks.poll_task, t["id"])
        got = self.c.get(f"/api/tasks/{t['id']}").json()
        self.assertEqual((got["state"], got["failure_reason"]), ("failed", "limit"))
        self.assertIn(f"try again at {clock_text}", got["error"])
        resets = datetime.datetime.fromtimestamp(got["resets_at"])  # the machine's own zone
        self.assertEqual((resets.hour, resets.minute), (soon.hour, soon.minute))

    def test_codex_re_review_needs_a_proven_security_or_final_origin(self):
        body = dict(agent="codex", kind="re_review", mode="read-only", worktree=False)
        r = self.post(**body)
        self.assertEqual(r.status_code, 400)
        self.assertIn("codex is reserved for", r.text)
        self.assertEqual(self.post(**body, origin_kind="review").status_code, 400)
        self.assertEqual(self.post(agent="claude", kind="review", origin_kind="review").status_code, 400)  # not a re_review
        self.assertEqual(self.post(**body, origin_kind="merge").status_code, 400)  # not a review kind
        t = self.ok(**body, origin_kind="security_review")
        self.assertEqual(t["kind"], "re_review")  # accounting kind is unchanged

    def test_cancelling_the_limited_task_itself_keeps_the_limit(self):
        limited = self.limited_codex()
        r = self.c.post(f"/api/tasks/{limited['id']}/cancel", headers=W)
        self.assertEqual(r.json()["state"], "cancelled")
        self.held()

    def test_completing_the_limited_task_itself_keeps_the_limit(self):
        limited = self.limited_codex()
        r = self.c.post(f"/api/tasks/{limited['id']}/complete", headers=W)
        self.assertEqual(r.json()["state"], "done")
        self.held()
        self.settle_all()
        other = self.limited_codex(agent="claude", model="claude-sonnet-5-5")
        out = self.c.post(f"/api/tasks/{other['id']}/outcome", json={"kind": "done"}, headers=W)
        self.assertEqual(out.status_code, 200, out.text)
        self.held(agent="claude", model="claude-sonnet-5-5", kind="draft", gate=None)

    def test_resuming_the_limited_task_itself_keeps_the_limit_until_a_run_succeeds(self):
        limited = self.limited_codex()
        # A resume passes admission like a new task (docs/SPEC-limit-handoff.md 4.5): refused while still limited.
        r = self.c.post(f"/api/tasks/{limited['id']}/message", json={"text": "carry on"}, headers=W)
        self.assertEqual(r.status_code, 409, r.text)
        self.assertIn("limited until", r.text)
        self.held()
        self.c.post(f"/api/tasks/{limited['id']}/cancel", headers=W)
        self.held()
        self.settle_all()
        again = self.limited_codex()
        with db() as c:  # its reset has passed: the resume is admitted
            c.execute("UPDATE agent_usage SET resets_at=? WHERE task_id=?", (time.time() - 1, again["id"]))
        r = self.c.post(f"/api/tasks/{again['id']}/message", json={"text": "carry on"}, headers=W)
        self.assertEqual(r.json()["state"], "working")  # its failure fields are cleared for the new run
        time.sleep(0.01)
        FakeRunner.jobs[again["id"]].update(state="exited", exit_code=0, result="done")
        self.c.portal.call(tasks.poll_task, again["id"])
        self.assertEqual(self.ok(agent="codex", kind="build", gate="pytest")["state"], "working")  # a success clears it

    def test_a_report_that_merely_discusses_a_limit_does_not_hold_the_model(self):
        t = self.ok(agent="codex", kind="security_review", mode="read-only", worktree=False)
        FakeRunner.jobs[t["id"]].update(
            state="exited", exit_code=1,
            result="Review notes: when the usage limit is hit the client shows that it resets at 2:30 PM; please retry.")
        self.c.portal.call(tasks.poll_task, t["id"])
        got = self.c.get(f"/api/tasks/{t['id']}").json()
        self.assertEqual(got["state"], "failed")
        self.assertEqual(self.ok(agent="codex", kind="build", gate="pytest")["state"], "working")

    def test_a_limit_shown_but_not_recognised_as_the_providers_is_reported_once(self):
        before = len(events_of("budget.limit_unrecognised"))
        t = self.ok(agent="codex", kind="security_review", mode="read-only", worktree=False)
        text = ("Note token=abcdefghijklmnop1234 so the usage limit was hit and it resets at 2:30 PM. "
                + "padding " * 60)
        FakeRunner.jobs[t["id"]].update(state="exited", exit_code=1, result=text)
        self.c.portal.call(tasks.poll_task, t["id"])
        self.assertEqual(tasks.get_task(t["id"])["failure_reason"], "limit")
        tasks.record_usage(t["id"])  # a repeat for the same run adds nothing
        found = events_of("budget.limit_unrecognised")[before:]
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["task_id"], t["id"])
        shown = found[0]["data"]["text"]
        self.assertEqual(len(shown), 200)
        self.assertTrue(shown.startswith("Note token=[redacted] so the usage limit"))
        self.assertNotIn("abcdefghijklmnop1234", shown)
        self.limited_codex()  # the provider's own message is a real limit, not an unrecognised one
        self.assertEqual(len(events_of("budget.limit_unrecognised")), before + 1)

    def test_read_only_tasks_launch_with_no_pre_approved_tools(self):
        r = self.c.put("/api/policies/agent/claude", json={"body": {"auto_approve": {
            "agents": ["claude"], "denied_tools": True, "tools": ["Edit", "Bash(git *)"]}}}, headers=W)
        self.assertEqual(r.status_code, 200, r.text)
        try:
            t = self.ok(agent="claude", kind="review", mode="read-only", worktree=False)
            self.assertEqual(self.sent(t["id"])["allowed_tools"], [])
            self.settle_all()
            w = self.ok(agent="claude", kind="build", gate="pytest")
            self.assertEqual(self.sent(w["id"])["allowed_tools"], ["Bash(git *)", "Edit"])
        finally:
            self.c.put("/api/policies/agent/claude", json={"body": {}}, headers=W)

    def test_stored_rules_missing_keys_are_filled_and_invalid_ones_are_reported(self):
        body = rules()
        for key in ("reviews", "effort_ladder"):
            del body[key]
        del body["prompt"]["max_line_chars"]
        body["models"]["codex"]["max_concurrent"] = 7  # the owner's edit survives
        with db() as c:
            version = c.execute("SELECT MAX(version)+1 FROM budget_rules").fetchone()[0]
            c.execute("INSERT INTO budget_rules (version, body, author, created_at) VALUES (?,?,?,?)",
                      (version, json.dumps(body), "test", time.time()))
        current = budget.current_rules()
        self.assertEqual(current["models"]["codex"]["max_concurrent"], 7)
        self.assertEqual(current["reviews"], budget.DEFAULT_RULES["reviews"])
        self.assertEqual(current["prompt"]["max_line_chars"], budget.DEFAULT_RULES["prompt"]["max_line_chars"])
        self.assertFalse(self.c.get("/api/budget/status").json()["rules_invalid"])
        before = len(events_of("budget.rules_invalid"))
        broken = rules()
        broken["models"]["codex"]["tier"] = "platinum"
        with db() as c:
            c.execute("INSERT INTO budget_rules (version, body, author, created_at) VALUES (?,?,?,?)",
                      (version + 1, json.dumps(broken), "test", time.time()))
        self.assertEqual(budget.current_rules()["models"]["codex"]["max_concurrent"], 1)  # defaults in force
        status = self.c.get("/api/budget/status").json()
        self.assertTrue(status["rules_invalid"])
        self.assertIn("tier", status["rules_error"])
        budget.current_rules()
        self.assertEqual(len(events_of("budget.rules_invalid")), before + 1)  # once per stored version

    def test_model_less_claude_is_held_by_the_limit_breaker(self):
        self.limited_codex(agent="claude", model=None)  # a Claude task with no model hit its limit
        t = self.ok(agent="claude", kind="build", gate="pytest")
        self.assertEqual(t["state"], "queued")
        self.assertRegex(t["error"], r"claude (account )?limited until \d{1,2}:\d\d [AP]M")
        self.assertNotIn(t["id"], FakeRunner.jobs)
        self.assertEqual(t["budget_key"], "claude:*")

    def test_model_less_claude_is_held_even_when_the_stored_rules_lack_a_claude_rule(self):
        body = rules()
        del body["models"]["claude:*"]
        self.put_rules(body)
        self.limited_codex(agent="claude", model=None)
        t = self.ok(agent="claude", kind="build", gate="pytest")
        self.assertEqual(t["state"], "queued")
        self.assertIn("limited until", t["error"])
        self.assertEqual(t["budget_key"], "claude:*")  # the governed fallback identity

    def test_model_less_claude_is_held_when_a_named_claude_model_is_limited(self):
        self.limited_codex(agent="claude", model="claude-opus-5-5")  # we can't know the default isn't Opus
        t = self.ok(agent="claude", kind="build", gate="pytest")
        self.assertEqual(t["state"], "queued")
        self.assertIn("limited until", t["error"])

    def test_model_less_claude_respects_the_concurrency_limit(self):
        body = rules()
        body["models"]["claude:*"]["max_concurrent"] = 1
        self.put_rules(body)
        first = self.ok(agent="claude", kind="draft")
        second = self.ok(agent="claude", kind="draft")
        self.assertEqual(first["state"], "working")
        self.assertEqual((second["state"], second["error"]), ("queued", "waiting for a claude:* slot"))
        body = rules()
        del body["models"]["claude:*"]  # no rule at all: still bounded by the conservative fallback
        self.put_rules(body)
        self.settle_all()
        one = self.ok(agent="claude", kind="draft")
        two = self.ok(agent="claude", kind="draft")
        three = self.ok(agent="claude", kind="draft")
        self.assertEqual([one["state"], two["state"], three["state"]], ["working", "working", "queued"])

    def test_the_runners_reported_default_model_is_the_admission_identity(self):
        # Report the default through the fake runner's /health. The controller's poll loop rewrites nodes.runner
        # from it, so editing the row directly races the poll.
        previous = FakeRunner.health_extra
        FakeRunner.health_extra = {"default_models": {"claude": "claude-sonnet-5-5"}}

        def reported():
            with db() as c:
                row = c.execute("SELECT runner FROM nodes WHERE id='master'").fetchone()
            return ((json.loads(row["runner"] or "null") or {}).get("default_models") or {}).get("claude")

        def restore():
            FakeRunner.health_extra = previous
            self.c.post("/api/refresh", headers=W)
            wait_for(lambda: reported() is None)
        self.addCleanup(restore)
        self.c.post("/api/refresh", headers=W)
        wait_for(lambda: reported() == "claude-sonnet-5-5")
        t = self.ok(agent="claude", kind="draft")
        self.assertEqual((t["budget_key"], t["budget_identity"]), ("claude:claude-sonnet-*", "claude:claude-sonnet-5-5"))

    def test_a_cancelled_task_does_not_clear_an_active_limit(self):
        self.limited_codex()
        later = self.ok(agent="codex", kind="build", gate="pytest")  # queued behind the limit
        self.c.post(f"/api/tasks/{later['id']}/cancel", headers=W)
        with db() as c:  # the cancelled attempt is the latest settled Codex task, ended after the limit
            c.execute("UPDATE tasks SET ended_at=?, budget_identity='codex' WHERE id=?", (time.time() + 1, later["id"]))
        self.usage_row(later["id"], "codex", "cancelled")
        t = self.ok(agent="codex", kind="build", gate="pytest")
        self.assertEqual(t["state"], "queued")
        self.assertIn("limited until", t["error"])

    def test_a_task_with_no_model_gets_its_kinds_default_never_the_parents(self):
        for kind, model in (("build", "claude-sonnet-5-5"), ("test", "claude-haiku-5-5"),
                            ("final_review", "claude-opus-5-5")):
            t = self.ok(agent="claude", kind=kind, mode="read-only", worktree=False)
            self.assertEqual(self.sent(t["id"]).get("model"), model, kind)
            self.settle_all()
        named = self.ok(agent="claude", kind="build", model="claude-haiku-5-5", mode="read-only", worktree=False)
        self.assertEqual(self.sent(named["id"]).get("model"), "claude-haiku-5-5")  # an explicit model is kept

    def test_a_lead_is_refused_without_an_allowed_model_and_gets_the_30_turn_cap(self):
        r = self.post(agent="claude", kind="lead", model="claude-haiku-5-5")
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn("lead tasks require", r.text)
        lead = self.ok(agent="claude", kind="lead", model="claude-sonnet-5-5")
        self.assertEqual(self.sent(lead["id"]).get("max_turns"), 30)

    def test_continue_job_must_be_the_replaced_task_with_a_worktree(self):
        first = self.ok(agent="claude", kind="build", mode="read-only", worktree=True)
        self.settle_all()
        r = self.post(agent="claude", kind="build", continue_job=first["id"], worktree=True)  # no replaces
        self.assertEqual(r.status_code, 400, r.text)
        r = self.post(agent="claude", kind="build", continue_job=first["id"], replaces=first["id"], worktree=False)
        self.assertEqual(r.status_code, 400, r.text)

    def test_a_continuation_prompt_over_4_kb_is_refused(self):
        first = self.ok(agent="claude", kind="build", mode="read-only", worktree=True)
        self.settle_all()
        big = self.post(agent="claude", kind="build", continue_job=first["id"], replaces=first["id"], worktree=True,
                        prompt="x" * 4097)
        self.assertEqual(big.status_code, 400, big.text)
        self.assertIn("4 KB", big.text)
        small = self.post(agent="claude", kind="build", continue_job=first["id"], replaces=first["id"],
                          worktree=True, prompt="x" * 4096)
        self.assertNotIn("4 KB", small.text)  # the size check passes; other admission rules are not under test

    def test_tasks_record_the_profile_and_account_label_when_known(self):
        plain = self.ok(agent="codex", kind="build", gate="pytest")
        self.assertEqual((plain["profile"], plain["account_label"]), ("fleet", None))
        self.settle_all()
        t = self.ok(agent="claude", model="claude-sonnet-5-5", kind="draft")
        FakeRunner.jobs[t["id"]].update(state="exited", exit_code=0, result="ok", profile="shared", account_label="work")
        self.c.portal.call(tasks.poll_task, t["id"])
        got = self.c.get(f"/api/tasks/{t['id']}").json()
        self.assertEqual((got["profile"], got["account_label"]), ("shared", "work"))
        with db() as c:
            have = {r[1] for r in c.execute("PRAGMA table_info(tasks)")}
        self.assertTrue({"profile", "account_label"} <= have)

    # --- 3.3 rule 4: concurrency ---
    def test_concurrency_queues_and_starts_fifo(self):
        first = self.ok(agent="codex", kind="build", gate="pytest")
        second = self.ok(agent="codex", kind="build", gate="pytest")
        third = self.ok(agent="codex", kind="build", gate="pytest")
        self.assertEqual(first["state"], "working")
        self.assertEqual((second["state"], second["error"]), ("queued", "waiting for a codex slot"))
        self.assertEqual(third["state"], "queued")
        FakeRunner.jobs[first["id"]].update(state="exited", exit_code=0, gate={"state": "passed", "exit_code": 0})
        self.c.portal.call(tasks.poll_task, first["id"])
        self.c.portal.call(tasks.recover)
        self.assertEqual(self.c.get(f"/api/tasks/{second['id']}").json()["state"], "working")
        self.assertEqual(self.c.get(f"/api/tasks/{third['id']}").json()["state"], "queued")

    def test_slot_waiters_are_recorded_and_older_unrelated_holds_do_not_count(self):
        first = self.ok(agent="codex", kind="build", gate="pytest")
        second = self.ok(agent="codex", kind="build", gate="pytest")
        self.assertEqual(tasks.get_task(second["id"])["budget_wait"], "slot")
        self.assertIsNone(tasks.get_task(first["id"])["budget_wait"])
        FakeRunner.jobs[first["id"]].update(state="exited", exit_code=0, gate={"state": "passed", "exit_code": 0})
        self.c.portal.call(tasks.poll_task, first["id"])
        # An older queued Codex task held for a reason unrelated to the budget (say quiet hours or an offline
        # node) must not starve a younger task that is free to run.
        with db() as c:
            c.execute("UPDATE tasks SET state='done' WHERE id=?", (second["id"],))
            c.execute("INSERT INTO tasks (id,agent,prompt,state,budget_key,budget_identity,kind,created_at,updated_at,"
                      "node_id,mode,policy) VALUES ('t-oldhold','codex','x','queued','codex','codex','build',?,?,"
                      "'master','auto-edit',?)",
                      (time.time() - 3600, time.time() - 3600, json.dumps({"limits": {"max_concurrent": 0}})))
        self.c.portal.call(tasks.recover)  # it is held by node capacity, nothing to do with the budget
        old = tasks.get_task("t-oldhold")
        self.assertEqual((old["state"], old["error"], old["budget_wait"]), ("queued", "max concurrent tasks reached", None))
        young = self.ok(agent="codex", kind="build", gate="pytest")
        self.assertEqual(young["state"], "working")

    def test_limited_and_at_concurrency_reports_the_limit(self):
        self.ok(agent="codex", kind="build", gate="pytest")  # holds the only slot
        self.usage_row("t-limboth", "codex", "limit", time.time() + 3600)  # and Codex hit its limit
        t = self.ok(agent="codex", kind="build", gate="pytest")
        self.assertIn("limited until", t["error"])

    def test_override_does_not_bypass_a_limit(self):
        self.limited_codex()
        t = self.ok(agent="codex", kind="merge", gate="pytest", budget_override="owner says so")
        self.assertEqual(t["state"], "queued")
        self.assertIn("limited until", t["error"])

    def test_admission_never_approves(self):
        self.limited_codex()
        t = self.ok(agent="codex", kind="build", mode="supervised")  # supervised tasks take no gate
        self.assertEqual(t["state"], "awaiting_approval")  # the person still decides first
        approved = self.c.post(f"/api/tasks/{t['id']}/approve", headers=W).json()
        self.assertEqual(approved["state"], "queued")  # then it waits for the limit; nothing was widened
        self.assertNotIn(t["id"], FakeRunner.jobs)

    # --- 3.3 rule 5: daily tokens ---
    def test_daily_soft_and_hard_tokens(self):
        self.put_rules(rules(models__codex__daily_tokens_soft=1000, models__codex__daily_tokens_hard=5000))
        with db() as c:
            c.execute("INSERT INTO agent_usage (ts,task_id,run,agent,model,kind,input,output,cached,outcome) "
                      "VALUES (?,?,?,?,?,?,?,?,?,?)", (time.time(), "t-soft", 1, "codex", None, "build", 1500, 100, 0, "ok"))
        with db() as c:
            c.execute("DELETE FROM events WHERE type='budget.soft'")
        t = self.ok(agent="codex", kind="build", gate="pytest")
        self.assertEqual(t["state"], "working")
        self.assertEqual(len(events_of("budget.soft")), 1)
        self.settle_all()
        self.ok(agent="codex", kind="build", gate="pytest")
        self.assertEqual(len(events_of("budget.soft")), 1)  # once per day per model
        with db() as c:
            c.execute("UPDATE agent_usage SET input=6000 WHERE task_id='t-soft'")
        self.settle_all()
        hard = self.ok(agent="codex", kind="build", gate="pytest")
        self.assertEqual(hard["state"], "queued")
        self.assertIn("daily token budget", hard["error"])
        with db() as c:
            c.execute("DELETE FROM agent_usage WHERE task_id='t-soft'")

    # --- 3.1 effort, 3.7 review limits, 3.8 tools at launch ---
    def test_effort_reaches_the_runner(self):
        t = self.ok(agent="codex", kind="build", gate="pytest", effort="medium")
        self.assertEqual(self.sent(t["id"])["effort"], "medium")
        self.assertEqual(t["effort"], "medium")
        self.settle_all()
        t = self.ok(agent="codex", kind="final_review", mode="read-only", worktree=False)
        self.assertEqual(self.sent(t["id"])["effort"], "high")  # the rules' default for (codex, final_review)

    def test_effort_ladder_follows_failed_gate_rounds(self):
        root = self.ok(agent="codex", kind="build", gate="pytest")
        self.assertEqual(root["effort"], "medium")
        FakeRunner.jobs[root["id"]].update(state="exited", exit_code=0, gate={"state": "failed", "exit_code": 1})
        self.c.portal.call(tasks.poll_task, root["id"])
        retry = self.ok(agent="codex", kind="build", gate="pytest", parent_id=root["id"], link="retry")
        self.assertEqual(retry["effort"], "medium")
        FakeRunner.jobs[retry["id"]].update(state="exited", exit_code=0, gate={"state": "failed", "exit_code": 1})
        self.c.portal.call(tasks.poll_task, retry["id"])
        third = self.ok(agent="codex", kind="build", gate="pytest", parent_id=retry["id"], link="retry")
        self.assertEqual(third["effort"], "high")

    def test_review_tasks_launch_with_only_review_tools_and_limits(self):
        t = self.ok(agent="claude", model="claude-fable-5-2", kind="review", mode="read-only", worktree=False)
        sent = self.sent(t["id"])
        self.assertEqual(sent["tools"], ["Read", "Grep", "Glob"])
        self.assertEqual(sent["max_turns"], 8)
        self.assertEqual(sent["policy"]["limits"]["task_minutes"], 15)
        self.settle_all()
        build = self.ok(agent="claude", model="claude-sonnet-5-5", kind="build", gate="pytest")
        self.assertNotIn("tools", self.sent(build["id"]))

    def test_an_explicitly_empty_review_tool_list_means_no_tools(self):
        self.put_rules(rules(review_exec__tools=[]))
        t = self.ok(agent="claude", model="claude-fable-5-2", kind="review", mode="read-only", worktree=False)
        sent = self.sent(t["id"])
        self.assertIn("tools", sent)
        self.assertEqual(sent["tools"], [])  # never dropped, which would leave the reviewer unrestricted

    def test_policy_pre_approved_tools_are_granted_at_launch(self):
        r = self.c.put("/api/policies/agent/claude", json={"body": {"auto_approve": {
            "agents": ["claude"], "denied_tools": True, "tools": ["Edit", "Bash(git *)"]}}}, headers=W)
        self.assertEqual(r.status_code, 200, r.text)
        try:
            t = self.ok(agent="claude", kind="build", gate="pytest")
            self.assertEqual(self.sent(t["id"])["allowed_tools"], ["Bash(git *)", "Edit"])
            # Without denied-tool auto-approval in the policy nothing is granted up front.
            self.c.put("/api/policies/agent/claude", json={"body": {"auto_approve": {
                "agents": ["claude"], "denied_tools": False, "tools": ["Edit"]}}}, headers=W)
            t = self.ok(agent="claude", kind="build", gate="pytest")
            self.assertEqual(self.sent(t["id"])["allowed_tools"], [])
        finally:
            self.c.put("/api/policies/agent/claude", json={"body": {}}, headers=W)

    # --- 3.4 hygiene ---
    def test_generated_sections_are_stripped_from_task_prompts(self):
        prompt = "Review:\n" + section("app.py", "x") + section("master/controller/ui/dist/assets/index-q.js", "var b;" * 99)
        t = self.ok(agent="claude", kind="review", mode="read-only", worktree=False, prompt=prompt)
        sent = self.sent(t["id"])["prompt"]
        self.assertNotIn("var b;", sent)
        self.assertIn("[omitted generated file master/controller/ui/dist/assets/index-q.js]", sent)
        self.assertGreater(t["prompt_stripped_bytes"], 0)
        self.assertEqual(t["prompt_omitted"], ["master/controller/ui/dist/assets/index-q.js"])

    def test_large_prompts_split_warn_or_refuse(self):
        self.put_rules(rules(prompt__max_kb=2, prompt__hard_max_kb=6))
        diff = "".join(section(f"pkg/m{i}.py", "k" * 700) for i in range(5))
        first = self.ok(agent="claude", model="claude-sonnet-5-5", kind="review", mode="read-only", worktree=False,
                        prompt="Review these.\n" + diff)
        self.assertGreater(len(first["split"]), 1)
        for child in first["split"][1:]:
            got = self.c.get(f"/api/tasks/{child}").json()
            self.assertEqual((got["parent_id"], got["link"]), (first["id"], "review"))
            self.assertLessEqual(len(got["prompt"].encode()), 2048)
            self.assertTrue(got["prompt"].startswith("Review these.\n"))
        self.settle_all()
        plain = self.ok(agent="claude", model="claude-sonnet-5-5", kind="draft", prompt="p" * 3000)
        self.assertEqual(plain["split"], [])
        warned = [e for e in events_of("budget.large_prompt") if e["task_id"] == plain["id"]]
        self.assertEqual(len(warned), 1)
        self.assertEqual(self.post(agent="claude", kind="draft", prompt="p" * 7000).status_code, 400)

    def test_split_parts_share_the_given_parent(self):
        self.put_rules(rules(prompt__max_kb=2, prompt__hard_max_kb=6))
        parent = self.ok(agent="claude", model="claude-sonnet-5-5", kind="build", gate="pytest")
        diff = "".join(section(f"pkg/m{i}.py", "k" * 700) for i in range(5))
        # A different model from the builder: ad-hoc reviews must be independent (docs/SPEC-live-monitor.md 6).
        first = self.ok(agent="claude", model="claude-fable-5-2", kind="review", mode="read-only", worktree=False,
                        prompt="Review these.\n" + diff, parent_id=parent["id"], link="review")
        self.assertGreater(len(first["split"]), 1)
        for part in first["split"]:
            got = self.c.get(f"/api/tasks/{part}").json()
            self.assertEqual((got["parent_id"], got["link"]), (parent["id"], "review"))

    def test_size_check_is_after_stripping(self):
        self.put_rules(rules(prompt__max_kb=2, prompt__hard_max_kb=3))
        prompt = "Review:\n" + section("app.py", "x") + section("master/controller/ui/dist/assets/big.js", "v" * 9000)
        t = self.ok(agent="claude", kind="review", mode="read-only", worktree=False, prompt=prompt)
        self.assertEqual(t["split"], [])

    # --- 3.2 rules API ---
    def test_rules_are_admin_only_validated_versioned_and_restorable(self):
        before = self.c.get("/api/budget/rules").json()
        self.assertEqual(before["body"], budget.DEFAULT_RULES)
        self.assertEqual(self.c.put("/api/budget/rules", json={"body": rules(unlisted="sometimes")}, headers=W).status_code, 400)
        changed = self.put_rules(rules(models__codex__max_concurrent=2))
        self.assertEqual(changed["version"], before["version"] + 1)
        history = self.c.get("/api/budget/rules/history").json()
        self.assertEqual(history[0]["version"], changed["version"])
        restored = self.c.post("/api/budget/rules/restore", json={"version": before["version"]}, headers=W).json()
        self.assertEqual(restored["body"], before["body"])
        self.assertEqual(restored["version"], changed["version"] + 1)
        self.assertEqual(self.c.post("/api/budget/rules/restore", json={"version": 99999}, headers=W).status_code, 404)
        remote = TestClient(controller.app, base_url="https://master.tail.ts.net:8701")
        op = auth.create_token("operate", "op")
        h = {"Authorization": f"Bearer {op}", **W}
        self.assertEqual(remote.put("/api/budget/rules", json={"body": rules()}, headers=h).status_code, 403)
        self.assertEqual(remote.post("/api/budget/rules/restore", json={"version": 1}, headers=h).status_code, 403)
        self.assertEqual(remote.get("/api/budget/rules", headers=h).status_code, 200)

    def test_seeding_never_overwrites_edits(self):
        edited = self.put_rules(rules(models__codex__max_concurrent=3))
        budget.seed_rules()
        self.assertEqual(self.c.get("/api/budget/rules").json()["version"], edited["version"])

    # --- 6. orchestrator view ---
    def test_fleet_status_carries_budget_and_profiles(self):
        fleet = self.c.get("/api/fleet").json()
        keys = {m["key"] for m in fleet["budget"]["models"]}
        self.assertIn("codex", keys)
        master = next(n for n in fleet["nodes"] if n["id"] == "master")
        self.assertEqual(master["codex_profile"], "shared")
        status = self.c.get("/api/budget/status").json()
        codex = next(m for m in status["models"] if m["key"] == "codex")
        self.assertEqual((codex["state"], codex["max_concurrent"]), ("ok", 1))
        self.assertTrue(status["fleet_only"])


class PipelineBudgetTests(unittest.TestCase):
    """Delta re-reviews, the reviewer's working directory, review caps and tiered (local-first) reviews."""
    ISSUE = {"severity": "medium", "file": "app.py", "problem": "Greeting regression", "evidence": "hello",
             "expected": "the expected greeting", "actual": "the greeting is hello", "fix": "restore the greeting"}

    @classmethod
    def setUpClass(cls):
        fresh_loop_state()
        cls.ctx = TestClient(controller.app, base_url="http://127.0.0.1")
        cls.c = cls.ctx.__enter__()
        FakeRunner.agents["codex"] = "/x/codex"
        cls.c.post("/api/refresh", headers=W)

    @classmethod
    def tearDownClass(cls):
        FakeRunner.agents["codex"] = None
        unmetered_claude_rules()
        cls.ctx.__exit__(None, None, None)

    def setUp(self):
        BudgetApiTests.settle_all(self)
        self.c.put("/api/budget/rules", json={"body": rules()}, headers=W)
        self.calls = []
        self.local_answers = []
        self.saved = (controller.pipeline_model_call, controller.auto_trusted_roles, controller.local_review_role)
        controller.local_review_role = lambda: "reviewer"
        controller.auto_trusted_roles = lambda: []

        def fake(role, spec, diff):
            self.calls.append(("local", role))
            return self.local_answers.pop(0) if self.local_answers else json.dumps({"verdict": "approve", "issues": []})
        controller.pipeline_model_call = fake
        FakeRunner.trees.clear()
        FakeRunner.deltas.clear()
        FakeRunner.delta_text = None
        FakeRunner.diff_text = None

    def tearDown(self):
        controller.pipeline_model_call, controller.auto_trusted_roles, controller.local_review_role = self.saved
        self.c.put("/api/budget/rules", json={"body": rules()}, headers=W)

    def start(self, **kw):
        body = {"title": "Budget pipeline", "spec": "Keep the greeting correct", "repo": "/repo", "gate": "pytest",
                "mode": "auto-edit", "builder": {"agent": "codex"}, "max_rounds": 3,
                "reviewers": [{"kind": "agent", "agent": "claude", "model": "claude-fable-5-2"}]} | kw
        r = self.c.post("/api/pipelines", json=body, headers=W)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def finish_build(self, p):
        FakeRunner.jobs[p["build_task"]].update(state="exited", exit_code=0, gate={"state": "passed", "exit_code": 0})

    def until(self, p, check):
        def ready(x):
            if x["state"] == "building" and FakeRunner.jobs[p["build_task"]]["state"] != "running":
                pass
            return check(x)
        return settle_pipeline(self.c, p["id"], ready)

    def review_task(self, p):
        current = self.until(p, lambda x: x if x["reviews"].get("0", {}).get("task_id") else None)
        return current["reviews"]["0"]["task_id"]

    def answer(self, task_id, verdict="approve", issues=()):
        FakeRunner.jobs[task_id].update(state="exited", exit_code=0,
                                        result=json.dumps({"verdict": verdict, "issues": list(issues)}))

    def test_reviewer_reads_the_build_worktree_and_the_prompt_never_asks_for_both(self):
        p = self.start()
        self.finish_build(p)
        rid = self.review_task(p)
        sent = [b for path, b in FakeRunner.calls if path == "/jobs" and b["id"] == rid][0]
        self.assertEqual((sent["mode"], sent["worktree"], sent["cwd"]), ("read-only", False, "/w"))
        self.assertIsNone(sent["repo"])
        self.assertIn("Read only the changed files and their direct callers", sent["prompt"])
        self.assertNotIn("Read surrounding files here", sent["prompt"])
        self.assertEqual(tasks.get_task(rid)["kind"], "review")
        self.assertEqual(self.calls[0], ("local", "reviewer"))  # local first
        self.c.post(f"/api/pipelines/{p['id']}/cancel", headers=W)

    def test_round_two_sends_findings_and_only_the_delta(self):
        FakeRunner.delta_text = section("app.py", "fixed greeting")
        p = self.start()
        self.finish_build(p)
        first = self.review_task(p)
        self.assertEqual(len(FakeRunner.trees.get(p["build_task"], [])), 1)  # tree recorded when the review starts
        self.answer(first, "request_changes", [self.ISSUE])
        self.until(p, lambda x: x if x["state"] == "building" and x["round"] == 2 else None)
        wait_for(lambda: any(path == f"/jobs/{p['build_task']}/message" for path, _ in FakeRunner.calls))
        fix = [b["text"] for path, b in FakeRunner.calls if path == f"/jobs/{p['build_task']}/message"][-1]
        self.assertNotIn("rerun the gate", fix)
        self.finish_build(p)
        second = self.until(p, lambda x: x if x["round"] == 2 and x["reviews"].get("0", {}).get("task_id") else None)
        rid = second["reviews"]["0"]["task_id"]
        sent = [b for path, b in FakeRunner.calls if path == "/jobs" and b["id"] == rid][0]["prompt"]
        self.assertIn("Greeting regression", sent)            # previous findings
        self.assertIn("fixed greeting changed", sent)         # the delta
        self.assertIn("Changed since the last review:\n- app.py", sent)
        self.assertNotIn("+hello", sent)                      # not the full diff again
        task = tasks.get_task(rid)
        self.assertEqual((task["kind"], task["effort"]), ("re_review", "low"))
        self.c.post(f"/api/pipelines/{p['id']}/cancel", headers=W)

    def test_codex_delta_rounds_are_re_reviews_and_security_paths_are_kept_separately(self):
        self.c.put("/api/budget/rules", json={"body": rules(reviews__max_codex_rounds=2, reviews__max_paid_rounds=3)},
                   headers=W)
        FakeRunner.diff_text = section("nodes/runner.py", "x")
        FakeRunner.delta_text = section("nodes/runner.py", "fixed")
        try:
            p = self.start(builder={"agent": "claude", "model": "claude-sonnet-5-5"},
                           reviewers=[{"kind": "agent", "agent": "codex"}])
            self.finish_build(p)
            first = self.review_task(p)
            self.assertEqual(tasks.get_task(first)["kind"], "security_review")
            issue = dict(self.ISSUE, file="nodes/runner.py", evidence="x")  # supported by the diff above
            self.answer(first, "request_changes", [issue])
            self.until(p, lambda x: x if x["state"] == "building" and x["round"] == 2 else None)
            wait_for(lambda: any(path == f"/jobs/{p['build_task']}/message" for path, _ in FakeRunner.calls))
            self.finish_build(p)
            second = self.until(p, lambda x: x if x["round"] == 2 and x["reviews"].get("0", {}).get("task_id") else None)
            task = tasks.get_task(second["reviews"]["0"]["task_id"])
            self.assertEqual(task["kind"], "re_review")
            self.assertEqual(task["agent"], "codex")
            self.assertEqual(second["budget"]["security_review_rounds"], [1, 2])
            self.c.post(f"/api/pipelines/{p['id']}/cancel", headers=W)
        finally:
            FakeRunner.diff_text = FakeRunner.delta_text = None

    def test_untrusted_role_findings_never_reach_the_round_two_paid_prompt(self):
        untrusted = dict(self.ISSUE, problem="Untrusted role finding", evidence="hello")
        self.local_answers = [json.dumps({"verdict": "request_changes", "issues": [untrusted]})] * 2
        p = self.start(reviewers=[{"kind": "role", "role": "reviewer"},
                                  {"kind": "agent", "agent": "claude", "model": "claude-fable-5-2"}])
        self.finish_build(p)
        current = self.until(p, lambda x: x if x["reviews"].get("1", {}).get("task_id") else None)
        self.answer(current["reviews"]["1"]["task_id"], "request_changes", [self.ISSUE])
        FakeRunner.delta_text = section("app.py", "fixed greeting")
        self.until(p, lambda x: x if x["state"] == "building" and x["round"] == 2 else None)
        wait_for(lambda: any(path == f"/jobs/{p['build_task']}/message" for path, _ in FakeRunner.calls))
        self.finish_build(p)
        second = self.until(p, lambda x: x if x["round"] == 2 and x["reviews"].get("1", {}).get("task_id") else None)
        sent = [b for path, b in FakeRunner.calls if path == "/jobs" and b["id"] == second["reviews"]["1"]["task_id"]][0]
        self.assertIn("Greeting regression", sent["prompt"])          # the paid reviewer's own finding
        self.assertNotIn("Untrusted role finding", sent["prompt"])    # never the untrusted role's (AC19)
        self.c.post(f"/api/pipelines/{p['id']}/cancel", headers=W)

    def test_a_refused_codex_review_does_not_use_up_the_round(self):
        self.c.put("/api/budget/rules", json={"body": rules(models__codex__kinds=["build"])}, headers=W)
        FakeRunner.diff_text = section("nodes/runner.py", "x")
        try:
            p = self.start(builder={"agent": "claude", "model": "claude-sonnet-5-5"},
                           reviewers=[{"kind": "agent", "agent": "codex"}])
            self.finish_build(p)
            done = self.until(p, lambda x: x if x["state"] in ("escalated", "failed", "ready") else None)
            self.assertTrue(any("codex task not started" in (h.get("note") or "") for h in done["history"]), done["history"])
            self.assertEqual(done["budget"].get("codex_rounds"), [])
            self.assertEqual(done["budget"].get("paid_rounds"), [])
        finally:
            FakeRunner.diff_text = None

    def test_cancel_reaches_every_split_review_chunk(self):
        self.c.put("/api/budget/rules", json={"body": rules(prompt__max_kb=1)}, headers=W)
        FakeRunner.diff_text = "".join(section(f"src/file{i}.py", "line " * 100) for i in range(5))
        try:
            p = self.start()
            self.finish_build(p)
            current = self.until(p, lambda x: x if x["reviews"].get("0", {}).get("task_ids") else None)
            ids = current["reviews"]["0"]["task_ids"]
            self.assertGreater(len(ids), 1)
            self.c.post(f"/api/pipelines/{p['id']}/cancel", headers=W)
            self.assertEqual([tasks.get_task(i)["state"] for i in ids], ["cancelled"] * len(ids))
        finally:
            FakeRunner.diff_text = None

    def test_empty_delta_starts_no_paid_review(self):
        FakeRunner.delta_text = "  \n"
        p = self.start()
        self.finish_build(p)
        first = self.review_task(p)
        self.answer(first, "request_changes", [self.ISSUE])
        self.until(p, lambda x: x if x["state"] == "building" and x["round"] == 2 else None)
        wait_for(lambda: any(path == f"/jobs/{p['build_task']}/message" for path, _ in FakeRunner.calls))
        self.finish_build(p)
        done = self.until(p, lambda x: x if x["state"] in ("escalated", "ready") else None)
        self.assertEqual(done["state"], "escalated")
        self.assertTrue(any("no change since last review" in (h.get("note") or "") for h in done["history"]))
        reviews = [t for t in self.c.get(f"/api/tasks?parent_id={p['build_task']}").json() if t["link"] == "review"]
        self.assertEqual(len(reviews), 1)

    def test_paid_review_rounds_are_capped(self):
        self.c.put("/api/budget/rules", json={"body": rules(reviews__max_paid_rounds=1)}, headers=W)
        FakeRunner.delta_text = section("app.py", "changed again")
        p = self.start()
        self.finish_build(p)
        self.answer(self.review_task(p), "request_changes", [self.ISSUE])
        self.until(p, lambda x: x if x["state"] == "building" and x["round"] == 2 else None)
        wait_for(lambda: any(path == f"/jobs/{p['build_task']}/message" for path, _ in FakeRunner.calls))
        self.finish_build(p)
        done = self.until(p, lambda x: x if x["state"] == "escalated" else None)
        self.assertIn("paid review rounds exhausted", done["history"][-1]["note"])

    def test_codex_reviews_only_security_paths(self):
        FakeRunner.diff_text = section("nodes/runner.py", "x")
        try:
            p = self.start(builder={"agent": "claude", "model": "claude-sonnet-5-5"},
                           reviewers=[{"kind": "agent", "agent": "codex"}])
            self.finish_build(p)
            rid = self.review_task(p)
            self.assertEqual(tasks.get_task(rid)["kind"], "security_review")
            self.c.post(f"/api/pipelines/{p['id']}/cancel", headers=W)
        finally:
            FakeRunner.diff_text = None

    def test_untrusted_local_findings_are_recorded_not_sent(self):
        local_issue = dict(self.ISSUE)
        self.local_answers = [json.dumps({"verdict": "request_changes", "issues": [local_issue]})]
        p = self.start()
        self.finish_build(p)
        rid = self.review_task(p)
        prompt = [b for path, b in FakeRunner.calls if path == "/jobs" and b["id"] == rid][0]["prompt"]
        self.assertNotIn("Greeting regression", prompt)   # never anchors the paid reviewer
        self.answer(rid, "request_changes", [self.ISSUE])
        self.until(p, lambda x: x if x["round"] == 2 else None)
        with db() as c:
            rows = [dict(r) for r in c.execute("SELECT * FROM review_agreement WHERE pipeline_id=?", (p["id"],))]
        self.assertEqual([(r["hint_id"], bool(r["confirmed"]), bool(r["trusted"])) for r in rows], [(0, True, False)])
        self.c.post(f"/api/pipelines/{p['id']}/cancel", headers=W)

    def test_trusted_local_hints_reach_the_paid_prompt_and_misses_are_recorded(self):
        controller.auto_trusted_roles = lambda: ["reviewer"]
        low = dict(self.ISSUE, severity="low", problem="Greeting wording")
        self.local_answers = [json.dumps({"verdict": "approve", "issues": [low]})]
        p = self.start()
        self.finish_build(p)
        rid = self.review_task(p)
        prompt = [b for path, b in FakeRunner.calls if path == "/jobs" and b["id"] == rid][0]["prompt"]
        self.assertIn("Greeting wording", prompt)
        self.assertIn("confirm or reject", prompt)
        other = dict(self.ISSUE, file="other.py", evidence="hello", problem="Missing test")
        self.answer(rid, "request_changes", [other])
        self.until(p, lambda x: x if x["round"] == 2 else None)
        with db() as c:
            rows = [dict(r) for r in c.execute("SELECT * FROM review_agreement WHERE pipeline_id=? ORDER BY id", (p["id"],))]
        self.assertEqual([(r["hint_id"], bool(r["confirmed"])) for r in rows], [(0, False), (None, False)])
        self.c.post(f"/api/pipelines/{p['id']}/cancel", headers=W)

    def test_trusted_local_request_changes_sends_back_before_any_paid_review(self):
        controller.auto_trusted_roles = lambda: ["reviewer"]
        self.local_answers = [json.dumps({"verdict": "request_changes", "issues": [self.ISSUE]})]
        p = self.start()
        self.finish_build(p)
        back = self.until(p, lambda x: x if x["state"] == "building" and x["round"] == 2 else None)
        self.assertIn("local reviewer", back["history"][-1]["note"])
        self.assertEqual([t for t in self.c.get(f"/api/tasks?parent_id={p['build_task']}").json()
                          if t["link"] == "review"], [])
        self.assertEqual(back["budget"]["paid_rounds_avoided"], 1)
        self.c.post(f"/api/pipelines/{p['id']}/cancel", headers=W)

    def test_untrusted_local_never_blocks_or_skips(self):
        self.c.put("/api/budget/rules", json={"body": rules(reviews__paid_skip_when={
            "max_changed_lines": 40, "paths_only": ["**"]})}, headers=W)
        self.local_answers = [json.dumps({"verdict": "request_changes", "issues": [self.ISSUE]})]
        p = self.start()
        self.finish_build(p)
        self.assertTrue(self.review_task(p))  # a paid review still ran
        self.c.post(f"/api/pipelines/{p['id']}/cancel", headers=W)

    def test_paid_skip_rule(self):
        controller.auto_trusted_roles = lambda: ["reviewer"]
        p = self.start()  # skip rule unset: a paid review always runs
        self.finish_build(p)
        self.assertTrue(self.review_task(p))
        self.c.post(f"/api/pipelines/{p['id']}/cancel", headers=W)
        self.c.put("/api/budget/rules", json={"body": rules(reviews__paid_skip_when={
            "max_changed_lines": 40, "paths_only": ["**"]})}, headers=W)
        p = self.start(title="Skippable")
        self.finish_build(p)
        done = self.until(p, lambda x: x if x["state"] in ("ready", "escalated") or x["reviews"].get("0", {}).get("task_id") else None)
        self.assertEqual(done["state"], "ready")
        self.assertEqual(done["history"][-1]["note"], "paid review skipped by budget rule")
        FakeRunner.diff_text = section("master/controller/auth.py", "hello")
        try:
            p = self.start(title="Security path")
            self.finish_build(p)
            self.assertTrue(self.review_task(p))
            self.c.post(f"/api/pipelines/{p['id']}/cancel", headers=W)
        finally:
            FakeRunner.diff_text = None

    def test_savings_and_local_reviewers_endpoints(self):
        savings = self.c.get("/api/budget/savings").json()
        for key in ("local_tokens", "paid_tokens", "local_reviews", "paid_rounds_avoided", "paid_reviews_skipped",
                    "prompt_bytes_stripped", "delta_reviews", "delta_bytes_saved"):
            self.assertIn(key, savings)
        self.assertIsInstance(self.c.get("/api/budget/local-reviewers").json(), list)


class StaticChecks(unittest.TestCase):
    def test_setup_never_reads_or_copies_codex_auth(self):
        text = (ROOT / "master" / "setup.sh").read_text()
        code = [line for line in text.splitlines() if not line.lstrip().startswith("#")]
        self.assertFalse([line for line in code if "auth.json" in line])
        self.assertIn(".codex-fleet", text)
        self.assertTrue(re.search(r'chmod 700 "\$HOME/\.codex-fleet"', text))
        self.assertIn('model_reasoning_effort = "medium"', text)

    def test_gate_runs_the_budget_tests(self):
        gate = (ROOT / "scripts" / "gate.sh").read_text()
        self.assertIn("tests/test_budget.py", gate)
        self.assertIn("tests/test_runner_budget.py", gate)

    def test_claude_md_points_to_the_budget_page(self):
        text = (ROOT / "CLAUDE.md").read_text()
        self.assertIn("Budget page", text)


if __name__ == "__main__":
    unittest.main()
