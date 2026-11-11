"""Phase 7 build pipeline state machine and evidence-checked reviews (docs/SPEC-phase7-skills-evals-pipelines.md §3).
Pure functions in master/controller/pipeline.py. Run: python3 -m unittest -v tests/test_pipeline.py
"""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "master" / "controller"))
import pipeline as PL  # noqa: E402

DIFF = """--- a/app.py
+++ b/app.py
@@ -1,3 +1,4 @@
 def get_user(db, name):
-    return db.execute("SELECT * FROM users WHERE name = ?", (name,))
+    return db.execute(f"SELECT * FROM users WHERE name = '{name}'")
"""


def spec(**kw):
    base = {"title": "t", "spec": "do it", "repo": "/r", "gate": "pytest", "mode": "auto-edit",
            "builder": {"agent": "codex"}, "reviewers": [{"kind": "role", "role": "reviewer"}, {"kind": "agent", "agent": "claude"}],
            "max_rounds": 2}
    base.update(kw)
    return base


def review(verdict, *issues):
    return json.dumps({"verdict": verdict, "issues": list(issues)})


SQLI = {"severity": "high", "file": "app.py", "problem": "SQL injection via f-string",
        "evidence": "f\"SELECT * FROM users WHERE name = '{name}'\"",
        "expected": "parameterized SQL", "actual": "interpolated SQL", "fix": "use a parameter"}
INVENTED = {"severity": "high", "file": "app.py", "problem": "missing auth check",
            "evidence": "if not user.is_admin:", "expected": "an auth check", "actual": "none",
            "fix": "add one"}
NIT = {"severity": "low", "file": "app.py", "problem": "naming", "evidence": "def get_user(db, name):",
       "expected": "a clearer name", "actual": "get_user", "fix": "rename"}


class ParseReview(unittest.TestCase):
    def test_valid_with_supported_evidence(self):
        r = PL.parse_review(review("request_changes", SQLI), DIFF)
        self.assertEqual(r["status"], "valid")
        self.assertEqual(r["verdict"], "request_changes")
        self.assertEqual(len(r["issues"]), 1)
        self.assertEqual(r["unsupported"], 0)

    def test_invented_evidence_is_dropped_and_counted(self):
        r = PL.parse_review(review("request_changes", SQLI, INVENTED), DIFF)
        self.assertEqual([i["problem"] for i in r["issues"]], ["SQL injection via f-string"])
        self.assertEqual(r["unsupported"], 1)

    def test_whitespace_differences_in_evidence_are_tolerated(self):
        loose = dict(SQLI, evidence="  f\"SELECT * FROM users WHERE name =   '{name}'\"  ")
        self.assertEqual(len(PL.parse_review(review("request_changes", loose), DIFF)["issues"]), 1)

    def test_invalid_json_or_schema_abstains(self):
        for raw in ("looks fine to me", "{}", '{"verdict":"maybe","issues":[]}', '{"verdict":"approve","issues":"none"}',
                    json.dumps({"verdict": "approve", "issues": [{"severity": "critical", "problem": "x", "evidence": "y"}]})):
            self.assertEqual(PL.parse_review(raw, DIFF)["status"], "abstained", raw)

    def test_fenced_json_is_accepted(self):
        raw = "```json\n" + review("approve") + "\n```"
        self.assertEqual(PL.parse_review(raw, DIFF)["status"], "valid")

    def test_explicit_abstain_is_valid(self):
        r = PL.parse_review(review("abstain"), DIFF)
        self.assertEqual((r["status"], r["verdict"], r["issues"]), ("abstained", "abstain", []))


