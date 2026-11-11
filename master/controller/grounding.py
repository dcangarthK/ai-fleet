"""Pure validation and grounding for the fleet's shared reviewer contract."""

import json
import re


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
