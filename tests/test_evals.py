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
        coder, reviewer, reviewer_real, fast, grounded = (E.load("coder"), E.load("reviewer"),
                                                          E.load("reviewer_real"), E.load("fast"),
                                                          E.load("grounded"))
        self.assertGreaterEqual(len(coder), 8)
        self.assertGreaterEqual(sum(c["expect"] == "request_changes" for c in reviewer), 8)
        self.assertGreaterEqual(sum(c["expect"] == "approve" for c in reviewer), 2)
        self.assertGreaterEqual(len(fast), 5)
        for c in reviewer:
            self.assertTrue(c["diff"].startswith("---"), c["id"])
        for c in reviewer_real:
            self.assertTrue(c["diff"].startswith("---"), c["id"])
        self.assertGreaterEqual(len(grounded), 30)
        self.assertEqual(sum(c["answerable"] for c in grounded), len(grounded) / 2)
        expected_keys = {"id", "context", "question", "answerable", "answer", "aliases"}
        for case in grounded:
            with self.subTest(case=case["id"]):
                self.assertEqual(set(case), expected_keys)
                hits = [value for value in [case["answer"], *case["aliases"]]
                        if E.grounding.contains_bounded(value, case["context"])]
                self.assertEqual(bool(hits), case["answerable"], hits)
        json.dumps(fast)

    def test_grounding_answerability_uses_bounded_values(self):
        self.assertTrue(E.grounding.contains_bounded("20", "timeout=20"))
        self.assertFalse(E.grounding.contains_bounded("20", "timeout=120"))
        self.assertFalse(E.grounding.contains_bounded("0", "temperature=0.2"))

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


