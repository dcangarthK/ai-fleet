"""Pure-python evaluator for policy/tailnet-policy.hujson (SPEC-networking.md section 4).

Deny by default: a connection is allowed only if some grant matches source, destination and port.
Reads the file only; touches no network, firewall or Tailscale state.

Source kinds understood by `allowed`:
  tag:master, tag:llm-node  tagged devices (never match autogroup:member, as in Tailscale)
  admin                     a tailnet admin's device (also a member)
  member                    an ordinary owner tailnet device
  untagged-node             a new node owned by a member that has not been tagged yet (enrol-before-tag)
"""
import json
import pathlib

POLICY_PATH = pathlib.Path(__file__).with_name("tailnet-policy.hujson")


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
    return json.loads(strip_hujson(pathlib.Path(path).read_text()))


# which grant source selectors each source kind satisfies
_SRC_MATCH = {
    "tag:master": {"tag:master"},
    "tag:llm-node": {"tag:llm-node"},
    "admin": {"autogroup:admin", "autogroup:member"},
    "member": {"autogroup:member"},
    "untagged-node": {"autogroup:member"},
}


def _port_ok(specs, proto, port):
    # "*" and ranges are not understood: fail closed
    return f"{proto}:{port}" in specs


def allowed(policy, src, dst, port, proto="tcp"):
    """True only if a grant allows src -> dst:port. dst is a tag name; unknown src kinds are denied."""
    selectors = _SRC_MATCH.get(src)
    if not selectors:
        return False
    for g in policy.get("grants", []):
        if (selectors & set(g.get("src", []))
                and dst in g.get("dst", [])
                and _port_ok(g.get("ip", []), proto, port)):
            return True
    return False


def run_policy_tests(policy):
    """Evaluate the file's own "tests" block; returns a list of failure strings (empty = all pass)."""
    fails = []
    for t in policy.get("tests", []):
        src = t["src"]
        for key, want in (("accept", True), ("deny", False)):
            for target in t.get(key, []):
                dst, port = target.rsplit(":", 1)
                if allowed(policy, src, dst, int(port)) != want:
                    fails.append(f"{src} -> {target} expected {key}")
    return fails
