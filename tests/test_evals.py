"""Tests for evals/run_evals.py scoring, plus reference solutions proving every coder case is solvable.
Run: python3 -m unittest -v tests/test_evals.py   (no Ollama needed)
"""
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "evals"))
import run_evals as E  # noqa: E402

REFERENCE = {
    "slugify": '''
import re, unicodedata
def slugify(text):
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
''',
    "parse_duration": '''
import re
def parse_duration(s):
    m = re.fullmatch(r"(?:(\\d+)h)?(?:(\\d+)m)?(?:(\\d+)s)?", s or "")
    if not s or not m:
        raise ValueError(s)
    h, mi, se = (int(x) if x else 0 for x in m.groups())
    return h * 3600 + mi * 60 + se
''',
    "merge_intervals": '''
def merge_intervals(intervals):
    out = []
    for s, e in sorted(intervals):
        if out and s <= out[-1][1]:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return out
''',
    "lru_cache": '''
from collections import OrderedDict
class LRUCache:
    def __init__(self, capacity):
        if capacity < 1:
            raise ValueError(capacity)
        self.cap, self.d = capacity, OrderedDict()
    def get(self, key):
        if key not in self.d:
            return -1
        self.d.move_to_end(key)
        return self.d[key]
    def put(self, key, value):
        self.d[key] = value
        self.d.move_to_end(key)
        if len(self.d) > self.cap:
            self.d.popitem(last=False)
''',
    "int_to_roman": '''
def int_to_roman(n):
    if isinstance(n, bool) or not isinstance(n, int):
        raise TypeError(n)
    if not 1 <= n <= 3999:
        raise ValueError(n)
    out = ""
    for v, r in [(1000,"M"),(900,"CM"),(500,"D"),(400,"CD"),(100,"C"),(90,"XC"),(50,"L"),(40,"XL"),(10,"X"),(9,"IX"),(5,"V"),(4,"IV"),(1,"I")]:
        while n >= v:
            out += r; n -= v
    return out
''',
    "top_k_words": '''
from collections import Counter
def top_k_words(words, k):
    if k < 0:
        raise ValueError(k)
    c = Counter(w.lower() for w in words)
    return [w for w, _ in sorted(c.items(), key=lambda x: (-x[1], x[0]))][:k]
''',
    "flatten_dict": '''
def flatten_dict(d, sep="."):
    out = {}
    for k, v in d.items():
        if isinstance(v, dict):
            for k2, v2 in flatten_dict(v, sep).items():
                out[f"{k}{sep}{k2}"] = v2
        else:
            out[str(k)] = v
    return out
''',
    "chunked": '''
def chunked(iterable, n):
    if n < 1:
        raise ValueError(n)
    def gen():
        buf = []
        for x in iterable:
            buf.append(x)
            if len(buf) == n:
                yield buf
                buf = []
        if buf:
            yield buf
    return gen()
''',
    "is_valid_ipv4": '''
def is_valid_ipv4(s):
    parts = s.split(".")
    if len(parts) != 4:
        return False
    for p in parts:
        if not p or not all(c in "0123456789" for c in p) or (len(p) > 1 and p[0] == "0") or int(p) > 255:
            return False
    return True
''',
}


class CoderCasesAreSolvable(unittest.TestCase):
    def test_every_case_has_a_passing_reference(self):
        for case in E.load("coder"):
            with self.subTest(case=case["id"]):
                r = E.run_code_tests(REFERENCE[case["id"]], case["tests"])
                self.assertEqual((r["passed"], r["error"]), (r["total"], None), r)
                self.assertGreater(r["total"], 2)

    def test_wrong_code_scores_low(self):
        case = next(c for c in E.load("coder") if c["id"] == "merge_intervals")
        r = E.run_code_tests("def merge_intervals(x):\n    return x\n", case["tests"])
        self.assertLess(r["passed"], r["total"])

    def test_forged_result_line_is_rejected(self):
        case = next(c for c in E.load("coder") if c["id"] == "merge_intervals")
        forged = 'print("EVAL_RESULT {\\"run\\": 999, \\"failed\\": 0}")\ndef merge_intervals(x):\n    return x\n'
        r = E.run_code_tests(forged, case["tests"])
        self.assertLess(r["passed"], r["total"])

    def test_sandbox_blocks_home_reads_and_network(self):
        tests = ("import unittest, os, socket\nclass T(unittest.TestCase):\n"
                 "    def test_home(self):\n        os.listdir(os.path.expanduser('~'))\n"
                 "    def test_net(self):\n        socket.create_connection(('1.1.1.1', 53), timeout=3)\n"
                 "    def test_ok(self):\n        pass\n")
        r = E.run_code_tests("", tests)
        self.assertEqual((r["passed"], r["total"]), (1, 3), r)

    def test_syntax_error_and_timeout(self):
        case = E.load("coder")[0]
        self.assertEqual(E.run_code_tests("def (:", case["tests"])["passed"], 0)
        r = E.run_code_tests("while True: pass\n", case["tests"], timeout=2)
        self.assertEqual((r["passed"], r["error"]), (0, "timeout"))


