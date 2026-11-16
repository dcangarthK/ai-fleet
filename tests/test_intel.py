"""Part I intelligent controller: docs/SPEC-intelligent-controller.md sections 4, 7, 8 (criteria 1-12).
Run: uv run -q --with fastapi --with uvicorn --with httpx --with 'mcp<2' python -m unittest tests/test_intel.py
Expected to fail with ModuleNotFoundError until master/controller/intel.py and intel_routes.py exist.
Assumptions (spec ambiguous): rank() stats/load/headroom are dicts keyed by candidate key (stats by
(kind, key)); headroom[key] = dict(used_percent, limited_until, routed); load[key] = running task count.
"""
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="intel-test-"))
os.environ.setdefault("FLEET_DB", str(TMP / "fleet.db"))
os.environ.setdefault("FLEET_PLUGINS_DIR", str(TMP / "no-plugins"))
(TMP / "no-plugins").mkdir(exist_ok=True)
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "master" / "controller"))

import intel  # noqa: E402  (does not exist yet: ImportError is the expected failure)

FABLE = "claude:claude-fable-5-2"
OPUS = "claude:claude-opus-5-5"
SONNET = "claude:claude-sonnet-5-5"
HAIKU = "claude:claude-haiku-5-5"
ALL = ["*"]


def mapping(*pairs):
    return dict(pairs)


RULES = dict(
    models=mapping(
        ("codex", dict(tier="scarce", max_concurrent=1, kinds=["final_review", "security_review", "build", "lead"])),
        ("claude:claude-opus-*", dict(tier="premium", max_concurrent=1,
                                      kinds=["build", "final_review", "security_review", "review", "test", "lead"])),
        ("claude:claude-fable-*", dict(tier="premium", max_concurrent=2,
                                       kinds=["review", "re_review", "final_review", "security_review", "build"])),
        ("claude:claude-sonnet-*", dict(tier="standard", max_concurrent=2, kinds=ALL)),
        ("claude:claude-haiku-*", dict(tier="cheap", max_concurrent=2, kinds=ALL)),
        ("local:*", dict(tier="cheap", max_concurrent=1, kinds=ALL)),
    ),
    limits=dict(near_limit_percent=85),
)


def local_up():
    return mapping(("local:fast", dict(routed=True, used_percent=0)),
                   ("local:coder", dict(routed=True, used_percent=0)))


def pick(cands, key):
    return next(c for c in cands if c["key"] == key)


def eligible_keys(cands):
    return [c["key"] for c in cands if c["eligible"]]


def rank(kind, **kw):
    kw.setdefault("rules", RULES)
    kw.setdefault("stats", dict())
    kw.setdefault("headroom", local_up())
    kw.setdefault("load", dict())
    return intel.rank(kind, **kw)


class RankTests(unittest.TestCase):
    def test_1_final_review_prefers_codex_then_fable_opus(self):  # criterion 1
        head = local_up()
        head.update(mapping(("codex", dict(used_percent=5)), (SONNET, dict(used_percent=100)),
                            (OPUS, dict(used_percent=100)), (FABLE, dict(used_percent=100))))
        cands = rank("final_review", headroom=head)
        self.assertEqual(cands[0]["key"], "codex")
        self.assertTrue(cands[0]["eligible"])
        self.assertTrue(any("preferred for final_review" in r for r in cands[0]["reasons"]))
        head2 = local_up()
        head2["codex"] = dict(used_percent=5)
        order = eligible_keys(rank("final_review", headroom=head2))
        self.assertEqual(order[0], "codex")
        self.assertIn(OPUS, order)
        self.assertIn(FABLE, order)

    def test_2_draft_prefers_local_then_haiku_over_sonnet(self):  # criterion 2
        cands = rank("draft")
        self.assertIn(cands[0]["key"], ("local:coder", "local:fast"))
        down = mapping(("local:fast", dict(routed=False)), ("local:coder", dict(routed=False)))
        cands = rank("draft", headroom=down)
        self.assertEqual(cands[0]["key"], HAIKU)
        order = eligible_keys(cands)
        self.assertLess(order.index(HAIKU), order.index(SONNET))
        self.assertFalse(pick(cands, "local:coder")["eligible"])

    def test_3_build_never_fable_or_forbidden_pairs(self):  # criterion 3
        cands = rank("build")
        fable = pick(cands, FABLE)
        self.assertFalse(fable["eligible"])
        self.assertTrue(any("rule 0" in r for r in fable["reasons"]))
        self.assertEqual(cands[0]["key"], SONNET)
        for kind in ("merge", "benchmark", "check", "draft", "re_review", "test"):
            self.assertFalse(pick(rank(kind), "codex")["eligible"], kind)
        for kind in ("build", "fix", "test", "merge", "benchmark", "draft", "check"):
            self.assertFalse(pick(rank(kind), FABLE)["eligible"], kind)
        flags = [c["eligible"] for c in cands]  # ineligible are kept, sorted last
        self.assertEqual(flags, sorted(flags, reverse=True))

    def test_4_near_limit_names_both_numbers(self):  # criterion 4
        head = local_up()
        head[SONNET] = dict(used_percent=90)
        c = pick(rank("build", headroom=head), SONNET)
        self.assertFalse(c["eligible"])
        text = " ".join(c["reasons"])
        self.assertIn("90", text)
        self.assertIn("85", text)

    def test_4b_limited_until_future_is_ineligible(self):
        head = local_up()
        head[SONNET] = dict(used_percent=0, limited_until=time.time() + 3600)
        self.assertFalse(pick(rank("build", headroom=head), SONNET)["eligible"])

    def test_5_review_excludes_builder(self):  # criterion 5
        cands = rank("review", exclude=[SONNET])
        self.assertFalse(pick(cands, SONNET)["eligible"])
        self.assertNotIn(SONNET, eligible_keys(cands))
        self.assertTrue(eligible_keys(cands))

    def test_candidate_shape(self):
        c = rank("build")[0]
        for k in ("key", "agent", "model", "score", "eligible", "est_tokens", "est_usd", "reasons"):
            self.assertIn(k, c)

    def test_success_history_moves_score(self):
        good = rank("build", stats=mapping((("build", SONNET), dict(done=20, bad=0))))
        bad = rank("build", stats=mapping((("build", SONNET), dict(done=0, bad=20))))
        self.assertGreater(pick(good, SONNET)["score"], pick(bad, SONNET)["score"])


