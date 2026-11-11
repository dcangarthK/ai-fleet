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
    "Evidence must be an exact quote from an added, removed, or context line. State the expected "
    "and actual behavior for each issue. A fix must differ from the evidence and from lines already "
    "in the diff. If there is no defensible issue, abstain."
)


def _normalise(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _diff_lines(diff: str) -> list[str]:
    """Return content from unified-diff added, removed, and context lines."""
    lines = []
    for line in diff.splitlines():
        if line.startswith(("+++", "---")):
            continue
        if line.startswith(("+", "-", " ")):
            lines.append(_normalise(line[1:]))
    return lines


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

    diff_lines = _diff_lines(diff)
    normal_diff = " ".join(line for line in diff_lines if line)
    supported = []
    unsupported = 0
    identical_fix = 0
    existing_lines = set(diff_lines)
    for issue in clean_issues:
        evidence = _normalise(issue["evidence"])
        if evidence not in normal_diff:
            unsupported += 1
            continue
        fix = _normalise(issue["fix"])
        if fix == evidence or fix in existing_lines:
            identical_fix += 1
            continue
        supported.append(issue)
    return _result("valid", verdict, supported, unsupported, identical_fix)
