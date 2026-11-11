"""Tests for the shared reviewer schema and deterministic diff grounding."""

import json
import math
import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "master" / "controller"))
import grounding  # noqa: E402


class GroundingCalibrationHelpers(unittest.TestCase):
    def test_check_quotes_uses_whitespace_normalised_verbatim_matching(self):
        context = "role: grounded\n  temperature:   0\nstatus: ready"
        quotes = ["temperature: 0", "status: ready", "status: missing", "   "]
        self.assertEqual(grounding.check_quotes(quotes, context), (2, 2))

    def test_content_logprobs_cover_only_split_answer_tokens(self):
        tokens = [
            {"token": '<think>guess {"answer":"London"}</think>', "logprob": math.log(0.01)},
            {"token": '{"status":"ANSWER","answer":"', "logprob": math.log(0.2)},
            {"token": "New", "logprob": math.log(0.9)},
            {"token": " York", "logprob": math.log(0.8)},
            {"token": " City", "logprob": math.log(0.7)},
            {"token": ".", "logprob": math.log(0.01)},
            {"token": '\",\"quotes\":[\"New York City\"],\"tags\":[\"ESTABLISHED\"],'
                      '\"confidence\":0.9}', "logprob": math.log(0.3)},
        ]
        score = grounding.content_logprob_score(tokens)
        self.assertAlmostEqual(score["min"], 0.7)
        self.assertAlmostEqual(score["mean"], 0.8)

    def test_content_logprobs_return_none_without_textual_answer_content(self):
        missing = [{"token": '{"status":"ABSTAIN"}', "logprob": -0.1}]
        punctuation = [{"token": '{"answer":"', "logprob": -0.1},
                       {"token": "...", "logprob": -2}, {"token": '"}', "logprob": -0.1}]
        self.assertIsNone(grounding.content_logprob_score(missing))
        self.assertIsNone(grounding.content_logprob_score(punctuation))

    def test_agreement_normalises_case_and_whitespace(self):
        samples = [{"answer": "New  York"}, {"answer": " new york "}, {"answer": "Boston"}]
        self.assertEqual(grounding.agreement(samples), 2 / 3)
        self.assertEqual(grounding.agreement([]), 0.0)

    def test_agreement_matches_primary_status_and_treats_invalid_as_disagreement(self):
        primary = {"status": "ANSWER", "answer": "X", "valid_json": True}
        samples = [
            {"status": "ANSWER", "answer": "Y", "valid_json": True},
            {"status": "ANSWER", "answer": " x ", "valid_json": True},
            {"status": "INVALID", "answer": "X", "valid_json": False},
        ]
        self.assertEqual(grounding.agreement(samples, primary), 1 / 3)
        abstain = {"status": "ABSTAIN", "answer": "", "valid_json": True}
        invalid = {"status": "INVALID", "answer": "", "valid_json": False}
        self.assertEqual(grounding.agreement([invalid], abstain), 0.0)

    def test_outcome_covers_all_five_classes(self):
        answerable = {"answerable": True, "answer": "11434", "aliases": ["port 11434"]}
        unavailable = {"answerable": False, "answer": "8000", "aliases": []}
        self.assertEqual(grounding.outcome(answerable, {"status": "ANSWER", "answer": "port 11434"}),
                         "correct")
        self.assertEqual(grounding.outcome(answerable, {"status": "ABSTAIN", "answer": ""}),
                         "wrong_abstain")
        self.assertEqual(grounding.outcome(answerable, {"status": "ANSWER", "answer": "8080"}), "wrong")
        self.assertEqual(grounding.outcome(unavailable, {"status": "ABSTAIN", "answer": ""}),
                         "correct_abstain")
        self.assertEqual(grounding.outcome(unavailable, {"status": "ANSWER", "answer": "8000"}),
                         "hallucinated")

    def test_outcome_accepts_sentence_form_but_rejects_near_misses(self):
        case = {"answerable": True, "answer": "8000", "aliases": ["port 8000"]}
        self.assertEqual(
            grounding.outcome(case, {"status": "ANSWER", "answer": "The service listens on port 8000."}),
            "correct",
        )
        self.assertEqual(
            grounding.outcome(case, {"status": "ANSWER", "answer": "The service listens on port 18000."}),
            "wrong",
        )

    def test_outcome_ignores_article_and_surrounding_punctuation(self):
        case = {"answerable": True, "answer": "reviewer", "aliases": []}
        reply = {"status": "ANSWER", "answer": "The reviewer."}
        self.assertEqual(grounding.outcome(case, reply), "correct")

    def test_outcome_rejects_numeric_answer_inside_decimal(self):
        case = {"answerable": True, "answer": "0", "aliases": []}
        reply = {"status": "ANSWER", "answer": "0.2"}
        self.assertEqual(grounding.outcome(case, reply), "wrong")

    def test_outcome_rejects_negated_and_ambiguous_mentions(self):
        case = {"answerable": True, "answer": "3", "aliases": []}
        for answer in ("It is not 3, it is 16", "3 or 16", "maybe 3", "I think it is 3"):
            with self.subTest(answer=answer):
                self.assertEqual(grounding.outcome(case, {"status": "ANSWER", "answer": answer}),
                                 "wrong")

    def test_contains_bounded_rejects_embedded_and_decimal_matches(self):
        self.assertTrue(grounding.contains_bounded("20", "timeout = 20"))
        self.assertFalse(grounding.contains_bounded("20", "timeout = 120"))
        self.assertFalse(grounding.contains_bounded("0", "temperature = 0.2"))

    def test_risk_coverage_keeps_high_signal_answered_rows(self):
        rows = [
            {"outcome": "correct", "confidence": 0.8},
            {"outcome": "wrong", "confidence": 0.4},
            {"outcome": "hallucinated", "confidence": 0.2},
            {"outcome": "correct_abstain", "confidence": 0.9},
        ]
        self.assertEqual(grounding.risk_coverage(rows, "confidence"), [
            (0.2, 1.0, 2 / 3),
            (0.4, 2 / 3, 0.5),
            (0.8, 1 / 3, 0.0),
        ])