def row(kind, key, i, o, c, usd):
    return dict(kind=kind, key=key, input=i, output=o, cached=c, cost_usd=usd)


class EstimateTests(unittest.TestCase):
    HIST = [
        row("build", SONNET, 100, 50, 50, 1.0),
        row("build", SONNET, 300, 100, 100, 3.0),
        row("build", SONNET, 200, 100, 100, 2.0),
        row("review", SONNET, 10, 5, 5, 0.1),
        row("final_review", "codex", 1000, 500, 500, 9.0),
    ]

    def test_6_same_kind_median(self):  # criterion 6
        r = intel.estimate("build", SONNET, self.HIST)
        self.assertEqual(r["tokens"], 400)  # median of 200, 500, 400
        self.assertEqual(r["usd"], 2.0)
        self.assertEqual(r["samples"], 3)

    def test_6_prompt_bytes_add_quarter_tokens(self):
        self.assertEqual(intel.estimate("build", SONNET, self.HIST, prompt_bytes=400)["tokens"], 500)

    def test_6_falls_back_to_model_then_default(self):
        self.assertEqual(intel.estimate("fix", SONNET, self.HIST)["basis"], "model")
        r = intel.estimate("fix", HAIKU, self.HIST)
        self.assertEqual(r["basis"], "default")
        self.assertIsNone(r["usd"])
        self.assertGreater(r["tokens"], 0)

    def test_6_codex_usd_always_none(self):
        r = intel.estimate("final_review", "codex", self.HIST)
        self.assertIsNone(r["usd"])
        self.assertEqual(r["tokens"], 2000)


class ForecastTests(unittest.TestCase):
    NOW = 1_800_000_000.0

    def acct(self, used, rate_per_h=0.0, resets_in_h=4.0, week=10):
        samples = [(self.NOW - 3600, used - rate_per_h), (self.NOW, used)]
        return dict(used_5h=used, resets_5h=self.NOW + resets_in_h * 3600, used_week=week,
                    resets_week=self.NOW + 86400, samples=samples)

    def test_7_at_risk_when_projection_crosses_85(self):  # criterion 7
        out = intel.forecast(mapping(("claude/fleet", self.acct(40, rate_per_h=20))), self.NOW)["claude/fleet"]
        self.assertEqual(out["status"], "at_risk")
        self.assertEqual(out["projected_5h"], 100)  # capped
        self.assertIsNotNone(out["exhausts_at"])
        self.assertLess(out["exhausts_at"], self.NOW + 4 * 3600)
        self.assertTrue(out["advice"])

    def test_7_ok_and_limited(self):
        out = intel.forecast(dict(a=self.acct(20, rate_per_h=1), b=self.acct(100), c=self.acct(10, week=100)),
                             self.NOW)
        self.assertEqual(out["a"]["status"], "ok")
        self.assertEqual(out["b"]["status"], "limited")
        self.assertEqual(out["c"]["status"], "limited")

    def test_7_no_rate_with_one_sample(self):
        a = self.acct(30)
        a["samples"] = [(self.NOW, 30)]
        out = intel.forecast(dict(a=a), self.NOW)["a"]
        self.assertEqual(out["projected_5h"], 30)
        self.assertIsNone(out["exhausts_at"])
        self.assertEqual(out["status"], "ok")


