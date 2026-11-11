"""Tests for the shared reviewer schema and deterministic diff grounding."""

import json
import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "master" / "controller"))
import grounding  # noqa: E402


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