DIFF = """--- a/app.py
+++ b/app.py
@@ -1,3 +1,4 @@
 def get_user(db, name):
-    return db.execute("SELECT * FROM users WHERE name = ?", (name,))
+    return db.execute(f"SELECT * FROM users WHERE name = '{name}'")
"""


def issue(**changes):
    value = {
        "severity": "high",
        "file": "app.py",
        "problem": "SQL injection via f-string",
        "evidence": "return db.execute(f\"SELECT * FROM users WHERE name = '{name}'\")",
        "expected": "The query uses a bound parameter.",
        "actual": "User input is interpolated into SQL.",
        "fix": "Pass name as a database parameter.",
    }
    value.update(changes)
    return value


def review(verdict="request_changes", *issues):
    return json.dumps({"verdict": verdict, "issues": list(issues)})


class CheckReview(unittest.TestCase):
    def test_valid_review(self):
        result = grounding.check_review(review("request_changes", issue()), DIFF)
        self.assertEqual(result["status"], "valid")
        self.assertEqual(result["verdict"], "request_changes")
        self.assertEqual(result["issues"], [issue()])
        self.assertEqual((result["unsupported"], result["identical_fix"]), (0, 0))

    def test_abstain_is_valid_and_counts_as_no_issues(self):
        result = grounding.check_review(review("abstain"), DIFF)
        self.assertEqual(result, {"status": "abstained", "verdict": "abstain", "issues": [],
                                  "unsupported": 0, "identical_fix": 0})

    def test_invalid_json_and_schema(self):
        invalid = [
            "not json",
            "{}",
            json.dumps({"verdict": "maybe", "issues": []}),
            json.dumps({"verdict": [], "issues": []}),
            json.dumps({"verdict": "approve", "issues": "none"}),
            review("request_changes", {key: value for key, value in issue().items() if key != "expected"}),
            review("request_changes", issue(severity="critical")),
            review("request_changes", issue(severity=[])),
            review("request_changes", issue(actual=3)),
        ]
        for raw in invalid:
            with self.subTest(raw=raw):
                self.assertEqual(grounding.check_review(raw, DIFF)["status"], "invalid")

    def test_unquoted_evidence_is_dropped_and_counted(self):
        result = grounding.check_review(
            review("request_changes", issue(evidence="This code permits SQL injection")), DIFF)
        self.assertEqual(result["issues"], [])
        self.assertEqual(result["unsupported"], 1)

    def test_evidence_is_whitespace_tolerant_and_ignores_diff_prefix(self):
        loose = "  return   db.execute(f\"SELECT * FROM users WHERE name = '{name}'\")  "
        result = grounding.check_review(review("request_changes", issue(evidence=loose)), DIFF)
        self.assertEqual(len(result["issues"]), 1)
        self.assertEqual(result["unsupported"], 0)

    def test_consecutive_lines_in_one_hunk_are_supported(self):
        evidence = ("return db.execute(\"SELECT * FROM users WHERE name = ?\", (name,)) "
                    "return db.execute(f\"SELECT * FROM users WHERE name = '{name}'\")")
        result = grounding.check_review(
            review("request_changes", issue(evidence=evidence)), DIFF)
        self.assertEqual(len(result["issues"]), 1)
        self.assertEqual(result["unsupported"], 0)

    def test_evidence_cannot_span_hunks(self):
        diff = """--- a/x.py
+++ b/x.py
@@ -1 +1 @@
-old one
+new one
@@ -10 +10 @@
-old two
+new two
"""
        result = grounding.check_review(
            review("request_changes", issue(evidence="new one new two")), diff)
        self.assertEqual((result["issues"], result["unsupported"]), ([], 1))

    def test_evidence_cannot_span_files(self):
        diff = """diff --git a/x.py b/x.py
--- a/x.py
+++ b/x.py
@@ -1 +1 @@
-old one
+new one
diff --git a/y.py b/y.py
--- a/y.py
+++ b/y.py
@@ -1 +1 @@
-old two
+new two
"""
        result = grounding.check_review(
            review("request_changes", issue(evidence="new one new two")), diff)
        self.assertEqual((result["issues"], result["unsupported"]), ([], 1))

    def test_evidence_cannot_skip_a_line(self):
        diff = """--- a/x.py
+++ b/x.py
@@ -1,3 +1,3 @@
+first line
+middle line
+last line
"""
        result = grounding.check_review(
            review("request_changes", issue(evidence="first line last line")), diff)
        self.assertEqual((result["issues"], result["unsupported"]), ([], 1))

    def test_diff_content_that_resembles_headers_is_quotable(self):
        diff = """--- a/x.py
+++ b/x.py
@@ -1 +1 @@
---counter
+++counter
"""
        for evidence in ("--counter", "++counter"):
            with self.subTest(evidence=evidence):
                result = grounding.check_review(
                    review("request_changes", issue(evidence=evidence)), diff)
                self.assertEqual(len(result["issues"]), 1)
                self.assertEqual(result["unsupported"], 0)

    def test_header_like_content_before_another_plain_hunk_is_quotable(self):
        diff = """--- a/x.py
+++ b/x.py
@@ -1 +1 @@
--- counter
+++ counter
@@ -3 +3 @@
-old
+new
"""
        for evidence in ("-- counter", "++ counter"):
            with self.subTest(evidence=evidence):
                result = grounding.check_review(
                    review("request_changes", issue(evidence=evidence)), diff)
                self.assertEqual(len(result["issues"]), 1)
                self.assertEqual(result["unsupported"], 0)

    def test_plain_diff_file_headers_split_hunks(self):
        diff = """--- a/x.py
+++ b/x.py
@@ -1 +1 @@
-old one
+new one
--- a/y.py
+++ b/y.py
@@ -1 +1 @@
-old two
+new two
"""
        result = grounding.check_review(
            review("request_changes", issue(evidence="new one new two")), diff)
        self.assertEqual((result["issues"], result["unsupported"]), ([], 1))

    def test_file_headers_are_not_quotable(self):
        for evidence in ("a/app.py", "b/app.py"):
            with self.subTest(evidence=evidence):
                result = grounding.check_review(
                    review("request_changes", issue(evidence=evidence)), DIFF)
                self.assertEqual((result["issues"], result["unsupported"]), ([], 1))

    def test_fix_equal_to_evidence_is_dropped(self):
        evidence = issue()["evidence"]
        result = grounding.check_review(
            review("request_changes", issue(fix=f"  {evidence}  ")), DIFF)
        self.assertEqual(result["issues"], [])
        self.assertEqual(result["identical_fix"], 1)

    def test_fix_equal_to_existing_diff_line_is_dropped(self):
        result = grounding.check_review(
            review("request_changes", issue(fix="  def   get_user(db, name): ")), DIFF)
        self.assertEqual(result["issues"], [])
        self.assertEqual(result["identical_fix"], 1)

    def test_fenced_json_is_accepted(self):
        raw = f"```json\n{review('approve')}\n```"
        result = grounding.check_review(raw, DIFF)
        self.assertEqual((result["status"], result["verdict"]), ("valid", "approve"))


if __name__ == "__main__":
    unittest.main()


class EmptyFix(unittest.TestCase):
    def test_empty_fix_is_not_an_identical_fix_even_with_blank_diff_lines(self):
        import json as _json
        diff = "--- a/x.py\n+++ b/x.py\n@@ -1,3 +1,3 @@\n def f():\n \n-    return 1\n+    return 2\n"
        raw = _json.dumps({"verdict": "request_changes", "issues": [{"severity": "high", "file": "x.py",
            "problem": "wrong value", "evidence": "return 2", "expected": "1", "actual": "2", "fix": ""}]})
        r = grounding.check_review(raw, diff)
        self.assertEqual((r["status"], len(r["issues"]), r["identical_fix"]), ("valid", 1, 0))


class Fragment(unittest.TestCase):
    def test_diff_fragment_without_hunk_header_is_one_hunk(self):
        raw = json.dumps({"verdict": "request_changes", "issues": [{"severity": "high", "file": "app.py",
            "problem": "greeting regressed", "evidence": "hello", "expected": "hi", "actual": "hello", "fix": "say hi"}]})
        r = grounding.check_review(raw, "+hello\n")
        self.assertEqual((r["status"], len(r["issues"])), ("valid", 1))