class Machine(unittest.TestCase):
    def test_builder_cannot_review_itself(self):
        with self.assertRaises(ValueError):
            PL.new(spec(reviewers=[{"kind": "agent", "agent": "codex"}]))
        with self.assertRaises(ValueError):
            PL.new(spec(reviewers=[]))

    def test_happy_path(self):
        p, actions = PL.start(PL.new(spec()))
        self.assertEqual(p["state"], "building")
        self.assertEqual(actions, [{"do": "spawn_build"}])
        p, actions = PL.step(p, {"type": "build", "state": "review"})
        self.assertEqual(p["state"], "reviewing")
        self.assertEqual(actions, [{"do": "spawn_review", "reviewer": 0}, {"do": "spawn_review", "reviewer": 1}])
        p, actions = PL.step(p, {"type": "review", "reviewer": 0, "raw": review("approve", NIT), "diff": DIFF})
        self.assertEqual((p["state"], actions), ("reviewing", []))
        p, actions = PL.step(p, {"type": "review", "reviewer": 1, "raw": review("approve"), "diff": DIFF})
        self.assertEqual(p["state"], "ready")
        self.assertEqual(actions, [])

    def test_issues_go_back_to_the_builder_then_ready(self):
        p, _ = PL.start(PL.new(spec()))
        p, _ = PL.step(p, {"type": "build", "state": "review"})
        p, _ = PL.step(p, {"type": "review", "reviewer": 0, "raw": review("request_changes", SQLI, INVENTED), "diff": DIFF})
        p, actions = PL.step(p, {"type": "review", "reviewer": 1, "raw": review("request_changes", SQLI), "diff": DIFF})
        self.assertEqual(p["state"], "building")
        self.assertEqual(p["round"], 2)
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]["do"], "message_build")
        self.assertEqual(actions[0]["text"].count("SQL injection"), 1)  # deduplicated
        self.assertNotIn("missing auth check", actions[0]["text"])  # unsupported issue not forwarded
        self.assertEqual(p["history"][-1]["unsupported"], 1)
        p, actions = PL.step(p, {"type": "build", "state": "review"})
        self.assertEqual(p["state"], "reviewing")
        self.assertEqual(len(actions), 2)
        p, _ = PL.step(p, {"type": "review", "reviewer": 0, "raw": review("approve"), "diff": DIFF})
        p, _ = PL.step(p, {"type": "review", "reviewer": 1, "raw": review("approve"), "diff": DIFF})
        self.assertEqual(p["state"], "ready")

    def test_rounds_run_out(self):
        p, _ = PL.start(PL.new(spec(max_rounds=1)))
        p, _ = PL.step(p, {"type": "build", "state": "review"})
        p, _ = PL.step(p, {"type": "review", "reviewer": 0, "raw": review("request_changes", SQLI), "diff": DIFF})
        p, actions = PL.step(p, {"type": "review", "reviewer": 1, "raw": review("approve"), "diff": DIFF})
        self.assertEqual(p["state"], "escalated")
        self.assertEqual(actions, [])

    def test_all_reviewers_abstain_escalates(self):
        p, _ = PL.start(PL.new(spec()))
        p, _ = PL.step(p, {"type": "build", "state": "review"})
        p, _ = PL.step(p, {"type": "review", "reviewer": 0, "raw": "no idea", "diff": DIFF})
        p, _ = PL.step(p, {"type": "review", "reviewer": 1, "raw": "{}", "diff": DIFF})
        self.assertEqual(p["state"], "escalated")

    def test_one_abstention_is_fine_if_another_reviewer_is_valid(self):
        p, _ = PL.start(PL.new(spec()))
        p, _ = PL.step(p, {"type": "build", "state": "review"})
        p, _ = PL.step(p, {"type": "review", "reviewer": 0, "raw": "no idea", "diff": DIFF})
        p, _ = PL.step(p, {"type": "review", "reviewer": 1, "raw": review("approve"), "diff": DIFF})
        self.assertEqual(p["state"], "ready")

    def test_explicit_abstentions_are_valid_no_issue_reviews(self):
        p, _ = PL.start(PL.new(spec()))
        p, _ = PL.step(p, {"type": "build", "state": "review"})
        p, _ = PL.step(p, {"type": "review", "reviewer": 0, "raw": review("abstain"), "diff": DIFF})
        p, _ = PL.step(p, {"type": "review", "reviewer": 1, "raw": review("abstain"), "diff": DIFF})
        self.assertEqual(p["state"], "ready")

    def test_reasons_for_unusable_reviews_are_kept(self):
        p, _ = PL.start(PL.new(spec()))
        p, _ = PL.step(p, {"type": "build", "state": "review"})
        p, _ = PL.step(p, {"type": "review", "reviewer": 0, "raw": "", "diff": DIFF, "reason": "model call failed: ReadTimeout"})
        p, _ = PL.step(p, {"type": "review", "reviewer": 1, "raw": "", "diff": DIFF, "reason": "master: on battery (28%)"})
        self.assertEqual(p["state"], "escalated")
        self.assertEqual(p["reviews"]["0"]["reason"], "model call failed: ReadTimeout")
        self.assertIn("on battery (28%)", p["history"][-1]["note"])
        self.assertIn("ReadTimeout", p["history"][-1]["note"])

    def test_escalated_pipeline_can_retry_its_reviews(self):
        p, _ = PL.start(PL.new(spec()))
        p, _ = PL.step(p, {"type": "build", "state": "review"})
        p, _ = PL.step(p, {"type": "review", "reviewer": 0, "raw": "", "diff": DIFF})
        p, _ = PL.step(p, {"type": "review", "reviewer": 1, "raw": "", "diff": DIFF})
        self.assertEqual(p["state"], "escalated")
        p, actions = PL.step(p, {"type": "retry"})
        self.assertEqual(p["state"], "reviewing")
        self.assertEqual(actions, [{"do": "spawn_review", "reviewer": 0}, {"do": "spawn_review", "reviewer": 1}])
        self.assertTrue(all(r["status"] == "pending" for r in p["reviews"].values()))
        p, _ = PL.step(p, {"type": "review", "reviewer": 0, "raw": review("approve"), "diff": DIFF})
        p, _ = PL.step(p, {"type": "review", "reviewer": 1, "raw": review("approve"), "diff": DIFF})
        self.assertEqual(p["state"], "ready")
        # Only an escalated pipeline can be retried.
        q, actions = PL.step(PL.start(PL.new(spec()))[0], {"type": "retry"})
        self.assertEqual((q["state"], actions), ("building", []))

    def test_build_failures_and_cancel(self):
        p, _ = PL.start(PL.new(spec()))
        p, _ = PL.step(p, {"type": "build", "state": "failed"})
        self.assertEqual(p["state"], "failed")
        p, _ = PL.start(PL.new(spec()))
        p, _ = PL.step(p, {"type": "cancel"})
        self.assertEqual(p["state"], "cancelled")

    def test_late_or_duplicate_reviews_are_ignored(self):
        p, _ = PL.start(PL.new(spec()))
        p, _ = PL.step(p, {"type": "review", "reviewer": 0, "raw": review("approve"), "diff": DIFF})  # before reviewing
        self.assertEqual(p["state"], "building")
        p, _ = PL.step(p, {"type": "build", "state": "review"})
        p, _ = PL.step(p, {"type": "review", "reviewer": 0, "raw": review("approve"), "diff": DIFF})
        p, _ = PL.step(p, {"type": "review", "reviewer": 0, "raw": review("request_changes", SQLI), "diff": DIFF})
        self.assertEqual(p["state"], "reviewing")  # second review from reviewer 0 ignored
        self.assertEqual(p["reviews"]["0"]["verdict"], "approve")

    def test_state_is_json_serialisable(self):
        p, _ = PL.start(PL.new(spec()))
        p, _ = PL.step(p, {"type": "build", "state": "review"})
        json.dumps(p)


if __name__ == "__main__":
    unittest.main()
