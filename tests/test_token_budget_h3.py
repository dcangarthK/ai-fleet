"""Part H batch 3: review input bounds (req 8), idle burners (req 9), telemetry (req 10).

Run alone: python3 -m unittest tests.test_token_budget_h3
"""
import importlib.util
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]
WORKER = ROOT / "nodes" / "worker"
CTRL = ROOT / "master" / "controller"


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def diff_for(name, lines):
    body = "\n".join(f"+line {i}" for i in range(lines))
    return f"diff --git a/{name} b/{name}\n--- a/{name}\n+++ b/{name}\n@@ -0,0 +1,{lines} @@\n{body}\n"


class ReviewPromptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # controller.py needs fastapi/httpx; the prompt builders are pure, so run just their source slice.
        src = (CTRL / "controller.py").read_text()
        start = src.index("# One instruction for every paid review")
        end = src.index("_pipeline_locks: dict")
        sys.path.insert(0, str(CTRL))
        import budget as real_budget
        ns = {"budget": real_budget,
              "grounding": types.SimpleNamespace(REVIEW_GUIDANCE="GUIDE.", REVIEW_SCHEMA="{schema}")}
        exec(compile(src[start:end], "controller_prompts", "exec"), ns)
        cls.c = types.SimpleNamespace(**{k: ns[k] for k in ("review_prompt", "re_review_prompt")})

    def test_small_diff_inlined_without_file_list(self):
        diff = diff_for("a.py", 3)
        text = self.c.review_prompt("SPEC BODY", diff, hints=None, worktree="/w")
        self.assertIn("DIFF:\n", text)
        self.assertIn("+line 1", text)
        self.assertNotIn("Worktree:", text)
        self.assertIn("Read only the changed files and their direct callers", text)

    def test_large_diff_becomes_file_list_and_worktree_not_both(self):
        diff = diff_for("big.py", 6000) + diff_for("other.py", 2)
        self.assertGreater(len(diff.encode()), 30 * 1024)
        text = self.c.review_prompt("SPEC BODY", diff, worktree="/w/t-1")
        self.assertNotIn("+line 5999", text)
        self.assertIn("Worktree: /w/t-1", text)
        self.assertIn("- big.py", text)
        self.assertIn("- other.py", text)
        self.assertLess(len(text.encode()), 60 * 1024)

    def test_spec_sections_replace_spec_text(self):
        text = self.c.review_prompt("LONG SPEC TEXT", diff_for("a.py", 1), spec_sections=["3.8", "6"],
                                    spec_path="docs/SPEC-token-budget.md")
        self.assertIn("sections 3.8, 6 in docs/SPEC-token-budget.md", text)
        self.assertNotIn("LONG SPEC TEXT", text)

    def test_re_review_carries_findings_and_bounded_delta(self):
        findings = [{"severity": "high", "problem": "uses eval", "file": "a.py"}]
        text = self.c.re_review_prompt("SPEC", findings, diff_for("a.py", 5000), worktree="/w")
        self.assertIn("PREVIOUS FINDINGS", text)
        self.assertIn("uses eval", text)
        self.assertIn("Worktree: /w", text)
        self.assertNotIn("+line 4999", text)

    def test_pipeline_validates_spec_sections(self):
        sys.path.insert(0, str(CTRL))
        import pipeline as machine
        base = {"title": "t", "spec": "s", "builder": {"agent": "claude"},
                "reviewers": [{"kind": "agent", "agent": "codex"}]}
        ok = machine.new(base | {"spec_sections": [" 3.8 "], "spec_path": "docs/S.md"})
        self.assertEqual((ok["spec_sections"], ok["spec_path"]), (["3.8"], "docs/S.md"))
        with self.assertRaises(ValueError):
            machine.new(base | {"spec_sections": ["", "x"]})
        self.assertIsNone(machine.new(base)["spec_sections"])


