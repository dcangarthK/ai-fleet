"""Pure validation and grounding helpers shared by the fleet and its evals."""

import math
import json
import re
import string
from collections import Counter


VERDICTS = {"approve", "request_changes", "abstain"}
SEVERITIES = {"high", "medium", "low"}
ISSUE_FIELDS = ("severity", "file", "problem", "evidence", "expected", "actual", "fix")
REVIEW_SCHEMA = (
    '{"verdict":"approve|request_changes|abstain","issues":[{'
    '"severity":"high|medium|low","file":"","problem":"","evidence":"",'
    '"expected":"","actual":"","fix":""}]}'
)
REVIEW_GUIDANCE = (
    "Evidence must be an exact quote from one added, removed, or context line, or from consecutive "
    "lines in one hunk. State the expected and actual behavior for each issue. A fix must differ "
    "from the evidence and from lines already in the diff. If there is no defensible issue, abstain."
)


def _normalise(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def check_quotes(quotes: list[str], context: str) -> tuple[int, int]:
    """Count quotes that occur verbatim after whitespace normalisation."""
    if not isinstance(quotes, list) or not isinstance(context, str):
        return 0, len(quotes) if isinstance(quotes, list) else 0
    normalised_context = _normalise(context)
    valid = 0
    for quote in quotes:
        if isinstance(quote, str) and _normalise(quote) and _normalise(quote) in normalised_context:
            valid += 1
    return valid, len(quotes) - valid


def _json_string_end(text: str, start: int) -> int | None:
    """Return the index of a JSON string's closing quote."""
    escaped = False
    for index in range(start + 1, len(text)):
        char = text[index]
        if escaped:
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == '"':
            return index
    return None


def _answer_value_span(text: str) -> tuple[int, int] | None:
    """Locate the raw content span of the top-level JSON ``answer`` string."""
    masked = list(text)
    for match in re.finditer(r"<think>[\s\S]*?</think>", text, re.IGNORECASE):
        masked[match.start():match.end()] = " " * (match.end() - match.start())
    visible = "".join(masked)
    depth = 0
    index = 0
    while index < len(visible):
        char = visible[index]
        if char == '"':
            end = _json_string_end(visible, index)
            if end is None:
                return None
            if depth == 1:
                try:
                    value = json.loads(visible[index:end + 1])
                except ValueError:
                    value = None
                cursor = end + 1
                while cursor < len(visible) and visible[cursor].isspace():
                    cursor += 1
                if value == "answer" and cursor < len(visible) and visible[cursor] == ":":
                    cursor += 1
                    while cursor < len(visible) and visible[cursor].isspace():
                        cursor += 1
                    if cursor >= len(visible) or visible[cursor] != '"':
                        return None
                    value_end = _json_string_end(visible, cursor)
                    return None if value_end is None else (cursor + 1, value_end)
            index = end + 1
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth = max(0, depth - 1)
        index += 1
    return None


def content_logprob_score(tokens: list[dict]) -> dict[str, float] | None:
    """Return min/mean probabilities for answer-content tokens in a JSON reply.

    Ollama supplies one entry per emitted token with ``token`` and ``logprob``
    fields. JSON syntax, punctuation-only answer tokens, and any ``<think>``
    block are deliberately excluded.
    """
    if not isinstance(tokens, list) or not tokens:
        return None
    pieces = []
    for entry in tokens:
        if not isinstance(entry, dict) or not isinstance(entry.get("token"), str):
            return None
        pieces.append(entry["token"])
    text = "".join(pieces)
    span = _answer_value_span(text)
    if span is None:
        return None
    answer_start, answer_end = span
    probabilities = []
    offset = 0
    for entry, piece in zip(tokens, pieces):
        token_start, token_end = offset, offset + len(piece)
        offset = token_end
        overlap_start = max(token_start, answer_start)
        overlap_end = min(token_end, answer_end)
        if overlap_start >= overlap_end:
            continue
        content = text[overlap_start:overlap_end]
        if not any(char.isalnum() for char in content):
            continue
        logprob = entry.get("logprob")
        if (isinstance(logprob, bool) or not isinstance(logprob, (int, float))
                or math.isnan(logprob) or logprob == math.inf):
            continue
        probability = 0.0 if logprob == -math.inf else (1.0 if logprob >= 0 else math.exp(logprob))
        probabilities.append(probability)
    if not probabilities:
        return None
    return {"min": min(probabilities), "mean": sum(probabilities) / len(probabilities)}


def _normalised_answer(value) -> str:
    if isinstance(value, dict):
        value = value.get("answer", "")
    return _normalise(value).casefold() if isinstance(value, str) else ""


def agreement(samples: list, primary: dict | None = None) -> float:
    """Return majority agreement, or agreement with a scored primary reply.

    When ``primary`` is supplied, schema-invalid samples always disagree and
    both status and normalised answer must match the primary.
    """
    if not samples:
        return 0.0
    if primary is not None:
        if primary.get("status") == "INVALID" or primary.get("valid_json") is False:
            return 0.0
        matches = 0
        for sample in samples:
            if (isinstance(sample, dict) and sample.get("valid_json") is not False
                    and sample.get("status") != "INVALID"
                    and sample.get("status") == primary.get("status")
                    and _normalised_answer(sample) == _normalised_answer(primary)):
                matches += 1
        return matches / len(samples)
    answers = [_normalised_answer(sample) for sample in samples]
    majority_count = Counter(answers).most_common(1)[0][1]
    return majority_count / len(answers)


def contains_bounded(value: str, text: str) -> bool:
    """Return whether ``value`` occurs as a bounded, non-decimal substring."""
    if not isinstance(value, str) or not value or not isinstance(text, str):
        return False
    pattern = rf"(?<![\w.]){re.escape(value)}(?![\w]|\.\w)"
    return re.search(pattern, text) is not None


_AMBIGUOUS_ANSWER = re.compile(
    r"\b(?:not|no|or|and|either|maybe|perhaps|possibly|probably|likely|approximately|around|about|"
    r"think|believe|guess|seems|appears|unknown|uncertain|instead|actually|but|from)\b",
    re.IGNORECASE,
)


def _answer_is_accepted(reply: str, accepted: list) -> bool:
    """Accept exact values and short, unambiguous declarative wrappers."""
    candidate = _normalised_answer(reply).strip(string.whitespace + string.punctuation)
    without_article = re.sub(r"^(?:the|a|an)\s+", "", candidate)
    values = [_normalised_answer(value) for value in accepted]
    values = [value for value in values if value]
    if candidate in values or without_article in values:
        return True
    if len(candidate.split()) > 8 or _AMBIGUOUS_ANSWER.search(candidate):
        return False
    for value in values:
        match = re.search(rf"(?<![\w.]){re.escape(value)}(?![\w]|\.\w)$", candidate)
        if not match:
            continue
        prefix = candidate[:match.start()].rstrip()
        if re.search(r"(?:\bis|\bwas|\bequals|=|:|\bon port)\s*$", prefix):
            if value.replace(".", "", 1).isdigit():
                other_numbers = re.findall(r"(?<![\w.])\d+(?:\.\d+)?(?![\w.]|\.\w)", prefix)
                if other_numbers:
                    continue
            return True
    return False


def outcome(case: dict, reply: dict) -> str:
    """Classify a grounded reply against an answerable/unanswerable case."""
    status = reply.get("status") if isinstance(reply, dict) else None
    if not case.get("answerable"):
        if status == "ABSTAIN":
            return "correct_abstain"
        return "hallucinated" if status == "ANSWER" else "wrong"
    if status == "ABSTAIN":
        return "wrong_abstain"
    if status != "ANSWER":
        return "wrong"
    accepted = [case.get("answer", ""), *case.get("aliases", [])]
    return "correct" if _answer_is_accepted(reply.get("answer", ""), accepted) else "wrong"


def risk_coverage(rows: list[dict], signal: str) -> list[tuple[float, float, float]]:
    """Build a high-signal-first risk/coverage curve over answered rows."""
    answered = [row for row in rows
                if row.get("outcome") in {"correct", "hallucinated", "wrong"}
                and row.get("status", "ANSWER") == "ANSWER"]
    if not answered:
        return []
    available = [row for row in answered
                 if isinstance(row.get(signal), (int, float)) and not isinstance(row.get(signal), bool)
                 and math.isfinite(row[signal])]
    thresholds = sorted({float(row[signal]) for row in available})
    curve = []
    for threshold in thresholds:
        kept = [row for row in available if row[signal] >= threshold]
        errors = sum(row["outcome"] in {"hallucinated", "wrong"} for row in kept)
        curve.append((threshold, len(kept) / len(answered), errors / len(kept)))
    return curve


def _plain_file_header(lines: list[str], index: int) -> bool:
    """Return whether ``index`` starts a plain unified-diff file header."""
    return (index + 2 < len(lines)
            and lines[index].startswith("--- ")
            and lines[index + 1].startswith("+++ ")
            and lines[index + 2].startswith("@@"))


def _hunk_counts(header: str) -> tuple[int, int] | None:
    """Return the old/new line counts declared by a unified-diff hunk."""
    match = re.match(r"^@@ -\d+(?:,(\d+))? \+\d+(?:,(\d+))? @@", header)
    if not match:
        return None
    return tuple(int(count) if count is not None else 1 for count in match.groups())


def _diff_hunks(diff: str) -> list[list[str]]:
    """Return normalised content lines, preserving file and hunk boundaries."""
    raw_lines = diff.splitlines()
    hunks: list[list[str]] = []
    current_hunk: list[str] | None = None
    remaining: tuple[int, int] | None = None
    git_file = False
    index = 0
    while index < len(raw_lines):
        line = raw_lines[index]
        if line.startswith("diff --git "):
            current_hunk = None
            remaining = None
            git_file = True
        elif line.startswith("@@"):
            current_hunk = []
            hunks.append(current_hunk)
            remaining = _hunk_counts(line)
        elif (current_hunk is not None and remaining == (0, 0) and not git_file
              and _plain_file_header(raw_lines, index)):
            # A plain unified diff has no ``diff --git`` boundary. Its paired
            # file headers followed by a hunk header start the next file.
            current_hunk = None
            remaining = None
            index += 1  # Skip both file-header lines; ``@@`` is handled next.
        elif current_hunk is not None and line.startswith(("+", "-", " ")):
            current_hunk.append(_normalise(line[1:]))
            if remaining is not None:
                old_count, new_count = remaining
                if line[0] in ("-", " "):
                    old_count -= 1
                if line[0] in ("+", " "):
                    new_count -= 1
                remaining = (old_count, new_count)
        index += 1
    if not hunks:
        # No hunk header was parsed (every ``@@`` line opens a hunk), so this is a
        # fragment such as pasted lines: its content lines form one hunk. Only a
        # ``--- `` line directly followed by a ``+++ `` line is a file-header pair.
        fragment: list[str] = []
        index = 0
        while index < len(raw_lines):
            line = raw_lines[index]
            if (line.startswith("--- ") and index + 1 < len(raw_lines)
                    and raw_lines[index + 1].startswith("+++ ")):
                index += 2
                continue
            if line.startswith(("+", "-", " ")):
                fragment.append(_normalise(line[1:]))
            index += 1
        if fragment:
            hunks.append(fragment)
    return hunks


def _diff_lines(diff: str) -> list[str]:
    """Return content from unified-diff added, removed, and context lines."""
    return [line for hunk in _diff_hunks(diff) for line in hunk]


def _evidence_is_supported(evidence: str, hunks: list[list[str]]) -> bool:
    """Check for a verbatim quote contained within a single diff hunk."""
    return any(evidence in _normalise(" ".join(hunk)) for hunk in hunks)


def _result(status: str, verdict: str | None = None, issues: list[dict] | None = None,
            unsupported: int = 0, identical_fix: int = 0) -> dict:
    return {
        "status": status,
        "verdict": verdict,
        "issues": issues or [],
        "unsupported": unsupported,
        "identical_fix": identical_fix,
    }


def _json_value(raw_text: str):
    text = raw_text.strip()
    fenced = re.fullmatch(r"```[ \t]*json[ \t]*\r?\n([\s\S]*?)\r?\n?```", text, re.IGNORECASE)
    if fenced:
        text = fenced.group(1).strip()
    return json.loads(text)


def check_review(raw_text: str, diff: str) -> dict:
    """Validate a review and drop issues not grounded in the unified diff.

    The returned dictionary always has ``status``, ``verdict``, ``issues``,
    ``unsupported``, and ``identical_fix`` keys.
    """
    if not isinstance(raw_text, str) or not isinstance(diff, str):
        return _result("invalid")
    try:
        value = _json_value(raw_text)
    except (TypeError, ValueError):
        return _result("invalid")
    if not isinstance(value, dict):
        return _result("invalid")
    verdict = value.get("verdict")
    if (not isinstance(verdict, str) or verdict not in VERDICTS
            or not isinstance(value.get("issues"), list)):
        return _result("invalid")

    clean_issues = []
    for issue in value["issues"]:
        if not isinstance(issue, dict) or any(field not in issue for field in ISSUE_FIELDS):
            return _result("invalid")
        if not isinstance(issue["severity"], str) or issue["severity"] not in SEVERITIES:
            return _result("invalid")
        if any(not isinstance(issue[field], str) for field in ISSUE_FIELDS[1:]):
            return _result("invalid")
        if not _normalise(issue["problem"]) or not _normalise(issue["evidence"]):
            return _result("invalid")
        clean_issues.append({field: issue[field] for field in ISSUE_FIELDS})

    if verdict == "abstain":
        return _result("abstained", verdict)

    diff_hunks = _diff_hunks(diff)
    diff_lines = [line for hunk in diff_hunks for line in hunk]
    supported = []
    unsupported = 0
    identical_fix = 0
    existing_lines = set(diff_lines)
    for issue in clean_issues:
        evidence = _normalise(issue["evidence"])
        if not _evidence_is_supported(evidence, diff_hunks):
            unsupported += 1
            continue
        fix = _normalise(issue["fix"])
        if fix and (fix == evidence or fix in existing_lines):  # an empty fix is just "no suggestion"
            identical_fix += 1
            continue
        supported.append(issue)
    return _result("valid", verdict, supported, unsupported, identical_fix)
