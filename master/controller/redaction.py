"""Secret redaction shared by the MCP bridge and the controller (docs/SPEC-limit-handoff.md 4.3 step 5).

`redact` is the bridge's rule set (moved here from master/mcp/server.py unchanged). `redact(..., strict=True)` adds
the runner's key-like patterns, for text that leaves the fleet in another agent's prompt (a handoff report).
"""
import re

SECRET_PATTERNS = [
    re.compile(r"(?i)(api[_-]?key|secret|token|password|passwd)\s*[:=]\s*\S+"),
    re.compile(r"sk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]+?-----END [A-Z ]*PRIVATE KEY-----"),
]

STRICT_PATTERNS = [
    re.compile(r"\b(?:sk|pk|rk|ghp|gho|ghs|xox[a-z])[-_][A-Za-z0-9_-]{12,}"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+"),
    re.compile(r"(?i)\bauthorization\s*[:=]\s*\S+"),
    # A long key-like run: letters and digits mixed. Not a path (no "/"), not a 40-hex commit sha, which a handoff
    # report needs to stay useful.
    re.compile(r"\b(?![0-9a-f]{40}\b)(?=[A-Za-z0-9+_-]*\d)(?=[A-Za-z0-9+_-]*[A-Za-z])[A-Za-z0-9+_-]{40,}={0,2}"),
]


def redact(text: str, strict: bool = False) -> str:
    text = str(text or "")
    for pattern in SECRET_PATTERNS + (STRICT_PATTERNS if strict else []):
        text = pattern.sub("[REDACTED]", text)
    return text