class McpPassthroughTests(unittest.TestCase):
    def test_run_pipeline_forwards_spec_sections_and_path(self):
        # master/mcp/server.py needs the mcp package; check the tool's source with ast instead.
        import ast
        tree = ast.parse((ROOT / "master" / "mcp" / "server.py").read_text())
        fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "run_pipeline")
        params = [a.arg for a in fn.args.args]
        self.assertIn("spec_sections", params)
        self.assertIn("spec_path", params)
        keys = {n.slice.value for n in ast.walk(fn) if isinstance(n, ast.Subscript)
                and isinstance(n.value, ast.Name) and n.value.id == "body" and isinstance(n.slice, ast.Constant)}
        self.assertEqual(keys, {"spec_sections", "spec_path"})


class IdleBurnerTests(unittest.TestCase):
    def test_every_worker_settings_file_turns_idle_features_off(self):
        files = sorted(WORKER.glob("settings-*.json"))
        self.assertTrue(files)
        for path in files:
            data = json.loads(path.read_text())
            self.assertIs(data.get("promptSuggestionEnabled"), False, path.name)
            self.assertEqual(data["env"]["CLAUDE_CODE_GOAL_CHECKIN_MINUTES"], "0", path.name)
            for key in ("autoDreamEnabled", "awaySummaryEnabled", "spinnerTipsEnabled", "autoMemoryEnabled"):
                self.assertIs(data.get(key), False, f"{path.name} {key}")

    def test_owner_profile_untouched_and_hold_documented_as_unverified(self):
        self.assertIn("could not be", (WORKER / "README.md").read_text())


class TelemetryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        import os
        self.prev = os.environ.get("FLEET_JOBS_DIR")
        os.environ["FLEET_JOBS_DIR"] = self.temp.name

        def restore():
            if self.prev is None:
                os.environ.pop("FLEET_JOBS_DIR", None)
            else:
                os.environ["FLEET_JOBS_DIR"] = self.prev
        self.addCleanup(restore)
        try:
            self.tokens = load("tokens_h3", CTRL / "tokens.py")
        except Exception as exc:
            raise unittest.SkipTest(f"fastapi unavailable: {exc!r}")
        self.kinds = {}
        self.models = {}
        self.tokens._kind = lambda task_id: self.kinds.get(task_id)
        self.tokens._model = lambda task_id: self.models.get(task_id)

    def write(self, task_id, usages, kind, model):
        folder = Path(self.temp.name) / task_id
        folder.mkdir()
        lines = []
        for i, (inp, read, write, out) in enumerate(usages):
            lines.append(json.dumps({"type": "assistant", "message": {
                "id": f"m{i}", "usage": {"input_tokens": inp, "cache_read_input_tokens": read,
                                         "cache_creation_input_tokens": write, "output_tokens": out}, "content": []}}))
        (folder / "events.jsonl").write_text("\n".join(lines))
        self.kinds[task_id] = kind
        self.models[task_id] = model

    def test_row_has_caps_usage_and_alerts(self):
        # review caps: 30 turns / 100k context; turn 1 is 9k (> 8k target), peak 85k (> 80%).
        self.write("t-aa0001", [(9_000, 0, 0, 5), (85_000, 0, 0, 5)], "review", "sonnet")
        row = self.tokens.task_tokens("t-aa0001")
        self.assertEqual((row["turns"], row["peak_context"], row["turn1_context"]), (2, 85_000, 9_000))
        self.assertEqual(row["caps"]["context"], 100_000)
        self.assertEqual(row["usage"]["context"], 0.85)
        self.assertIn("context_80_percent", row["alerts"])
        self.assertIn("turn1_context", row["alerts"])
        self.assertEqual(row["model"], "sonnet")

    def test_lean_task_has_no_alerts(self):
        self.write("t-aa0002", [(5_000, 0, 0, 5)], "build", "sonnet")
        row = self.tokens.task_tokens("t-aa0002")
        self.assertEqual(row["alerts"], [])
        self.assertEqual(row["usage"]["turn1"], 0.625)

    def test_turn_cap_alert_at_80_percent(self):
        self.write("t-aa0003", [(1_000, 0, 0, 1)] * 12, "re_review", "opus")  # cap 15 turns -> 12 = 80%
        self.assertIn("turns_80_percent", self.tokens.task_tokens("t-aa0003")["alerts"])

    def test_rows_ordered_by_mtime_and_limit_keeps_newest(self):
        import os
        # Ids sort opposite to age: the sort must follow mtime, not the name.
        for task_id, stamp in (("t-ff0001", 100), ("t-aa0010", 300), ("t-bb0011", 200)):
            self.write(task_id, [(1, 0, 0, 1)], "build", "sonnet")
            os.utime(Path(self.temp.name) / task_id / "events.jsonl", (stamp, stamp))
        ids = [r["task_id"] for r in self.tokens.tokens_since(None, None)["tasks"]]
        self.assertEqual(ids, ["t-ff0001", "t-bb0011", "t-aa0010"])
        newest = [r["task_id"] for r in self.tokens.tokens_since(None, 2)["tasks"]]
        self.assertEqual(newest, ["t-bb0011", "t-aa0010"])
        since = [r["task_id"] for r in self.tokens.tokens_since(150, None)["tasks"]]
        self.assertEqual(since, ["t-bb0011", "t-aa0010"])

    def test_summary_defaults_to_last_24h(self):
        import os
        self.write("t-aa0020", [(1, 0, 0, 1)], "build", "sonnet")
        self.write("t-aa0021", [(1, 0, 0, 1)], "build", "sonnet")
        os.utime(Path(self.temp.name) / "t-aa0021" / "events.jsonl", (1, 1))
        self.assertEqual(self.tokens.tokens_summary(None)["by_kind"]["build"]["tasks"], 1)

    def test_kind_and_model_come_from_one_query(self):
        import contextlib
        import sqlite3
        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row
        db.execute("CREATE TABLE tasks (id TEXT, kind TEXT, model TEXT)")
        db.executemany("INSERT INTO tasks VALUES (?,?,?)",
                       [("t-aa0030", "review", "opus"), ("t-aa0031", "build", None)])
        statements = []
        db.set_trace_callback(statements.append)

        @contextlib.contextmanager
        def fake_db():
            yield db
        store = types.ModuleType("store")
        store.db = fake_db
        previous = sys.modules.get("store")
        sys.modules["store"] = store
        self.addCleanup(lambda: sys.modules.__setitem__("store", previous) if previous else sys.modules.pop("store"))
        fresh = load("tokens_h3_real", CTRL / "tokens.py")  # unpatched _kind/_model
        for task_id in ("t-aa0030", "t-aa0031", "t-aa0032"):
            self.write(task_id, [(1, 0, 0, 1)], "x", "x")
        rows = {r["task_id"]: r for r in fresh.tokens_since(None, None)["tasks"]}
        selects = [s for s in statements if s.startswith("SELECT")]
        self.assertEqual(len(selects), 1, selects)
        self.assertEqual((rows["t-aa0030"]["kind"], rows["t-aa0030"]["model"]), ("review", "opus"))
        self.assertEqual(rows["t-aa0031"]["kind"], "build")
        self.assertNotIn("model", rows["t-aa0031"])
        self.assertNotIn("kind", rows["t-aa0032"])

    def test_row_reports_input_and_output(self):
        self.write("t-aa0040", [(7, 0, 0, 3), (9, 0, 0, 4)], "build", "sonnet")
        row = self.tokens.task_tokens("t-aa0040")
        self.assertEqual((row["input"], row["output"]), (16, 7))

    def test_spend_summary_per_kind_and_model(self):
        self.write("t-aa0004", [(100, 200, 300, 10)], "build", "sonnet")
        self.write("t-aa0005", [(10, 20, 30, 1)], "review", "sonnet")
        self.write("t-aa0006", [(1, 2, 3, 1)], "build", "opus")
        summary = self.tokens.tokens_summary(None)
        self.assertEqual(summary["by_kind"]["build"]["tasks"], 2)
        self.assertEqual(summary["by_kind"]["build"]["input"], 600 + 6)
        self.assertEqual(summary["by_model"]["sonnet"]["input"], 600 + 60)
        self.assertEqual(summary["by_model"]["opus"]["cache_write"], 3)
        self.assertEqual(summary["by_kind"]["review"]["output"], 1)


if __name__ == "__main__":
    unittest.main()