class GroundedRole(unittest.TestCase):
    @staticmethod
    def reply(status, answer, quotes, tags, confidence, probability=0.9):
        text = json.dumps({"status": status, "answer": answer, "quotes": quotes,
                           "tags": tags, "confidence": confidence}, separators=(",", ":"))
        return {"text": text, "seconds": 0.1, "tokens_per_s": 12,
                "logprobs": [{"token": text, "logprob": __import__("math").log(probability)}]}

    def test_chat_requests_json_temperature_and_logprobs(self):
        captured = {}

        def fake_ollama(base, path, body):
            captured.update({"base": base, "path": path, "body": body})
            return {"message": {"content": '{"answer":"ready"}', "logprobs": []}}

        with mock.patch.object(E, "ollama", side_effect=fake_ollama):
            E.chat("http://unused", "model", "system", "prompt", json_mode=True,
                   temperature=0.5, logprobs=True, seed=44)
        self.assertEqual(captured["path"], "/api/chat")
        self.assertEqual(captured["body"]["format"], "json")
        self.assertTrue(captured["body"]["logprobs"])
        self.assertEqual(captured["body"]["options"]["temperature"], 0.5)
        self.assertEqual(captured["body"]["options"]["seed"], 44)

    def test_grounded_eval_uses_fake_replies_and_computes_metrics(self):
        cases = [
            {"id": "known", "context": "status: ready", "question": "What status?",
             "answerable": True, "answer": "ready", "aliases": []},
            {"id": "unknown", "context": "status: pending", "question": "Who owns it?",
             "answerable": False, "answer": "owner", "aliases": []},
        ]
        calls = []

        def fake_chat(base, model, system, prompt, **kwargs):
            calls.append(kwargs)
            if "status: ready" in prompt:
                return self.reply("ANSWER", "ready", ["status: ready"], ["ESTABLISHED"], 0.9)
            return self.reply("ABSTAIN", "", [], ["UNKNOWN"], 0.2)

        with (mock.patch.object(E, "load", return_value=cases),
              mock.patch.object(E, "chat", side_effect=fake_chat),
              mock.patch.object(E, "loaded_size_gb", return_value=1.0),
              mock.patch.object(E, "unload")):
            result = E.eval_model("http://unused", "model", ["grounded"], 1)
        role = result["roles"]["grounded"]
        self.assertEqual((role["score"], role["hallucination_rate"], role["correct_abstain_rate"]),
                         (100.0, 0.0, 1.0))
        self.assertEqual(role["quote_validity"], 1.0)
        self.assertAlmostEqual(role["cases"][0]["logprob_min"], 0.9)
        self.assertEqual(role["cases"][0]["agreement"], 1.0)
        self.assertIsNone(role["cases"][1]["agreement"])
        self.assertEqual(len(calls), 5)  # primary for both cases plus three short-answer samples
        self.assertEqual([call["temperature"] for call in calls], [0, 0.5, 0.5, 0.5, 0])
        self.assertTrue(all(call["json_mode"] and call["logprobs"] for call in calls))
        report = E.grounding_report([result])
        self.assertIn("model | 100.0% | 0.0% | 100.0%", report)
        self.assertIn("logprob_min | dropped", report)

    def test_grounded_reply_schema_rejects_malformed_fake_replies(self):
        invalid = ["not json", "{}", json.dumps({"status": "MAYBE", "answer": "x", "quotes": [],
                                                   "tags": ["UNKNOWN"], "confidence": 0.2})]
        for raw in invalid:
            self.assertEqual(E.parse_grounded_reply(raw)["status"], "INVALID")

        malformed = E.parse_grounded_reply(json.dumps(
            {"status": "ANSWER", "answer": "x", "quotes": [], "tags": ["CERTAIN"], "confidence": 2}))
        self.assertEqual(malformed["status"], "ANSWER")
        self.assertFalse(malformed["valid_json"])

    def test_schema_invalid_abstention_is_still_scored_as_abstention(self):
        case = {"id": "missing", "context": "status: pending", "question": "Who owns it?",
                "answerable": False, "answer": "Alice", "aliases": []}
        row = E.score_grounded(case, {"text": json.dumps({"status": "ABSTAIN", "answer": "",
                                                            "quotes": []}), "logprobs": []}, [])
        self.assertEqual(row["outcome"], "correct_abstain")
        self.assertFalse(row["valid_json"])

    def test_sample_agreement_is_with_primary_reply(self):
        case = {"id": "known", "context": "value: X", "question": "What value?",
                "answerable": True, "answer": "X", "aliases": []}
        primary = self.reply("ANSWER", "X", ["value: X"], ["ESTABLISHED"], 0.9)
        samples = [self.reply("ANSWER", "Y", [], ["ESTABLISHED"], 0.8) for _ in range(3)]
        row = E.score_grounded(case, primary, samples)
        self.assertEqual(row["agreement"], 0.0)

    @staticmethod
    def calibration_rows(held_out_good=True):
        train_ids = ["case-0", "case-2", "case-3", "case-7", "case-8",
                     "case-9", "case-10", "case-11", "case-12", "case-13"]
        held_ids = ["case-1", "case-4", "case-5", "case-6"]
        rows = []
        for index, case_id in enumerate(train_ids):
            rows.append({"id": case_id, "status": "ANSWER",
                         "outcome": "correct" if index < 7 else "wrong",
                         "confidence": 0.9 if index < 7 else 0.1})
        for index, case_id in enumerate(held_ids):
            correct = index < 3
            signal = (0.9 if correct else 0.1) if held_out_good else (0.1 if correct else 0.9)
            rows.append({"id": case_id, "status": "ANSWER",
                         "outcome": "correct" if correct else "wrong", "confidence": signal})
        return rows

    def test_threshold_is_fit_on_training_and_decided_on_held_out_cases(self):
        passing = E.held_out_signal(self.calibration_rows(), "confidence")
        failing = E.held_out_signal(self.calibration_rows(held_out_good=False), "confidence")
        self.assertEqual(passing["threshold"], 0.9)
        self.assertEqual(failing["threshold"], 0.9)
        self.assertEqual((passing["decision"], passing["coverage"], passing["error_rate"]),
                         ("kept", 0.75, 0.0))
        self.assertEqual(failing["decision"], "dropped")
        self.assertEqual((passing["train_rows"], passing["held_out_rows"]), (10, 4))

    def test_reports_explicitly_keep_or_drop_every_signal(self):
        kept = {"decision": "kept", "threshold": 0.7, "coverage": 0.75,
                "error_rate": 0.1, "baseline_error_rate": 0.3}
        dropped = {"decision": "dropped", "threshold": 0.5, "coverage": 0.5,
                   "error_rate": 0.2, "baseline_error_rate": 0.3}
        role = {"score": 60.0, "hallucination_rate": 0.4, "correct_abstain_rate": 0.6,
                "quote_validity": 0.9, "cases": [],
                "signal_decisions": {signal: (kept if signal == "confidence" else dropped)
                                     for signal in E.GROUNDING_SIGNALS}}
        result = {"model": "calibrated", "roles": {"grounded": role}, "median_s": 1.0,
                  "tokens_per_s": 10.0, "size_gb": 2.0, "fits_16gb": True}
        report = E.grounding_report([result])
        board = E.leaderboard([result], ["grounded"])
        for signal in E.GROUNDING_SIGNALS:
            decision = "kept" if signal == "confidence" else "dropped"
            self.assertIn(f"| calibrated | {signal} | {decision} |", report)
            self.assertIn(f"| calibrated | {signal} | {decision} |", board)

    def test_committed_grounding_report_is_generated(self):
        results = json.loads((E.HERE / "results" / "results.json").read_text())
        self.assertEqual((E.HERE / "results" / "GROUNDING.md").read_text(),
                         E.grounding_report(results))
        # The placeholder is only for a results.json without grounded rows.
        grounded = [r for r in results if "grounded" in r.get("roles", {})]
        if not grounded:
            self.assertIn("No grounded evaluations have been recorded", E.grounding_report(results))
        import re as _re
        strip = lambda text: _re.sub(r"Generated [^.]*\.", "Generated X.", text)  # noqa: E731
        leaderboard = (E.HERE / "results" / "LEADERBOARD.md").read_text()
        self.assertEqual(strip(leaderboard), strip(E.leaderboard(results)))
        for result in grounded:
            decisions = E.signal_decisions(result["roles"]["grounded"])
            for signal in E.GROUNDING_SIGNALS:
                self.assertIn(f"| {result['model']} | {signal} | {decisions[signal]['decision']} |", leaderboard)
        if not grounded:
            for signal in E.GROUNDING_SIGNALS:
                self.assertIn(f"| — | {signal} | dropped |", leaderboard)