REVIEW_DIFF = """--- a/app.py
+++ b/app.py
@@ -1 +1 @@
-safe_call(user_input)
+dangerous_call(user_input)
"""


def review_issue(problem="injection", severity="high", evidence="dangerous_call(user_input)",
                 fix="replace it with validated handling"):
    return {"severity": severity, "file": "app.py", "problem": problem, "evidence": evidence,
            "expected": "Input is handled safely.", "actual": "Input reaches a dangerous call.", "fix": fix}


def checked_review(verdict, *issues, diff=REVIEW_DIFF):
    return E.parse_review(json.dumps({"verdict": verdict, "issues": list(issues)}), diff)


class Extraction(unittest.TestCase):
    def test_extract_fenced(self):
        self.assertEqual(E.extract_code("Here:\n```python\ndef f():\n    return 1\n```\nDone"), "def f():\n    return 1")

    def test_extract_longest_and_unfenced(self):
        text = "```python\nx = 1\n```\n```python\ndef g():\n    return 2\n```"
        self.assertIn("def g", E.extract_code(text))
        self.assertEqual(E.extract_code("def h(): pass"), "def h(): pass")

    def test_extract_prefers_block_defining_symbol(self):
        text = ("```Python\ndef add(a, b):\n    return a + b\n```\nExample:\n```python3\n"
                "import unittest\nclass LongExampleTests(unittest.TestCase):\n    def test_x(self):\n        pass\n```")
        self.assertTrue(E.extract_code(text, "add").startswith("def add"))

    def test_parse_review_is_strict(self):
        invalid = ["looks good", '{"verdict": "maybe", "issues": []}',
                   'Sure! {"verdict":"approve","issues":[]} thanks', '{"verdict":"approve"}',
                   '{"verdict":"approve","issues":["serious defect"]}',
                   '{"verdict":"approve","issues":[{"severity":"critical","problem":"x"}]}']
        for raw in invalid:
            self.assertEqual(E.parse_review(raw, REVIEW_DIFF)["status"], "invalid", raw)
        self.assertEqual(E.parse_review(' {"verdict":"approve","issues":[]} ', REVIEW_DIFF)["verdict"],
                         "approve")
        self.assertEqual(E.parse_review('{"verdict":"abstain","issues":[]}', REVIEW_DIFF)["status"],
                         "abstained")


class ReviewScoring(unittest.TestCase):
    bug = {"expect": "request_changes", "keywords": ["injection"]}
    clean = {"expect": "approve", "keywords": []}

    def test_found_bug(self):
        r = checked_review("request_changes", review_issue("SQL injection via f-string"))
        self.assertEqual(E.score_review(self.bug, r)["score"], 1.0)

    def test_keyword_in_low_issue_or_late_issue_does_not_count(self):
        low = checked_review("request_changes", review_issue(severity="low"))
        self.assertEqual(E.score_review(self.bug, low)["score"], 0.5)
        pile = [review_issue(f"thing {i}", fix=f"distinct proposal {i}") for i in range(3)]
        late = checked_review("request_changes", *pile, review_issue("injection", fix="late proposal"))
        self.assertEqual(E.score_review(self.bug, late)["score"], 0.5)

    def test_blanket_rejection_scores_poorly(self):
        cases = E.load("reviewer")
        always = checked_review("request_changes")
        total = sum(E.score_review(c, always)["score"] for c in cases) / len(cases)
        self.assertLess(total, 0.35)

    def test_right_verdict_wrong_reason(self):
        r = checked_review("request_changes", review_issue("rename variable", severity="low"))
        self.assertEqual(E.score_review(self.bug, r)["score"], 0.5)

    def test_missed_bug_and_invalid(self):
        self.assertEqual(E.score_review(self.bug, checked_review("approve"))["score"], 0.0)
        self.assertEqual(E.score_review(self.bug, None), {"score": 0.0, "valid_json": False,
                                                          "verdict_ok": False, "found_bug": False})

    def test_clean_diff_false_alarm(self):
        self.assertEqual(E.score_review(self.clean, checked_review("approve"))["score"], 1.0)
        self.assertEqual(E.score_review(self.clean, checked_review("abstain"))["score"], 1.0)
        nit = checked_review("approve", review_issue("docstring", severity="low"))
        self.assertEqual(E.score_review(self.clean, nit)["score"], 1.0)
        alarm = checked_review("approve", review_issue("imaginary bug"))
        self.assertEqual(E.score_review(self.clean, alarm)["score"], 0.0)
        self.assertEqual(E.score_review(self.clean, checked_review("request_changes"))["score"], 0.0)

    def test_unsupported_and_identical_fix_issues_do_not_count(self):
        unsupported = checked_review("request_changes", review_issue(evidence="not present in the diff"))
        identical = checked_review("request_changes", review_issue(fix="dangerous_call(user_input)"))
        self.assertEqual((unsupported["unsupported"], unsupported["issues"]), (1, []))
        self.assertEqual((identical["identical_fix"], identical["issues"]), (1, []))
        self.assertEqual(E.score_review(self.bug, unsupported)["score"], 0.5)
        self.assertEqual(E.score_review(self.bug, identical)["score"], 0.5)