class SuggestTests(unittest.TestCase):
    def test_8_codex_re_review_suggests_claude_first(self):  # criterion 8
        s = intel.suggest("re_review", "codex", RULES, local_up(), dict())
        self.assertTrue(1 <= len(s) <= 3)
        self.assertEqual(s[0]["agent"], "claude")
        self.assertEqual(s[0]["kind"], "re_review")
        for k in ("agent", "model", "kind", "why"):
            self.assertIn(k, s[0])
        self.assertFalse(any(x["agent"] == "codex" for x in s))

    def test_8_security_adds_codex_security_review(self):
        s = intel.suggest("re_review", "codex", RULES, local_up(), dict(), security=True)
        sec = [x for x in s if x["agent"] == "codex"]
        self.assertEqual([x["kind"] for x in sec], ["security_review"])


class CleanupTests(unittest.TestCase):
    def test_10_only_terminal_master_merged(self):  # criterion 10
        def t(id, state="done", node="master", branch=None, wt="/w"):
            return dict(id=id, state=state, branch=branch or "fleet/" + id, repo="/r", node_id=node, worktree=wt)
        rows = [t("ok"), t("failed", state="failed"), t("cancelled", state="cancelled"),
                t("working", state="working"), t("remote", node="n2"), t("unmerged", branch="fleet/unmerged"),
                t("nowt", wt=None)]
        got = intel.cleanup_candidates(rows, lambda repo, branch: branch != "fleet/unmerged")
        self.assertEqual(sorted(r["id"] for r in got), ["cancelled", "failed", "ok"])


class SavingsTests(unittest.TestCase):
    def test_11_max_cost_per_task(self):  # criterion 11
        from store import db, init_db
        init_db()
        now = time.time()
        with db() as c:
            intel.ensure_schema(c)
            c.execute("INSERT INTO tasks (id, agent, kind, state, created_at) VALUES ('sv1','claude','build','done',?)",
                      (now,))
            for cost in (0.8, 1.0, 2.4):
                c.execute("INSERT INTO agent_usage (task_id, agent, input, output, cached, cost_usd, ts) "
                          "VALUES ('sv1','claude',10,5,0,?,?)", (cost, now))
            out = intel.savings(c, now - 60, now - 3600, now - 1800)
        cur = out.get("current", out)  # assumption: per-window dict under "current", or flat
        self.assertAlmostEqual(cur["usd"], 2.4)  # not 4.2


class RouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient
        import controller
        import intel_routes  # noqa: F401  (expected ImportError for now)
        cls.ctx = TestClient(controller.app, base_url="http://127.0.0.1")
        cls.c = cls.ctx.__enter__()
        cls.H = dict(**mapping(("X-Fleet", "1")))

    @classmethod
    def tearDownClass(cls):
        cls.ctx.__exit__(None, None, None)

    def test_12_all_routes_200_on_empty_db(self):  # criterion 12
        for path in ("/api/intel/decisions", "/api/intel/template?kind=build&security=0", "/api/intel/cleanup",
                     "/api/intel/savings", "/api/intel/forecast", "/api/intel/tasks/nope"):
            r = self.c.get(path, headers=self.H)
            self.assertEqual(r.status_code, 200, path + ": " + r.text)
        r = self.c.post("/api/intel/estimate", json=dict(kind="build"), headers=self.H)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn("candidates", r.json())

    def test_decisions_round_trip(self):
        from store import db
        with db() as conn:
            intel.ensure_schema(conn)
            intel.record(conn, task_id="rt1", kind="build", action="suggest", chosen=SONNET,
                         candidates=[dict(key=SONNET)], reasons=["because"], est_tokens=10, est_usd=0.5)
        rows = self.c.get("/api/intel/decisions?task_id=rt1&limit=5", headers=self.H).json()
        rows = rows if isinstance(rows, list) else rows.get("decisions", [])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["candidates"], [dict(key=SONNET)])
        self.assertEqual(rows[0]["reasons"], ["because"])
        per = self.c.get("/api/intel/tasks/rt1", headers=self.H).json()
        per = per if isinstance(per, list) else per.get("decisions", [])
        self.assertEqual(len(per), 1)

    def test_9_auto_model_fills_concrete_and_records_one_auto_pick(self):  # criterion 9
        from store import db
        body = dict(agent="claude", model="auto", kind="build", prompt="x")
        r = self.c.post("/api/tasks", json=body, headers=self.H)
        self.assertLess(r.status_code, 500, r.text)  # admission may still queue or refuse (spec R6)
        with db() as conn:
            rows = conn.execute("SELECT chosen FROM intel_decisions WHERE action='auto_pick'").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertNotEqual(rows[0][0], "auto")
        if r.status_code == 200:
            self.assertNotEqual(r.json().get("model"), "auto")

    def test_9_auto_requires_kind(self):
        body = dict(agent="claude", model="auto", prompt="x")
        r = self.c.post("/api/tasks", json=body, headers=self.H)
        self.assertEqual(r.status_code, 400, r.text)


