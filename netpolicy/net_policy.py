"""Pure-python evaluator for netpolicy/tailnet-policy.hujson (SPEC-networking.md section 4).

Deny by default: a connection is allowed only if some grant matches source, destination and port.
Reads the file only; touches no network, firewall or Tailscale state.

Source kinds understood by `allowed`:
  tag:master, tag:llm-node  tagged devices (never match autogroup:member, as in Tailscale)
  admin                     a tailnet admin's device (also a member)
  member                    an ordinary owner tailnet device
  untagged-node             a new node owned by a member that has not been tagged yet (enrol-before-tag)

Strict: anything this evaluator does not understand raises PolicyError instead of being ignored, so an
over-broad policy (wildcards, ranges, legacy acls, unknown keys) can never pass a deny test by being skipped.
Untagged-node reach is an ACL lookup only; it is not a simulation of the enrolment flow.
"""
import json
import pathlib
import re

POLICY_PATH = pathlib.Path(__file__).with_name("tailnet-policy.hujson")
TARGET_PATH = pathlib.Path(__file__).with_name("tailnet-policy-target.hujson")


class PolicyError(ValueError):
    """The policy uses something this evaluator does not model."""


def _strip_comments(text):
    out, i, n, in_str = [], 0, len(text), False
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 1
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
            out.append(c)
        elif text.startswith("//", i):
            while i < n and text[i] != "\n":
                i += 1
            continue
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = n if end < 0 else end + 2
            continue
        else:
            out.append(c)
        i += 1
    return "".join(out)


def _strip_trailing_commas(s):
    res, in_str, i = [], False, 0
    while i < len(s):
        c = s[i]
        if in_str:
            res.append(c)
            if c == "\\" and i + 1 < len(s):
                res.append(s[i + 1])
                i += 1
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
            res.append(c)
        elif c == ",":
            j = i + 1
            while j < len(s) and s[j].isspace():
                j += 1
            if j < len(s) and s[j] in "}]":
                i += 1
                continue
            res.append(c)
        else:
            res.append(c)
        i += 1
    return "".join(res)


def strip_hujson(text):
    """Remove // and /* */ comments (outside strings) and trailing commas, giving plain JSON."""
    return _strip_trailing_commas(_strip_comments(text))


def load_policy(path=POLICY_PATH):
    return validate(json.loads(strip_hujson(pathlib.Path(path).read_text())))




_TOP_KEYS = {"tagOwners", "grants", "tests"}
_GRANT_KEYS = {"src", "dst", "ip"}
_SRC_SELECTORS = {"tag:master", "tag:llm-node", "autogroup:member", "autogroup:admin"}
_DST_SELECTORS = {"tag:master", "tag:llm-node"}
_PORT_RE = re.compile(r"tcp:([0-9]{1,5})")  # used with fullmatch; ASCII digits only
_DIGITS_RE = re.compile(r"[0-9]{1,5}")


def _port_number(text):
    """ASCII-digit port in 1..65535 or None (so '2' superscript, '99999' and '' are all rejected)."""
    if not isinstance(text, str) or not _DIGITS_RE.fullmatch(text):
        return None
    n = int(text)
    return n if 1 <= n <= 65535 else None


def validate(policy):
    """Raise PolicyError on any key, selector or port form this evaluator does not model."""
    if not isinstance(policy, dict):
        raise PolicyError("policy must be an object")
    extra = set(policy) - _TOP_KEYS
    if extra:
        raise PolicyError(f"unsupported top-level key(s): {sorted(extra)}")
    for g in policy.get("grants", []):
        if not isinstance(g, dict) or set(g) != _GRANT_KEYS:
            raise PolicyError(f"grant must have exactly keys {sorted(_GRANT_KEYS)}: {g!r}")
        for s in g["src"]:
            if s not in _SRC_SELECTORS:
                raise PolicyError(f"unsupported src selector: {s!r}")
        for d in g["dst"]:
            if d not in _DST_SELECTORS:
                raise PolicyError(f"unsupported dst selector: {d!r}")
        for spec in g["ip"]:
            m = _PORT_RE.fullmatch(spec) if isinstance(spec, str) else None
            if not m or _port_number(m.group(1)) is None:
                raise PolicyError(f"unsupported ip/port form: {spec!r}")
    return policy


# which grant source selectors each source kind satisfies
_SRC_MATCH = {
    "tag:master": {"tag:master"},
    "tag:llm-node": {"tag:llm-node"},
    "admin": {"autogroup:admin", "autogroup:member"},
    "member": {"autogroup:member"},
    "untagged-node": {"autogroup:member"},
}


def allowed(policy, src, dst, port, proto="tcp"):
    """True only if a grant allows src -> dst:port.
    Raises PolicyError for an unknown src kind or policy form."""
    validate(policy)
    selectors = _SRC_MATCH.get(src)
    if selectors is None:
        raise PolicyError(f"unknown source kind: {src!r}")
    for g in policy.get("grants", []):
        if (selectors & set(g["src"]) and dst in g["dst"] and f"{proto}:{port}" in g["ip"]):
            return True
    return False


def run_policy_tests(policy):
    """Evaluate the file's own "tests" block; returns failure strings (empty = all pass).
    Raises PolicyError for a non-tag source (e.g. a user login): those rows are console-only."""
    fails = []
    for t in policy.get("tests", []):
        src = t["src"]
        if src not in ("tag:master", "tag:llm-node"):
            raise PolicyError(f"evaluator cannot run tests for source {src!r}; use the Tailscale console")
        for key, want in (("accept", True), ("deny", False)):
            for target in t.get(key, []):
                dst, _, port = target.rpartition(":")
                number = _port_number(port)
                if number is None:
                    raise PolicyError(f"bad test target (port not 1-65535): {target!r}")
                if allowed(policy, src, dst, number) != want:
                    fails.append(f"{src} -> {target} expected {key}")
    return fails