class ReviewMetrics(unittest.TestCase):
    """G2 precision/recall/fp_rate over high/medium issues, with hand-made reviews."""
    bugs = [{"id": f"b{i}", "expect": "request_changes", "keywords": ["injection"],
             "split": "dev" if i < 2 else "holdout"} for i in range(4)]
    cleans = [{"id": f"c{i}", "expect": "approve", "keywords": [], "split": "dev" if i < 1 else "holdout"}
              for i in range(2)]

    def found(self):
        return checked_review("request_changes", review_issue("SQL injection via f-string"))

    def test_perfect_reviewer(self):
        pairs = ([(c, self.found()) for c in self.bugs]
                 + [(c, checked_review("approve", review_issue("docstring", severity="low"))) for c in self.cleans])
        m = E.review_metrics(pairs)
        self.assertEqual((m["recall"], m["precision"], m["fp_rate"]), (1.0, 1.0, 0.0))
        self.assertEqual((m["bug_cases"], m["clean_cases"], m["serious"], m["matched"]), (4, 2, 4, 4))

    def test_blanket_rejecter(self):
        # Flags the same plausible-sounding issue everywhere: full recall, but every clean case is a false alarm.
        pairs = [(c, self.found()) for c in self.bugs + self.cleans]
        m = E.review_metrics(pairs)
        self.assertEqual((m["recall"], m["fp_rate"]), (1.0, 1.0))
        self.assertAlmostEqual(m["precision"], 4 / 6, places=3)
        vague = [(c, checked_review("request_changes", review_issue("something is off"))) for c in self.bugs + self.cleans]
        m = E.review_metrics(vague)
        self.assertEqual((m["recall"], m["precision"], m["fp_rate"]), (0.0, 0.0, 1.0))

    def test_ratios_are_stored_unrounded_with_their_counts(self):
        # 8 matched of 9 raised = 0.888...; rounding to 3 places before a 0.9 trust check is what we must not do.
        nine = [review_issue(f"injection {i}", fix=f"distinct proposal {i}") for i in range(8)]
        nine.append(review_issue("unrelated worry", fix="another proposal"))
        bugs = [dict(self.bugs[0], id=f"b{i}") for i in range(3)]
        pairs = [(bugs[0], checked_review("request_changes", *nine)), (bugs[1], checked_review("approve")),
                 (bugs[2], checked_review("approve"))]
        m = E.review_metrics(pairs)
        self.assertEqual((m["matched"], m["serious"], m["found"], m["bug_cases"]), (8, 9, 1, 3))
        self.assertEqual(m["precision"], 8 / 9)
        self.assertEqual(m["recall"], 1 / 3)
        self.assertEqual(json.loads(json.dumps(m))["precision"], 8 / 9)  # survives results.json as-is
        self.assertEqual(E._fmt(m["precision"]), "0.89")  # only the leaderboard rounds

    def test_silent_approver(self):
        pairs = [(c, checked_review("approve")) for c in self.bugs + self.cleans]
        m = E.review_metrics(pairs)
        self.assertEqual((m["recall"], m["precision"], m["fp_rate"], m["serious"]), (0.0, None, 0.0, 0))

    def test_mixed_with_invalid_unsupported_and_identical(self):
        unsupported = checked_review("request_changes", review_issue(evidence="not present in the diff"))
        identical = checked_review("request_changes", review_issue(fix="dangerous_call(user_input)"))
        pairs = [(self.bugs[0], self.found()),
                 (self.bugs[1], checked_review("request_changes", review_issue("injection"),
                                               review_issue("unrelated worry", fix="another proposal"))),
                 (self.bugs[2], unsupported),
                 (self.bugs[3], None),
                 (self.cleans[0], identical),
                 (self.cleans[1], checked_review("approve", review_issue("imaginary bug")))]
        m = E.review_metrics(pairs)
        self.assertEqual(m["recall"], 0.5)
        self.assertEqual((m["serious"], m["matched"]), (4, 2))
        self.assertEqual(m["precision"], 0.5)
        self.assertEqual(m["fp_rate"], 0.5)
        self.assertEqual((m["unsupported"], m["identical_fix"]), (1, 1))
        self.assertEqual(E.review_metrics([(self.bugs[0], E.parse_review("not json", REVIEW_DIFF))])["recall"], 0.0)

    def test_split_metrics(self):
        pairs = [(c, self.found()) for c in self.bugs[:2]] + [(c, checked_review("approve")) for c in self.bugs[2:]]
        pairs += [(c, checked_review("approve")) for c in self.cleans]
        m = E.split_metrics(pairs)
        self.assertEqual(set(m), {"dev", "holdout", "all"})
        self.assertEqual((m["dev"]["recall"], m["holdout"]["recall"], m["all"]["recall"]), (1.0, 0.0, 0.5))
        self.assertEqual((m["dev"]["bug_cases"], m["holdout"]["clean_cases"]), (2, 1))
        self.assertIsNone(E.split_metrics([])["holdout"]["recall"])

    def test_eval_model_stores_metrics_and_leaderboard_shows_them(self):
        raw = json.dumps({"verdict": "approve", "issues": []})
        with (mock.patch.object(E, "chat", return_value={"text": raw, "seconds": 0.1, "tokens_per_s": 1}),
              mock.patch.object(E, "loaded_size_gb", return_value=1.0),
              mock.patch.object(E, "unload")):
            result = E.eval_model("http://unused", "model", ["reviewer_real"], 1)
        metrics = result["roles"]["reviewer_real"]["metrics"]
        self.assertEqual(metrics["holdout"]["bug_cases"], 5)
        self.assertEqual(metrics["holdout"]["clean_cases"], 3)
        self.assertEqual((metrics["all"]["recall"], metrics["all"]["precision"], metrics["all"]["fp_rate"]),
                         (0.0, None, 0.0))
        board = E.leaderboard([result], ["reviewer_real"])
        self.assertIn("Holdout P | Holdout R | Holdout FP", board)
        self.assertIn("| model | - | 0.00 | 0.00 |", board)

    def test_rerunning_other_roles_keeps_reviewer_real_and_its_timestamp(self):
        real = {"score": 80.0, "metrics": {"holdout": {"precision": 1.0, "recall": 0.8}}, "evaluated_at": 100.0}
        old = {"model": "m", "evaluated_at": 100.0, "roles": {"reviewer_real": real, "fast": {"score": 10.0}}}
        new = {"model": "m", "evaluated_at": 200.0, "roles": {"fast": {"score": 90.0, "evaluated_at": 200.0}}}
        merged = E.merge_result(old, new)
        self.assertEqual(merged["roles"]["reviewer_real"], real)
        self.assertEqual(merged["roles"]["fast"], {"score": 90.0, "evaluated_at": 200.0})
        self.assertEqual(merged["evaluated_at"], 200.0)
        legacy = E.merge_result({"model": "m", "evaluated_at": 50.0, "roles": {"coder": {"score": 1.0}}}, new)
        self.assertEqual(legacy["roles"]["coder"]["evaluated_at"], 50.0)  # older row's time, not the new run's
        rerun = E.merge_result(old, {"model": "m", "evaluated_at": 300.0,
                                     "roles": {"reviewer_real": {"score": 5.0, "evaluated_at": 300.0}}})
        self.assertEqual(rerun["roles"]["reviewer_real"]["score"], 5.0)
        self.assertEqual(E.merge_result(None, new), new)

    def test_eval_model_stamps_each_role(self):
        raw = json.dumps({"verdict": "approve", "issues": []})
        with (mock.patch.object(E, "chat", return_value={"text": raw, "seconds": 0.1, "tokens_per_s": 1}),
              mock.patch.object(E, "loaded_size_gb", return_value=1.0),
              mock.patch.object(E, "unload")):
            result = E.eval_model("http://unused", "model", ["reviewer_real"], 1)
        self.assertGreaterEqual(result["roles"]["reviewer_real"]["evaluated_at"], result["evaluated_at"])

    def test_chat_failure_counts_as_a_missed_bug(self):
        case = next(c for c in E.load("reviewer_real") if c["expect"] == "request_changes")
        with (mock.patch.object(E, "load", return_value=[case]),
              mock.patch.object(E, "chat", side_effect=OSError("down")),
              mock.patch.object(E, "loaded_size_gb", return_value=1.0),
              mock.patch.object(E, "unload")):
            result = E.eval_model("http://unused", "model", ["reviewer_real"], 1)
        self.assertEqual(result["roles"]["reviewer_real"]["metrics"]["all"]["bug_cases"], 1)
        self.assertEqual(result["roles"]["reviewer_real"]["metrics"]["all"]["recall"], 0.0)