if __name__ == "__main__":
    unittest.main()


class ReviewFixTests(unittest.TestCase):
    def test_h1_codex_builder_with_model_excluded_from_review_pick(self):
        import intel_routes
        from store import db, init_db
        init_db()
        with db() as c:
            c.execute("INSERT INTO tasks (id, agent, model, kind, state, created_at) "
                      "VALUES ('h1b','codex','gpt-5.6-terra','build','done',?)", (time.time(),))
            self.assertEqual(intel_routes.exclusion(c, "final_review", "h1b"), ("codex",))
            self.assertEqual(intel_routes.exclusion(c, "build", "h1b"), ())
        out = intel.rank("final_review", rules=RULES, stats={}, headroom=local_up(), load={}, exclude=("codex",))
        self.assertFalse(pick(out, "codex")["eligible"])

    def test_h2_intel_view_uses_real_used_percent(self):
        import intel_routes
        import tasks
        orig = intel_routes.used_5h
        intel_routes.used_5h = lambda: {"claude": 92, "codex": 10}
        try:
            headroom, _ = tasks._intel_view(RULES)
        finally:
            intel_routes.used_5h = orig
        self.assertEqual(headroom[SONNET]["used_percent"], 92)
        out = intel.rank("build", rules=RULES, stats={}, headroom=headroom, load={})
        self.assertFalse(pick(out, SONNET)["eligible"])

    def test_h3_no_repo_or_no_commits_not_merged(self):
        import subprocess
        import intel_routes
        self.assertFalse(intel_routes._is_merged(None, "fleet/x"))
        self.assertFalse(intel_routes._is_merged("", "fleet/x"))
        repo = str(TMP / "h3repo")
        ident = ["-c", "user.name=t", "-c", "user.email=t@t"]
        run = lambda *a: subprocess.run(["git", "-C", repo, *a], check=True, capture_output=True)  # noqa: E731
        subprocess.run(["git", "init", "-q", "-b", "main", repo], check=True)
        run(*ident, "commit", "--allow-empty", "-m", "i")
        run("branch", "fleet/none")  # tip == main: never committed
        self.assertFalse(intel_routes._is_merged(repo, "fleet/none"))
        run(*ident, "commit", "--allow-empty", "-m", "m2")
        run("branch", "fleet/behind", "HEAD~1")  # real commits, tip strictly behind main: merged
        self.assertTrue(intel_routes._is_merged(repo, "fleet/behind"))
        run("checkout", "-q", "-b", "fleet/work")
        run(*ident, "commit", "--allow-empty", "-m", "w")
        run("checkout", "-q", "main")
        self.assertFalse(intel_routes._is_merged(repo, "fleet/work"))

    def test_m1_round_reviewer_first_and_security_only_for_re_review(self):
        s = intel.suggest("re_review", "codex", RULES, local_up(), {}, round_reviewer=SONNET)
        self.assertEqual((s[0]["agent"], s[0]["model"]), ("claude", "claude-sonnet-5-5"))
        keys = [(x["agent"], x["model"], x["kind"]) for x in s]
        self.assertEqual(len(keys), len(set(keys)))
        s = intel.suggest("review", SONNET, RULES, local_up(), {}, security=True)
        self.assertFalse(any(x["kind"] == "security_review" for x in s))

    def test_m3_stats_keyed_by_key_of(self):
        import intel_routes
        from store import db, init_db
        init_db()
        with db() as c:
            for i in range(2):
                c.execute("INSERT INTO tasks (id, agent, model, kind, state, created_at) "
                          "VALUES (?,'codex','gpt-5.6-terra','review','done',?)", ("m3" + str(i), time.time()))
            st = intel_routes.build_stats(c)
        self.assertEqual(st[("review", "codex")]["done"], 2)

    def test_l5_cleanup_run_continues_after_error(self):
        import asyncio
        import intel_routes
        import tasks
        calls = []
        orig_c, orig_f = intel_routes._cleanable, tasks.cleanup
        intel_routes._cleanable = lambda c: [dict(id="a", branch="b1"), dict(id="b", branch="b2")]

        async def fake(tid):
            calls.append(tid)
            if tid == "a":
                raise RuntimeError("boom")
        tasks.cleanup = fake
        try:
            out = asyncio.run(intel_routes.cleanup_run())
        finally:
            intel_routes._cleanable, tasks.cleanup = orig_c, orig_f
        self.assertEqual(calls, ["a", "b"])
        self.assertEqual(out["cleaned"], ["b"])
        self.assertEqual(out["failed"][0]["id"], "a")