def _mixed_results():
    real = {"score": 60.0, "metrics": {"holdout": {"precision": 0.5, "recall": 0.4}}}
    grounded = {"score": 70.0, "hallucination_rate": 0.1, "correct_abstain_rate": 0.9, "quote_validity": 1.0,
                "cases": [], "signal_decisions": {}}
    base = {"median_s": 1.0, "tokens_per_s": 5.0, "size_gb": 2.0, "fits_16gb": True}
    return [
        {"model": "only-real", "roles": {"reviewer_real": real}, **base},
        {"model": "old-trio", "roles": {"coder": {"score": 50.0}, "reviewer": {"score": 40.0},
                                        "fast": {"score": 30.0}}, **base},
        {"model": "all-in", "roles": {"coder": {"score": 90.0}, "reviewer_real": real, "grounded": grounded}, **base},
        {"model": "big-no-fit", "roles": {"coder": {"score": 99.0}}, **{**base, "fits_16gb": False}},
        {"model": "no-meta", "roles": {"coder": {"score": 20.0, "think": False}}, "size_gb": None},
    ]


class ReportAndThinkTests(unittest.TestCase):
    def test_leaderboard_lists_every_model_per_section_with_empty_cells(self):
        board = E.leaderboard(_mixed_results())
        section = board.split("## reviewer_real")[1].split("\n## ")[0]
        self.assertIn("| only-real |", section)
        self.assertIn("| all-in |", section)
        self.assertNotIn("| old-trio |", section)
        for model in ("only-real", "old-trio", "all-in"):
            self.assertIn(f"| {model} |", board)
        self.assertIn("| only-real |  |  |  | 60 |  |", board)  # empty cells for missing roles
        self.assertIn("| all-in | 90 |", board)
        grounding = board.split("## Grounding signal policy")[1]
        self.assertIn("| all-in |", grounding)

    def test_rows_sorted_by_mean_of_present_roles_then_role_count(self):
        board = E.leaderboard(_mixed_results())
        order = [m for m in ("big-no-fit", "all-in", "only-real", "old-trio", "no-meta")]
        positions = [board.index(f"| {m} |") for m in order]
        self.assertEqual(positions, sorted(positions))
        two = {"model": "two", "roles": {"coder": {"score": 50.0}, "fast": {"score": 50.0}}}
        one = {"model": "one", "roles": {"coder": {"score": 50.0}}}
        tied = E.leaderboard([one, two])
        self.assertLess(tied.index("| two |"), tied.index("| one |"))
        self.assertIn("mean score", board)

    def test_missing_fields_and_recommendations_only_name_models_with_the_role(self):
        board = E.leaderboard(_mixed_results())
        self.assertIn("| no-meta | 20 |", board)  # no median_s / fits_16gb: empty cells, no crash
        self.assertIn("(last run)", board)
        recs = board.split("## Recommendation")[1].split("Treat role scores")[0]
        has = {}
        for row in _mixed_results():
            for role in row["roles"]:
                has.setdefault(role, set()).add(row["model"])
        names = {row["model"] for row in _mixed_results()}
        for line in recs.splitlines():
            if not line.startswith("- **"):
                continue
            role = line.split("**")[1]
            for name in names - has[role]:
                self.assertNotIn(name, line)
        self.assertIn("- **coder**: big-no-fit (99%); on a 16GB laptop: all-in (90%)", recs)
        self.assertIn("- **reviewer_real**: only-real (60%)", recs)

    def test_think_is_stored_per_role_and_unknown_is_noted(self):
        raw = json.dumps({"verdict": "approve", "issues": []})
        with (mock.patch.object(E, "chat", return_value={"text": raw, "seconds": 0.1, "tokens_per_s": 1}),
              mock.patch.object(E, "loaded_size_gb", return_value=1.0),
              mock.patch.object(E, "unload")):
            result = E.eval_model("http://unused", "model", ["reviewer_real"], 1)
        self.assertIs(result["roles"]["reviewer_real"]["think"], False)
        board = E.leaderboard(_mixed_results())
        note = [x for x in board.splitlines() if x.startswith("think: unknown (before think=false) for:")]
        self.assertEqual(len(note), 1)
        self.assertIn("only-real", note[0])
        self.assertNotIn("no-meta", note[0])  # its role result records think=false
        self.assertIn("| no-meta | 20 |", board)
        self.assertIn(" off |", board)

    def test_report_only_requires_results_json(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            with (mock.patch.object(sys, "argv", ["run_evals.py", "--report-only", "--out", tmp]),
                  mock.patch("sys.stderr")):
                with self.assertRaises(SystemExit):
                    E.main()
            self.assertFalse(Path(tmp, "LEADERBOARD.md").exists())

    def test_models_still_required_without_report_only(self):
        with (mock.patch.object(sys, "argv", ["run_evals.py"]), mock.patch("sys.stderr")):
            with self.assertRaises(SystemExit):
                E.main()

    def test_merge_of_subset_run_keeps_other_roles(self):
        old = {"model": "m", "evaluated_at": 1.0,
               "roles": {"coder": {"score": 1.0}, "reviewer_real": {"score": 2.0}, "fast": {"score": 3.0}}}
        new = {"model": "m", "evaluated_at": 2.0, "roles": {"fast": {"score": 9.0}}}
        merged = E.merge_result(old, new)
        self.assertEqual(set(merged["roles"]), {"coder", "reviewer_real", "fast"})
        self.assertEqual(merged["roles"]["fast"]["score"], 9.0)

    def test_report_only_makes_no_model_calls(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "results.json").write_text(json.dumps(_mixed_results()))
            before = Path(tmp, "results.json").read_bytes()
            with (mock.patch.object(E, "ollama", side_effect=AssertionError("model call")),
                  mock.patch.object(E, "eval_model", side_effect=AssertionError("model call")),
                  mock.patch.object(sys, "argv", ["run_evals.py", "--report-only", "--out", tmp]),
                  mock.patch("builtins.print")):
                self.assertEqual(E.main(), 0)
            board = Path(tmp, "LEADERBOARD.md").read_text()
            self.assertIn("| only-real |", board)
            self.assertTrue(Path(tmp, "GROUNDING.md").exists())
            self.assertEqual(Path(tmp, "results.json").read_bytes(), before)

    def test_chat_sends_think_false_by_default(self):
        captured = {}

        def fake_ollama(base, path, body):
            captured.update(body)
            return {"message": {"content": "<think>x</think>ok"}}

        with mock.patch.object(E, "ollama", side_effect=fake_ollama):
            out = E.chat("http://unused", "qwen3:8b", "s", "p")
        self.assertIs(captured["think"], False)
        self.assertEqual(out["text"], "ok")


if __name__ == "__main__":
    unittest.main()