class FastScoring(unittest.TestCase):
    def test_label(self):
        case = {"kind": "label", "expect": "bug"}
        self.assertEqual(E.score_fast(case, "Bug"), 1.0)
        self.assertEqual(E.score_fast(case, "bug."), 0.0)
        self.assertEqual(E.score_fast(case, "bug\nbecause it crashes"), 0.0)
        self.assertEqual(E.score_fast(case, "feature"), 0.0)

    def test_summary(self):
        case = {"kind": "summary", "max_chars": 50, "keywords": [["token"], ["expir"]]}
        self.assertEqual(E.score_fast(case, "Reject expired tokens in auth middleware"), 1.0)
        self.assertLess(E.score_fast(case, "Reject expired tokens in auth middleware."), 1.0)
        self.assertLess(E.score_fast(case, "token expir"), 1.0)
        self.assertLess(E.score_fast(case, '"Reject expired tokens in auth middleware"'), 1.0)
        self.assertEqual(E.score_fast(case, "Reject expired tokens\nand more"), 0.0)


class CaseFiles(unittest.TestCase):
    def test_shapes(self):
        coder, reviewer, reviewer_real, fast = (E.load("coder"), E.load("reviewer"),
                                                E.load("reviewer_real"), E.load("fast"))
        self.assertGreaterEqual(len(coder), 8)
        self.assertGreaterEqual(sum(c["expect"] == "request_changes" for c in reviewer), 8)
        self.assertGreaterEqual(sum(c["expect"] == "approve" for c in reviewer), 2)
        self.assertGreaterEqual(len(fast), 5)
        for c in reviewer:
            self.assertTrue(c["diff"].startswith("---"), c["id"])
        for c in reviewer_real:
            self.assertTrue(c["diff"].startswith("---"), c["id"])
        json.dumps(fast)

    def test_reviewer_real_split_is_balanced(self):
        cases = E.load("reviewer_real")
        self.assertEqual(len({c["id"] for c in cases}), len(cases))
        for split in E.SPLITS:
            chosen = [c for c in cases if c.get("split") == split]
            self.assertEqual(sum(c["expect"] == "request_changes" for c in chosen), 5, split)
            self.assertEqual(sum(c["expect"] == "approve" for c in chosen), 3, split)
        self.assertTrue(all(c.get("split") in E.SPLITS for c in cases))
        traps = [c for c in cases if c.get("trap")]
        self.assertGreaterEqual(len(traps), 2)
        self.assertTrue(all(c["split"] == "holdout" for c in traps))

    def test_reviewer_real_runs_through_reviewer_scoring(self):
        case = E.load("reviewer_real")[0]
        raw = json.dumps({"verdict": "abstain", "issues": []})
        with (mock.patch.object(E, "load", return_value=[case]),
              mock.patch.object(E, "chat", return_value={"text": raw, "seconds": 0.1, "tokens_per_s": 1}),
              mock.patch.object(E, "loaded_size_gb", return_value=1.0),
              mock.patch.object(E, "unload")):
            result = E.eval_model("http://unused", "model", ["reviewer_real"], 1)
        scored = result["roles"]["reviewer_real"]["cases"][0]
        self.assertTrue(scored["valid_json"])
        self.assertEqual(scored["score"], E.score_review(case, E.parse_review(raw, case["diff"]))["score"])


if __name__ == "__main__":
    unittest.main()
