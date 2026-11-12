"""Paid-model budget: rules, admission checks, prompt hygiene and usage (docs/SPEC-paid-budget.md).

The pure helpers (validation, matching, effort, hygiene, limit state, agreement) take plain values so tests and
the API share one definition. The DB helpers read the latest rules and settled tasks.

Admission never approves, unblocks or widens anything: it can only queue, reroute or refuse a task.
"""
import copy
import datetime
import json
import re
import time

from store import db

# "test" (docs/SPEC-live-monitor.md 6): a test pass on someone else's build; never seeded for Fable (CLAUDE.md rule 0)
KINDS = ("build", "fix", "review", "re_review", "final_review", "security_review", "merge", "benchmark", "check",
         "draft", "test")
REVIEW_KINDS = {"review", "re_review", "final_review", "security_review"}
EFFORTS = ("low", "medium", "high")
TIERS = ("cheap", "standard", "premium", "scarce")
PAID_AGENTS = {"claude", "codex"}
KEY_AGENTS = {"claude", "codex", "ollama"}
STAGES = ("local", "paid")
TOOL_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(\(.{0,200}\))?$")
CONCRETE_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/:-]{0,127}$")
GLOB_BAD = set("[]{}\\\x00")

DEFAULT_RULES = {
    "models": {
        "codex": {"tier": "scarce", "max_concurrent": 1, "kinds": ["final_review", "security_review", "build"],
                  "default_effort": {"final_review": "high", "security_review": "high", "build": "high", "*": "medium"},
                  "daily_tokens_soft": 2000000, "daily_tokens_hard": None},
        "claude:claude-opus-*": {"tier": "premium", "max_concurrent": 1,
                                 "kinds": ["build", "final_review", "security_review", "review", "test"]},
        "claude:claude-fable-*": {"tier": "premium", "max_concurrent": 2,
                                  "kinds": ["review", "re_review", "final_review", "security_review"]},
        "claude:claude-sonnet-*": {"tier": "standard", "max_concurrent": 2, "kinds": ["*"]},
        "claude:claude-haiku-*": {"tier": "cheap", "max_concurrent": 2, "kinds": ["*"]},
        # A Claude run whose model is unknown (no model named, no single default reported) and any Claude model
        # not listed above: governed conservatively rather than left outside the limit breaker and concurrency.
        "claude:*": {"tier": "standard", "max_concurrent": 2, "kinds": ["*"]},
    },
    "unlisted": "allow",
    "prompt": {"max_kb": 40, "hard_max_kb": 120, "max_line_chars": 1000,
               "exclude_globs": ["master/controller/ui/dist/**", "**/*.min.js", "**/*.bundle.js",
                                 "**/package-lock.json", "evals/results/*.json"]},
    "limits": {"on_limit": "queue",
               "fallbacks": {"codex": ["claude:claude-fable-5-2"], "claude:claude-opus-*": ["claude:claude-sonnet-5-5"]},
               # A task that stops at a usage limit (docs/SPEC-limit-handoff.md 4.2): hand its work to another slot,
               # or wait for the reset. By kind; "*" covers the rest.
               "on_task_limit": {"build": "handoff", "fix": "handoff", "*": "wait"},
               "max_handoffs": 2, "allow_cross_vendor": False,
               # A stalled task (docs/SPEC-stall-reroute.md 12.7, 12.10): reroute its work, or leave it for the
               # owner. By kind; "*" covers the rest. At most max_stall_handoffs stall reroutes per work item,
               # counted apart from limit handoffs; the next stall escalates.
               "on_task_stall": {"*": "reroute"}, "max_stall_handoffs": 2},
    "effort_ladder": {"build": ["medium", "medium", "high"], "fix": ["low", "medium", "high"], "re_review": ["low", "medium"]},
    # "auto": a Claude limit holds the whole account (every model shares it), Codex limits stay per model.
    "limit_scope": "auto",
    "review_exec": {"tools": ["Read", "Grep", "Glob"], "max_turns": 8, "task_minutes": 15},
    "reviews": {"stages": ["local", "paid"], "max_paid_rounds": 2, "max_codex_rounds": 1, "paid_skip_when": None,
                "security_paths": ["master/controller/auth.py", "master/controller/policy.py", "nodes/runner.py",
                                   "master/mcp/**"]},
}

# Used for a paid agent whose model can't be resolved when the stored rules have no `agent:*` or bare-agent rule.
FALLBACK_ENTRY = {"tier": "standard", "max_concurrent": 2, "kinds": ["*"]}

TOP_KEYS = set(DEFAULT_RULES)
MODEL_FIELDS = {"tier", "max_concurrent", "kinds", "default_effort", "daily_tokens_soft", "daily_tokens_hard"}


class Refusal(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


# ---------- globs ----------

def valid_glob(pattern) -> bool:
    return (isinstance(pattern, str) and 0 < len(pattern) <= 200 and not pattern.startswith("/")
            and ".." not in pattern.split("/") and not (set(pattern) & GLOB_BAD))


def _glob_re(pattern: str) -> re.Pattern:
    out, i = "", 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out, i = out + "(?:.*/)?", i + 3
        elif pattern.startswith("**", i):
            out, i = out + ".*", i + 2
        elif pattern[i] == "*":
            out, i = out + "[^/]*", i + 1
        elif pattern[i] == "?":
            out, i = out + "[^/]", i + 1
        else:
            out, i = out + re.escape(pattern[i]), i + 1
    return re.compile(out + r"\Z")


def glob_match(pattern: str, path: str) -> bool:
    return bool(_glob_re(pattern).match(path))


def touches(files: list[str], globs: list[str]) -> bool:
    return any(glob_match(g, f) for f in files for g in globs)


# ---------- validation ----------

def _int(value, low: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= low


def _number(value, low: float, strict: bool = True) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return value > low if strict else value >= low


def _unknown(value: dict, allowed: set, label: str) -> None:
    extra = sorted(set(value) - allowed)
    if extra:
        raise ValueError(f"unknown {label} key: {extra[0]}")


def valid_key(key) -> bool:
    if not isinstance(key, str):
        return False
    agent, sep, model = key.partition(":")
    if agent not in KEY_AGENTS:
        return False
    return not sep or (bool(model) and len(model) <= 128 and not (set(model) & GLOB_BAD) and " " not in model)


def validate_rules(body) -> dict:
    """Check every field of a rules body; raise ValueError naming the first problem. Returns the body."""
    if not isinstance(body, dict):
        raise ValueError("rules must be an object")
    _unknown(body, TOP_KEYS, "rules")
    missing = sorted(TOP_KEYS - set(body))
    if missing:
        raise ValueError(f"rules are missing {missing[0]}")
    models = body["models"]
    if not isinstance(models, dict):
        raise ValueError("models must be an object")
    for key, entry in models.items():
        if not valid_key(key):
            raise ValueError(f"bad model key {key!r}: use an agent (claude, codex, ollama) or agent:model-glob")
        if not isinstance(entry, dict):
            raise ValueError(f"models.{key} must be an object")
        _unknown(entry, MODEL_FIELDS, f"models.{key}")
        if entry.get("tier") not in TIERS:
            raise ValueError(f"models.{key}.tier must be one of {list(TIERS)}")
        if "max_concurrent" in entry and not _int(entry["max_concurrent"], 1):
            raise ValueError(f"models.{key}.max_concurrent must be a positive integer")
        kinds = entry.get("kinds", ["*"])
        if not isinstance(kinds, list) or any(k != "*" and k not in KINDS for k in kinds):
            raise ValueError(f"models.{key}.kinds must list task kinds or '*'")
        effort = entry.get("default_effort", {})
        if not isinstance(effort, dict) or any((k != "*" and k not in KINDS) or v not in EFFORTS
                                               for k, v in effort.items()):
            raise ValueError(f"models.{key}.default_effort maps kinds to low, medium or high")
        for field in ("daily_tokens_soft", "daily_tokens_hard"):
            if entry.get(field) is not None and not _int(entry[field], 0):
                raise ValueError(f"models.{key}.{field} must be a non-negative integer or null")
    if body["unlisted"] not in ("allow", "refuse"):
        raise ValueError("unlisted must be allow or refuse")
    prompt = body["prompt"]
    if not isinstance(prompt, dict):
        raise ValueError("prompt must be an object")
    _unknown(prompt, {"max_kb", "hard_max_kb", "max_line_chars", "exclude_globs"}, "prompt")
    if not _number(prompt.get("max_kb"), 0) or not _number(prompt.get("hard_max_kb"), 0):
        raise ValueError("prompt.max_kb and prompt.hard_max_kb must be positive numbers")
    if prompt["hard_max_kb"] < prompt["max_kb"]:
        raise ValueError("prompt.hard_max_kb must be at least prompt.max_kb")
    if not _int(prompt.get("max_line_chars"), 80):
        raise ValueError("prompt.max_line_chars must be an integer of at least 80")
    if not isinstance(prompt.get("exclude_globs"), list) or not all(valid_glob(g) for g in prompt["exclude_globs"]):
        raise ValueError("prompt.exclude_globs must be relative globs (no '..', no leading '/', no [ ] { })")
    limits = body["limits"]
    if not isinstance(limits, dict):
        raise ValueError("limits must be an object")
    _unknown(limits, {"on_limit", "fallbacks", "on_task_limit", "max_handoffs", "allow_cross_vendor", "on_task_stall",
                      "max_stall_handoffs"}, "limits")
    on_stall = limits.get("on_task_stall", {})
    if not isinstance(on_stall, dict) or any((k != "*" and k not in KINDS) or v not in ("reroute", "leave")
                                             for k, v in on_stall.items()):
        raise ValueError("limits.on_task_stall maps task kinds (or '*') to reroute or leave")
    if "max_stall_handoffs" in limits and not _int(limits["max_stall_handoffs"], 0):
        raise ValueError("limits.max_stall_handoffs must be a non-negative integer")
    if limits.get("on_limit") not in ("queue", "fallback", "refuse"):
        raise ValueError("limits.on_limit must be queue, fallback or refuse")
    on_task = limits.get("on_task_limit", {})
    if not isinstance(on_task, dict) or any((k != "*" and k not in KINDS) or v not in ("handoff", "wait")
                                            for k, v in on_task.items()):
        raise ValueError("limits.on_task_limit maps task kinds (or '*') to handoff or wait")
    if "max_handoffs" in limits and not _int(limits["max_handoffs"], 0):
        raise ValueError("limits.max_handoffs must be a non-negative integer")
    if "allow_cross_vendor" in limits and not isinstance(limits["allow_cross_vendor"], bool):
        raise ValueError("limits.allow_cross_vendor must be true or false")
    fallbacks = limits.get("fallbacks", {})
    if not isinstance(fallbacks, dict):
        raise ValueError("limits.fallbacks must be an object")
    for source, targets in fallbacks.items():
        if not valid_key(source) or not isinstance(targets, list):
            raise ValueError(f"limits.fallbacks.{source} must be a list")
        for target in targets:
            agent, sep, model = target.partition(":") if isinstance(target, str) else ("", "", "")
            if agent not in PAID_AGENTS or not sep or not CONCRETE_MODEL_RE.fullmatch(model or ""):
                raise ValueError(f"fallback {target!r} must name one concrete model, e.g. claude:claude-sonnet-5-5")
    ladder = body["effort_ladder"]
    if not isinstance(ladder, dict) or any(k not in KINDS or not isinstance(v, list) or not v
                                           or any(e not in EFFORTS for e in v) for k, v in ladder.items()):
        raise ValueError("effort_ladder maps kinds to non-empty lists of low, medium or high")
    if body["limit_scope"] not in ("model", "account", "auto"):
        raise ValueError("limit_scope must be auto, model or account")
    review = body["review_exec"]
    if not isinstance(review, dict):
        raise ValueError("review_exec must be an object")
    _unknown(review, {"tools", "max_turns", "task_minutes"}, "review_exec")
    if not isinstance(review.get("tools"), list) or not all(isinstance(t, str) and TOOL_RE.fullmatch(t)
                                                            for t in review["tools"]):
        raise ValueError("review_exec.tools must be tool names")
    if review.get("max_turns") is not None and not _int(review["max_turns"], 1):
        raise ValueError("review_exec.max_turns must be a positive integer")
    if review.get("task_minutes") is not None and not _number(review["task_minutes"], 0):
        raise ValueError("review_exec.task_minutes must be a positive number")
    reviews = body["reviews"]
    if not isinstance(reviews, dict):
        raise ValueError("reviews must be an object")
    _unknown(reviews, {"stages", "max_paid_rounds", "max_codex_rounds", "paid_skip_when", "security_paths"}, "reviews")
    stages = reviews.get("stages")
    if not isinstance(stages, list) or any(s not in STAGES for s in stages) or len(set(stages)) != len(stages):
        raise ValueError("reviews.stages must list local and/or paid")
    for field in ("max_paid_rounds", "max_codex_rounds"):
        if not _int(reviews.get(field), 0):
            raise ValueError(f"reviews.{field} must be a non-negative integer")
    if not isinstance(reviews.get("security_paths"), list) or not all(valid_glob(g) for g in reviews["security_paths"]):
        raise ValueError("reviews.security_paths must be relative globs")
    skip = reviews.get("paid_skip_when")
    if skip is not None:
        if not isinstance(skip, dict):
            raise ValueError("reviews.paid_skip_when must be an object or null")
        _unknown(skip, {"max_changed_lines", "paths_only"}, "reviews.paid_skip_when")
        if "max_changed_lines" in skip and not _int(skip["max_changed_lines"], 0):
            raise ValueError("reviews.paid_skip_when.max_changed_lines must be a non-negative integer")
        if "paths_only" in skip and (not isinstance(skip["paths_only"], list)
                                     or not all(valid_glob(g) for g in skip["paths_only"])):
            raise ValueError("reviews.paid_skip_when.paths_only must be relative globs")
    return body


# ---------- matching and effort ----------

def match_key(rules: dict, agent: str, model: str | None) -> str | None:
    """The rules key for (agent, model): exact beats a glob, a longer glob beats a shorter one, an agent key is
    last. A task with no model only matches `agent:*` or the bare agent key."""
    best, best_score = None, -1
    for key in rules.get("models", {}):
        k_agent, sep, glob = key.partition(":")
        if k_agent != agent:
            continue
        if not sep:
            score = 0
        elif model is None:
            if glob != "*":
                continue
            score = 1
        elif not _glob_re(glob).match(model):
            continue
        elif "*" not in glob and "?" not in glob:
            score = 10_000
        else:
            score = 1 + len(glob.replace("*", "").replace("?", ""))
        if score > best_score:
            best, best_score = key, score
    return best


ORIGIN_KINDS = ("review", "final_review", "security_review")


def kind_allowed(entry: dict, kind: str, origin: str | None = None) -> bool:
    """Whether the entry's `kinds` admit this task kind. A delta `re_review` is also admitted against the review
    it continues (`origin`: review, final_review or security_review) when the entry lists that kind; with no
    proven origin only an explicit listing of re_review (or "*") counts."""
    kinds = entry.get("kinds", ["*"])
    if "*" in kinds or kind in kinds:
        return True
    return kind == "re_review" and origin in ORIGIN_KINDS and origin in kinds


def fallback_key(agent: str) -> str:
    return f"{agent}:*"


def entry_for(rules: dict, key: str | None, agent: str | None = None) -> dict:
    """The rules entry for a key; the conservative fallback for a paid agent's own `agent:*` key with no rule."""
    entry = rules.get("models", {}).get(key) if key else None
    if entry is not None:
        return entry
    return FALLBACK_ENTRY if key and agent in PAID_AGENTS and key == fallback_key(agent) else {}


def cheapest_for(rules: dict, kind: str, exclude: str | None = None) -> str | None:
    ranked = sorted(rules.get("models", {}).items(),
                    key=lambda item: (TIERS.index(item[1].get("tier", "scarce")), item[0].endswith(":*"), item[0]))
    for key, entry in ranked:
        if key != exclude and kind_allowed(entry, kind):
            return key
    return None


def resolve_effort(rules: dict, key: str | None, kind: str, explicit: str | None, failures: int) -> str | None:
    """Explicit effort wins; then the ladder for this kind (by failed gate rounds); then the model's default."""
    if explicit:
        return explicit
    ladder = (rules.get("effort_ladder") or {}).get(kind)
    if ladder:
        return ladder[min(max(int(failures or 0), 0), len(ladder) - 1)]
    if key is None or key not in rules.get("models", {}):
        return None
    defaults = rules["models"][key].get("default_effort") or {}
    return defaults.get(kind) or defaults.get("*")


def identity(agent: str, model: str | None) -> str:
    return f"{agent}:{model}" if model else agent


def label(key_or_identity: str | None) -> str:
    return key_or_identity or "this model"


# ---------- hygiene ----------

_DIFF_RE = re.compile(r"^diff --git ", re.MULTILINE)
_PATH_RE = re.compile(r'^diff --git "?a/(.*?)"? "?b/(.*?)"?\s*$')


def has_diff(text: str) -> bool:
    return bool(_DIFF_RE.search(text or ""))


def diff_sections(text: str) -> tuple[str, list[str]]:
    """(text before the first `diff --git`, one string per file section). Joined back they equal `text`."""
    starts = [m.start() for m in _DIFF_RE.finditer(text or "")]
    if not starts:
        return text or "", []
    sections = [text[s:e] for s, e in zip(starts, starts[1:] + [len(text)])]
    return text[:starts[0]], sections


def section_path(section: str) -> str:
    first = section.split("\n", 1)[0]
    match = _PATH_RE.match(first)
    return match.group(2) if match else ""


def changed_files(diff: str) -> list[str]:
    return [p for p in (section_path(s) for s in diff_sections(diff)[1]) if p]


def changed_lines(diff: str) -> int:
    return sum(1 for line in (diff or "").splitlines()
               if (line.startswith("+") and not line.startswith("+++")) or (line.startswith("-") and not line.startswith("---")))


def strip_prompt(text: str, prompt_rules: dict) -> dict:
    """Drop diff sections of generated files and replace over-long added lines. Kept sections are unchanged
    except for those lines. Returns {text, stripped_bytes, omitted, long_lines}."""
    if not has_diff(text):
        return {"text": text, "stripped_bytes": 0, "omitted": [], "long_lines": []}
    globs = prompt_rules.get("exclude_globs") or []
    max_line = int(prompt_rules.get("max_line_chars") or 1000)
    preamble, sections = diff_sections(text)
    out, omitted, long_lines = [preamble], [], []
    for sec in sections:
        path = section_path(sec)
        if path and any(glob_match(g, path) for g in globs):
            omitted.append(path)
            out.append(f"[omitted generated file {path}]\n")
            continue
        changed, lines = False, []
        for line in sec.splitlines(keepends=True):
            body = line.rstrip("\r\n")
            if body.startswith("+") and not body.startswith("+++") and len(body) > max_line:
                lines.append(f"[line omitted: {len(body)} chars]" + line[len(body):])
                changed = True
            else:
                lines.append(line)
        if changed:
            long_lines.append(path or "(unnamed file)")
        out.append("".join(lines))
    new = "".join(out)
    return {"text": new, "stripped_bytes": max(0, len(text.encode()) - len(new.encode())),
            "omitted": omitted, "long_lines": long_lines}


def chunk_sections(sections: list[str], max_bytes: int) -> list[str]:
    """Pack whole-file sections into chunks of at most max_bytes (a single bigger section stays whole)."""
    chunks, current = [], ""
    for sec in sections:
        if current and len((current + sec).encode()) > max_bytes:
            chunks.append(current)
            current = ""
        current += sec
        if len(current.encode()) > max_bytes:
            chunks.append(current)
            current = ""
    if current:
        chunks.append(current)
    return chunks


def split_prompt(text: str, max_bytes: int) -> list[str]:
    """Split a diff-bearing prompt by file into prompts of at most max_bytes, each with the same preamble."""
    preamble, sections = diff_sections(text)
    if not sections:
        return [text]
    room = max(1, max_bytes - len(preamble.encode()))
    return [preamble + chunk for chunk in chunk_sections(sections, room)]


def paid_skip_ok(reviews: dict, diff: str, gate_passed: bool, trusted_approved: bool) -> bool:
    """Opt-in rule (off by default): a small diff in the listed paths, gate passed, trusted local approval, no
    security path touched."""
    rule = reviews.get("paid_skip_when")
    if not rule or not gate_passed or not trusted_approved:
        return False
    files = changed_files(diff)
    if touches(files, reviews.get("security_paths") or []):
        return False
    cap = rule.get("max_changed_lines")
    if cap is not None and changed_lines(diff) > cap:
        return False
    paths = rule.get("paths_only")
    return paths is None or all(any(glob_match(g, f) for g in paths) for f in files)


# ---------- limits ----------

def _succeeded(row: dict) -> bool:
    state = row.get("state")
    return state in ("review", "done") if state is not None else row.get("failure_reason") is None


def limit_state(rows: list[dict], now: float, scope: str = "auto", rules: dict | None = None) -> dict:
    """{"models": {identity: until}, "accounts": {agent: until}} from settled task rows.

    A model is limited while one of its tasks failed with a limit whose reset is still ahead, unless a task of
    that model settled successfully after it. A later cancel, block or other failure proves nothing about the
    limit, so it never clears one. With scope "model", two different models of one agent limited at once mean the
    whole account is limited until the later reset; with "account" any limit does. Given `rules`, "two different
    models" means two different rules keys: two versions of one model are one model.
    """
    last_success: dict[str, float] = {}
    limits: dict[str, list[tuple[float, float]]] = {}
    for row in rows:
        ended = row.get("ended_at")
        if ended is None:
            continue
        ident = row.get("budget_identity") or identity(row.get("agent") or "", row.get("model"))
        resets = row.get("resets_at")
        if _succeeded(row):
            last_success[ident] = max(last_success.get(ident, ended), ended)
        elif row.get("failure_reason") == "limit" and resets and resets > now:
            limits.setdefault(ident, []).append((ended, resets))
    models = {}
    for ident, found in limits.items():
        after = last_success.get(ident)
        live = [resets for ended, resets in found if after is None or ended > after]
        if live:
            models[ident] = max(live)
    accounts: dict[str, float] = {}
    by_agent: dict[str, dict[str, float]] = {}
    for ident, until in models.items():
        agent, _, model = ident.partition(":")
        distinct = (match_key(rules, agent, model or None) or ident) if rules is not None else ident
        known = by_agent.setdefault(agent, {})
        known[distinct] = max(until, known.get(distinct, 0))
    for agent, untils in by_agent.items():
        if scope == "account" or (scope == "auto" and agent == "claude") or len(untils) >= 2:
            accounts[agent] = max(untils.values())
    return {"models": models, "accounts": accounts}


def _limit(state: dict, agent: str, ident: str | None, rules: dict | None, key: str | None):
    """(until, scope, subject): the one place a limit is read (Part F 6). `scope` is "account" (the whole agent
    account is limited: limit_scope "account", limit_scope "auto" for Claude (its models share one account limit),
    or two limited models of one agent) or "model". A model is limited
    when a limited identity is its own, maps to the same rules key (when `rules` is given), or is the bare agent
    (a run whose model was unknown); a run whose model is unknown (ident == agent) is held while any model is."""
    if agent in state["accounts"]:
        return state["accounts"][agent], "account", f"{agent} account"
    if key is None and rules is not None and ident:
        key = match_key(rules, *(ident.split(":", 1) if ":" in ident else (ident, None)))
    found: list[tuple[float, str]] = []
    for other, until in state["models"].items():
        other_agent, _, other_model = other.partition(":")
        if other_agent != agent:
            continue
        if (other == agent or other == ident or ident == agent
                or (key is not None and rules is not None
                    and match_key(rules, other_agent, other_model or None) == key)):
            found.append((until, other))
    if not found:
        return None, None, None
    until, other = max(found)
    if ident in state["models"] and ident != agent:
        subject = ident
    else:
        subject = agent if (ident == agent or other == agent) else (key or other)
    return until, "model", subject


limit_detail = _limit  # (until, scope, subject): which model (or the account) a limit is on


def limit_for(rules: dict, state: dict, agent: str, ident: str | None, key: str | None = None
              ) -> tuple[float | None, str | None]:
    """(limited-until, scope) for one model key on one account; `state` is limit_snapshot(rules, now, account).
    Budget status, fleet, resources, live and admission all read limits through this."""
    until, scope, _subject = _limit(state, agent, ident, rules, key)
    return until, scope


def limited_for(state: dict, agent: str, ident: str, rules: dict | None = None,
                key: str | None = None) -> tuple[float | None, str | None]:
    until, _scope, subject = _limit(state, agent, ident, rules, key)
    return (until, f"{subject} limited until {clock(until)}") if until else (None, None)


def clock(ts: float) -> str:
    moment = datetime.datetime.fromtimestamp(ts).astimezone()
    return f"{moment.hour % 12 or 12}:{moment.minute:02d} {'AM' if moment.hour < 12 else 'PM'}"


def local_midnight(now: float) -> float:
    moment = datetime.datetime.fromtimestamp(now).astimezone()
    return moment.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


# ---------- agreement (local hints vs the paid verdict) ----------

def _norm(text) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip().lower()


def hint_matches(hint: dict, issue: dict) -> bool:
    if _norm(hint.get("file")) != _norm(issue.get("file")):
        return False
    a, b = _norm(hint.get("evidence")), _norm(issue.get("evidence"))
    return bool(a and b and (a in b or b in a))


def agreement_rows(hints: list[dict], paid_issues: list[dict]) -> list[dict]:
    """One row per stage-1 finding (confirmed when a paid finding matches it) and one miss per paid finding that
    no stage-1 finding matched."""
    rows = [{"hint_id": i, "confirmed": any(hint_matches(h, p) for p in paid_issues)} for i, h in enumerate(hints)]
    rows += [{"hint_id": None, "confirmed": False} for p in paid_issues if not any(hint_matches(h, p) for h in hints)]
    return rows


# ---------- stored rules ----------

def seed_rules() -> None:
    """First start only: the owner's later edits are never overwritten."""
    with db() as c:
        if not c.execute("SELECT 1 FROM budget_rules LIMIT 1").fetchone():
            c.execute("INSERT INTO budget_rules (version, body, author, created_at) VALUES (1, ?, 'seed', ?)",
                      (json.dumps(DEFAULT_RULES), time.time()))
        # One-time: bodies stored while the default was "model" keep it forever through with_defaults, which left
        # Opus and Fable looking free after a Sonnet limit. On the first run of this check, move the latest body to
        # "auto" if it says "model". Either way a marker version (author migration:limit_scope, the body as it then
        # stands) is written, so the check never runs again and an owner's later "model" choice is kept on restart.
        latest = c.execute("SELECT version, body FROM budget_rules ORDER BY version DESC LIMIT 1").fetchone()
        if latest and not c.execute("SELECT 1 FROM budget_rules WHERE author='migration:limit_scope'").fetchone():
            try:
                body = json.loads(latest["body"])
            except (ValueError, TypeError):
                body = None
            if isinstance(body, dict):
                if body.get("limit_scope") == "model":
                    body = body | {"limit_scope": "auto"}
                c.execute("INSERT INTO budget_rules (version, body, author, created_at) VALUES (?, ?, ?, ?)",
                          (latest["version"] + 1, json.dumps(body), "migration:limit_scope", time.time()))


def rules_row() -> dict:
    with db() as c:
        row = c.execute("SELECT * FROM budget_rules ORDER BY version DESC LIMIT 1").fetchone()
    if not row:
        return {"version": 0, "body": copy.deepcopy(DEFAULT_RULES), "author": None, "created_at": None}
    return dict(row) | {"body": json.loads(row["body"])}


def with_defaults(body) -> dict:
    """A stored body with any missing top-level key, or missing key inside a settings section, taken from the
    defaults. The owner's models and effort ladders (and every value that is present) are kept as stored, so a
    model or ladder the owner removed is not added back."""
    if not isinstance(body, dict):
        return copy.deepcopy(DEFAULT_RULES)
    out = copy.deepcopy(body)
    for key, default in DEFAULT_RULES.items():
        if key not in out:
            out[key] = copy.deepcopy(default)
        elif isinstance(default, dict) and isinstance(out[key], dict) and key not in ("models", "effort_ladder"):
            for sub, value in default.items():
                out[key].setdefault(sub, copy.deepcopy(value))
    return out


_reported_invalid: set[int] = set()


def resolve_rules() -> tuple[dict, str | None]:
    """(rules in force, why the stored rules aren't in force or None). Missing keys are filled from the defaults
    first; a body that still doesn't validate means the defaults are in force, and a budget.rules_invalid event
    says so once per stored version."""
    row = rules_row()
    try:
        return validate_rules(with_defaults(row["body"])), None
    except ValueError as exc:
        if row["version"] not in _reported_invalid:
            _reported_invalid.add(row["version"])
            from events import emit
            emit(None, "budget.rules_invalid", {"version": row["version"], "error": str(exc)})
        return copy.deepcopy(DEFAULT_RULES), str(exc)  # a stored body that no longer validates never disables the checks


def current_rules() -> dict:
    return resolve_rules()[0]


def rules_history() -> list[dict]:
    with db() as c:
        rows = c.execute("SELECT * FROM budget_rules ORDER BY version DESC").fetchall()
    return [dict(r) | {"body": json.loads(r["body"])} for r in rows]


def save_rules(body: dict, author: str) -> dict:
    validate_rules(body)
    now = time.time()
    with db() as c:
        c.execute("BEGIN IMMEDIATE")
        version = c.execute("SELECT COALESCE(MAX(version),0)+1 FROM budget_rules").fetchone()[0]
        c.execute("INSERT INTO budget_rules (version, body, author, created_at) VALUES (?,?,?,?)",
                  (version, json.dumps(body), author, now))
    return {"version": version, "body": body, "author": author, "created_at": now}


# ---------- live state from the database ----------

def account_key(agent: str, node_id: str | None, profile: str | None, label: str | None) -> str:
    """The account behind a slot (docs/SPEC-limit-handoff.md 3.2): the owner's label, shared across profiles and
    nodes, or (agent, node, profile) when the profile has no label."""
    if label:
        return f"{agent}/{label}"
    return f"{agent}@{node_id or 'master'}/{profile or 'fleet'}"


# The same account key in SQL, for task rows (older rows, placed before accounts existed, use their node's default).
ACCOUNT_SQL = "COALESCE(account, agent || '@' || COALESCE(node_id, 'master') || '/fleet')"


def task_account(task: dict) -> str:
    return task.get("account") or account_key(task.get("agent") or "", task.get("node_id"), None, None)


PROFILE_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def clean_codex_rate_limits(value) -> list[dict] | None:
    """The runner's `codex_rate_limits`: a list of {profile, primary, secondary, seen_at}, one per Codex profile
    (account) that has a record, keeping only numbers and a valid profile id. None when nothing usable is left.
    Heartbeats can carry anything, so this is the only way the field is stored."""
    def number(x):
        return float(x) if isinstance(x, (int, float)) and not isinstance(x, bool) and x == x and x >= 0 else None

    if not isinstance(value, list):
        return None
    out = []
    for item in value[:16]:
        if not isinstance(item, dict) or not isinstance(item.get("profile"), str) \
                or not PROFILE_ID_RE.fullmatch(item["profile"]):
            continue
        entry: dict = {"profile": item["profile"]}
        for name in ("primary", "secondary"):
            w = item.get(name)
            used = number(w.get("used_percent")) if isinstance(w, dict) else None
            reset = number(w.get("resets_at")) if isinstance(w, dict) else None
            entry[name] = ({"used_percent": used, "resets_at": reset, "window_minutes": number(w.get("window_minutes"))}
                           if used is not None and reset is not None else None)
        if entry["primary"] or entry["secondary"]:
            entry["seen_at"] = number(item.get("seen_at"))
            out.append(entry)
    return out or None


def node_accounts(agent: str | None = None) -> dict[str, dict]:
    """Every account the approved runners report: {account: {"agent", "node", "profile", "label"}}. A runner that
    reports no profiles has the one `fleet` profile (account_key)."""
    with db() as c:
        rows = c.execute("SELECT id, runner FROM nodes WHERE status='approved' AND runner IS NOT NULL").fetchall()
    out: dict[str, dict] = {}
    for row in rows:
        try:
            runner = json.loads(row["runner"]) or {}
        except (ValueError, TypeError):
            continue
        profiles = runner.get("profiles") if isinstance(runner.get("profiles"), dict) else {}
        for name in sorted(PAID_AGENTS):
            if (agent and name != agent) or not (runner.get("agents") or {}).get(name):
                continue
            entries = profiles.get(name)
            if not isinstance(entries, list):
                entries = [{"id": "fleet", "label": None}]
            for entry in entries:
                if not isinstance(entry, dict) or not isinstance(entry.get("id"), str):
                    continue
                label = entry.get("label") if isinstance(entry.get("label"), str) else None
                out[account_key(name, row["id"], entry["id"], label)] = {
                    "agent": name, "node": row["id"], "profile": entry["id"], "label": label}
    return out


def codex_records() -> dict[str, dict]:
    """{account: its newest rate_limits record} from the approved runners (docs/SPEC-dashboard-truth.md 11). A
    record belongs to the account of the profile that wrote it (its label, else node and profile) and only limits
    that account."""
    with db() as c:
        rows = c.execute("SELECT id, runner FROM nodes WHERE status='approved' AND runner IS NOT NULL").fetchall()
    out: dict[str, dict] = {}
    for row in rows:
        try:
            runner = json.loads(row["runner"]) or {}
        except (ValueError, TypeError):
            continue
        labels = {e.get("id"): e.get("label") for e in ((runner.get("profiles") or {}).get("codex") or [])
                  if isinstance(e, dict)} if isinstance(runner.get("profiles"), dict) else {}
        for found in clean_codex_rate_limits(runner.get("codex_rate_limits")) or []:
            label = labels.get(found["profile"])
            account = account_key("codex", row["id"], found["profile"], label if isinstance(label, str) else None)
            if account not in out or (found["seen_at"] or 0) > (out[account]["seen_at"] or 0):
                out[account] = found
    return out


def rate_limit_until(record: dict, now: float) -> float | None:
    """When Codex is free again: the latest reset among windows that are used up and whose reset is still ahead."""
    ends = [w["resets_at"] for w in (record.get("primary"), record.get("secondary"))
            if w and w["used_percent"] >= 100 and w["resets_at"] > now]
    return max(ends) if ends else None


def limit_snapshot(rules: dict, now: float, account: str | None = None) -> dict:
    """The breaker reads what each run itself ended with (agent_usage), never the task's current state: cancelling,
    completing or resuming a limited task changes the task but not the fact that its run hit the limit.

    With `account`, only that account's runs count (plus older runs recorded before accounts, which hold every
    account of their agent). Without it, every run counts: the most conservative view, "any account limited"."""
    query = ("SELECT agent, model, identity AS budget_identity, outcome, resets_at, ts AS ended_at, account "
             "FROM agent_usage WHERE outcome IN ('ok','limit') AND agent IN ('claude','codex')")
    args: tuple = ()
    if account:
        query += " AND (account IS NULL OR account=?)"
        # The owner's reset (docs/SPEC-live-monitor.md 13 b): limits on this account up to it no longer count, for
        # every identity, including older rows that hold every account. A newer limit counts again.
        query += (" AND NOT (outcome='limit' AND ts <= COALESCE((SELECT MAX(at) FROM limit_resets "
                  "WHERE limit_resets.account=?), 0))")
        args = (account, account)
    else:
        query += (" AND NOT (outcome='limit' AND account IS NOT NULL AND ts <= COALESCE((SELECT MAX(at) FROM "
                  "limit_resets WHERE limit_resets.account=agent_usage.account), 0))")
    with db() as c:
        rows = [dict(r) | ({"state": "failed", "failure_reason": "limit"} if r["outcome"] == "limit"
                           else {"state": "review", "failure_reason": None})
                for r in c.execute(query, args)]
    # Codex's own rate_limits record (Part F 11) beats error text, per account: a limit message only holds that
    # account's Codex until its next record, which says what is really left.
    records = codex_records()
    rows = [r for r in rows if not (r["agent"] == "codex" and r["failure_reason"] == "limit"
                                    and (rec := records.get(r["account"] or account))
                                    and rec.get("seen_at") and r["ended_at"] <= rec["seen_at"])]
    state = limit_state(rows, now, rules.get("limit_scope", "auto"), rules)
    ends = [until for owner, rec in records.items() if (account is None or owner == account)
            and (until := rate_limit_until(rec, now))]
    scope = rules.get("limit_scope", "auto")
    for owner, rec in records.items():
        if account is not None and owner != account:
            continue
        until = rate_limit_until(rec, now)
        if not until:
            continue
        # Under "auto" Codex is limited per model: the record belongs to the model that account last ran
        # (it carries none itself). Only with no known model, or scope "account", is it account-wide.
        last = max((r for r in rows if r["agent"] == "codex" and r["model"] and (r["account"] or account) == owner),
                   key=lambda r: r["ended_at"] or 0, default=None)
        if scope == "auto" and last:
            ident = last.get("budget_identity") or identity("codex", last["model"])
            state["models"][ident] = max(until, state["models"].get(ident, 0))
        else:
            state["accounts"]["codex"] = max(until, state["accounts"].get("codex", 0))
    # Past the daily hard token budget, a model behaves as limited until local midnight.
    midnight = local_midnight(now) + 86400
    for key, entry in rules.get("models", {}).items():
        hard = entry.get("daily_tokens_hard")
        if hard is not None and today_tokens(rules, key, now) >= hard:
            state.setdefault("hard", {})[key] = midnight
    return state


def node_default_model(agent: str, node_id: str | None = None) -> str | None:
    """The default model the runner reports for this agent: the named node's when one is selected, otherwise
    the one every approved runner agrees on. None when it is unknown or the runners disagree."""
    with db() as c:
        if node_id:
            rows = c.execute("SELECT runner FROM nodes WHERE status='approved' AND runner IS NOT NULL AND id=?",
                             (node_id,)).fetchall()
        else:
            rows = c.execute("SELECT runner FROM nodes WHERE status='approved' AND runner IS NOT NULL").fetchall()
    found = set()
    for row in rows:
        try:
            runner = json.loads(row["runner"]) or {}
        except (ValueError, TypeError):
            continue
        if not (runner.get("agents") or {}).get(agent):
            continue
        model = (runner.get("default_models") or {}).get(agent)
        found.add(model if isinstance(model, str) and model else None)
    return found.pop() if len(found) == 1 else None


def today_tokens(rules: dict, key: str, now: float | None = None) -> int:
    since = local_midnight(time.time() if now is None else now)
    agent = key.split(":", 1)[0]
    with db() as c:
        rows = c.execute("SELECT agent, model, COALESCE(input,0)+COALESCE(output,0) AS tokens FROM agent_usage "
                         "WHERE ts>=? AND agent=?", (since, agent)).fetchall()
    return sum(r["tokens"] for r in rows if match_key(rules, r["agent"], r["model"]) == key)


# Continuations (resumes) admitted but not yet acknowledged by the runner: task id -> (rules key, account, node).
# They hold their slot from admission until the task is working (or the runner call failed), so two resumes
# admitted at once can't both take an account's last slot, nor a node's (docs/SPEC-limit-handoff.md 4.5). Unpaid
# tasks are reserved too (key None), for the node's count only.
RESERVED: dict[str, tuple[str | None, str | None, str | None]] = {}


def running_count(key: str, exclude: str | None = None, account: str | None = None) -> int:
    """Running tasks of this rules key; with `account`, only those on that account (concurrency is per account).
    Reserved continuations count as running."""
    query = "SELECT id FROM tasks WHERE budget_key=? AND state IN ('starting','working') AND id IS NOT ?"
    args: tuple = (key, exclude)
    if account:
        query += f" AND {ACCOUNT_SQL}=?"
        args += (account,)
    with db() as c:
        running = {r[0] for r in c.execute(query, args)}
    reserved = {task_id for task_id, (r_key, r_account, _node) in list(RESERVED.items())
                if r_key == key and task_id != exclude and (not account or r_account == account)}
    return len(running | reserved)


def log_decision(decision: str, reason: str, *, task_id: str | None = None, **data) -> None:
    from events import emit  # local import: events imports store only, but keep budget importable on its own
    emit(task_id, f"budget.{decision}", {"decision": decision, "reason": reason, **data})


def maybe_soft_notice(rules: dict, key: str, now: float) -> None:
    entry = rules["models"].get(key) or {}
    soft = entry.get("daily_tokens_soft")
    if soft is None:
        return
    used = today_tokens(rules, key, now)
    if used < soft:
        return
    day = datetime.datetime.fromtimestamp(now).astimezone().date().isoformat()
    with db() as c:
        seen = c.execute("SELECT 1 FROM events WHERE type='budget.soft' AND json_extract(data,'$.key')=? "
                         "AND json_extract(data,'$.day')=? LIMIT 1", (key, day)).fetchone()
    if not seen:
        from events import emit
        emit(None, "budget.soft", {"key": key, "day": day, "used": used, "soft": soft})


def blocked_by(rules: dict, state: dict, agent: str, key: str | None, ident: str) -> tuple[float | None, str | None]:
    until, why = limited_for(state, agent, ident, rules, key)
    if until:
        return until, why
    hard = (state.get("hard") or {}).get(key) if key else None
    if hard:
        return hard, f"{label(key)} daily token budget used; resumes after midnight"
    return None, None


def admit(*, agent: str, model: str | None, kind: str, effort: str | None, override: bool, failures: int = 0,
          now: float | None = None, node_id: str | None = None, origin_kind: str | None = None,
          accounts_for=None, reroute: bool = True, forbidden=None) -> dict:
    """Decide how a new task may run. Raises Refusal for a 400/409; otherwise returns the (possibly rerouted)
    agent/model, its rules key, the effort, the account it should run on, and whether it must wait (queued) and why.

    `accounts_for(agent)` lists the accounts that agent's task may use, in preference order (limits and concurrency
    are per account); without it there is one account and the whole fleet's state counts. `reroute=False` (a resume
    of an existing session) never moves the work to a fallback model. `forbidden` holds (agent, model) pairs a
    fallback may never be (model None: any model of that agent), e.g. a reviewer must not become a builder."""
    now = time.time() if now is None else now
    rules = current_rules()
    snapshots: dict = {}

    def snapshot(account):
        if account not in snapshots:
            snapshots[account] = limit_snapshot(rules, now, account)
        return snapshots[account]

    def accounts(of_agent):
        found = list(accounts_for(of_agent)) if accounts_for else []
        return found or [None]

    def place(of_agent, of_key, of_ident, of_entry):
        """(account, wait, reason) on the first account that isn't limited, preferring one with a free slot;
        None when every account is limited. Also returns the first limit reason seen."""
        chosen, first = None, (None, None)
        for account in accounts(of_agent):
            until, why = blocked_by(rules, snapshot(account), of_agent, of_key, of_ident)
            if until:
                first = first if first[0] else (until, why)
                continue
            maximum = of_entry.get("max_concurrent")
            if maximum is None or of_key is None or running_count(of_key, account=account) < maximum:
                return (account, None, None), first
            chosen = chosen or (account, "slot", f"waiting for a {label(of_key)} slot")
        return chosen, first
    paid = agent in PAID_AGENTS
    if kind == "check" and paid:
        raise Refusal(400, "checks never run on paid models; use a local model (agent ollama) for availability checks")
    resolved = model or (node_default_model(agent, node_id) if paid else None)
    key = match_key(rules, agent, resolved)
    if rule0_blocks(agent, resolved, kind) or rule0_blocks(agent, key, kind):
        raise Refusal(400, f"{identity(agent, resolved)} never runs {kind} tasks (CLAUDE.md rule 0: Fable gives "
                           "guidance only)")
    if paid and key is None and rules.get("unlisted") == "refuse":
        raise Refusal(400, f"{identity(agent, resolved)} is not in the budget rules, which refuse unlisted models")
    if paid and key is None and resolved is None:
        key = fallback_key(agent)  # an unresolved paid identity is always governed, never skipped
    entry = entry_for(rules, key, agent)
    if key and not override and not kind_allowed(entry, kind, origin_kind):
        alternative = cheapest_for(rules, kind, exclude=key)
        reserved = ", ".join(entry.get("kinds") or [])
        raise Refusal(400, f"{label(key)} is reserved for [{reserved}]"
                           + (f"; use {alternative} for {kind}" if alternative else f"; no model in the rules allows {kind}"))
    decision = {"agent": agent, "model": model, "key": key, "identity": identity(agent, resolved), "routed_from": None,
                "wait": None, "reason": None, "account": accounts(agent)[0]}
    if paid and key:  # "unlisted": "allow" means the rules don't govern a named model that has no rule
        placed, (until, why) = place(agent, key, decision["identity"], entry)
        if placed:
            decision.update(account=placed[0], wait=placed[1], reason=placed[2])
        else:
            on_limit = rules["limits"]["on_limit"]
            if on_limit == "refuse":
                raise Refusal(409, why)
            rerouted = False
            if on_limit == "fallback" and reroute:
                for target in (rules["limits"].get("fallbacks") or {}).get(key) or []:
                    f_agent, _, f_model = target.partition(":")
                    if forbidden and ((f_agent, None) in forbidden or (
                            f_agent, f_model or node_default_model(f_agent, node_id)) in forbidden):
                        continue
                    f_key = match_key(rules, f_agent, f_model)
                    if rule0_blocks(f_agent, f_model or node_default_model(f_agent, node_id), kind) or \
                            rule0_blocks(f_agent, f_key, kind):
                        continue  # rule 0: never reroute a build, fix or test onto Fable
                    if f_key and not kind_allowed(rules["models"][f_key], kind, origin_kind):
                        continue
                    f_entry = entry_for(rules, f_key, f_agent)
                    f_placed, _ = place(f_agent, f_key, identity(f_agent, f_model), f_entry)
                    if not f_placed:
                        continue
                    decision.update(agent=f_agent, model=f_model, key=f_key, identity=identity(f_agent, f_model),
                                    routed_from=decision["identity"], account=f_placed[0], wait=f_placed[1],
                                    reason=f_placed[2])
                    rerouted = True
                    break
            if not rerouted:
                decision.update(wait="limit", reason=why, until=until)
        if decision["key"]:
            maybe_soft_notice(rules, decision["key"], now)
    decision["effort"] = resolve_effort(rules, decision["key"], kind, effort, failures)
    if kind in REVIEW_KINDS:
        decision["review_exec"] = rules["review_exec"]
    return decision


def start_wait(task: dict, now: float | None = None) -> tuple[str | None, str | None]:
    """(kind, why) a queued paid task must keep waiting on the budget: kind "limit" (a limit or the daily hard
    budget) or "slot" (no free slot, or an older task waiting for the same slot goes first); (None, None) when
    the budget lets it start. Only tasks waiting for a slot hold a place in the queue ahead of it."""
    key = task.get("budget_key")
    if task.get("agent") not in PAID_AGENTS or not task.get("kind") or not key:
        return None, None
    now = time.time() if now is None else now
    rules = current_rules()
    ident = task.get("budget_identity") or identity(task["agent"], task.get("model"))
    account = task_account(task)
    until, why = blocked_by(rules, limit_snapshot(rules, now, account), task["agent"], key, ident)
    if until:
        return "limit", why
    maximum = entry_for(rules, key, task["agent"]).get("max_concurrent")
    if maximum is not None:
        with db() as c:
            older = c.execute("SELECT COUNT(*) FROM tasks WHERE budget_key=? AND state='queued' AND budget_wait='slot' "
                              f"AND {ACCOUNT_SQL}=? AND id!=? AND (created_at < ? OR (created_at = ? AND rowid < "
                              "(SELECT rowid FROM tasks WHERE id=?)))",
                              (key, account, task["id"], task["created_at"], task["created_at"], task["id"])).fetchone()[0]
        if running_count(key, task["id"], account) + older >= maximum:
            return "slot", f"waiting for a {label(key)} slot"
    return None, None


def on_task_limit(rules: dict, kind: str | None) -> str:
    """What a task of this kind does when it stops at a usage limit: "handoff" or "wait" (docs/SPEC-limit-handoff.md 4.2)."""
    table = (rules.get("limits") or {}).get("on_task_limit") or DEFAULT_RULES["limits"]["on_task_limit"]
    return table.get(kind or "draft") or table.get("*") or "wait"


def on_task_stall(rules: dict, kind: str | None) -> str:
    """What a stalled task of this kind does: "reroute" (the default for every kind, reviews included) or "leave"."""
    table = (rules.get("limits") or {}).get("on_task_stall") or DEFAULT_RULES["limits"]["on_task_stall"]
    return table.get(kind or "draft") or table.get("*") or "reroute"


# CLAUDE.md rule 0, hard-coded whatever the rules say: Fable never builds, fixes or tests.
RULE0_KINDS = {"build", "fix", "test"}


def rule0_blocks(agent: str | None, model_or_key: str | None, kind: str) -> bool:
    return kind in RULE0_KINDS and agent == "claude" and "fable" in (model_or_key or "").lower()


def admission(task: dict, now: float | None = None, *, effort: str | None = None) -> dict:
    """The admission function applied to more work on an existing task (a resume, a fix round): the same checks as
    a new task, on the task's own account and model (never rerouted). The owner's recorded budget_override on the
    task still covers its kind (it never bypasses a limit, a budget or concurrency). Returns
    {"outcome": "admit"|"defer"|"refuse", "until", "reason", "wait"} (docs/SPEC-limit-handoff.md 4.5)."""
    now = time.time() if now is None else now
    if task.get("agent") not in PAID_AGENTS:
        return {"outcome": "admit", "until": None, "reason": None, "wait": None}
    account = task_account(task)
    try:
        decision = admit(agent=task["agent"], model=task.get("model"), kind=task.get("kind") or "draft",
                         effort=effort, override=bool(task.get("budget_override")), now=now,
                         node_id=task.get("node_id"),
                         accounts_for=lambda _agent: [account], reroute=False)
    except Refusal as refusal:
        return {"outcome": "refuse", "until": None, "reason": refusal.detail, "wait": None}
    if decision["wait"] is None:
        # Others running on this account (not this task, which is settled) decide the slot.
        return {"outcome": "admit", "until": None, "reason": None, "wait": None}
    if decision["wait"] == "limit":
        until = decision.get("until")
        reason = decision["reason"]
        return {"outcome": "defer", "until": until, "reason": reason, "wait": "limit"}
    return {"outcome": "defer", "until": now + 60, "reason": decision["reason"], "wait": "slot"}


def start_block(task: dict, now: float | None = None) -> str | None:
    """Why a queued paid task must keep waiting (a limit, the daily hard budget or no free slot), else None."""
    return start_wait(task, now)[1]


def status(now: float | None = None) -> dict:
    """Per paid model: OK, limited or at its soft budget; running/max; today's tokens. Fleet tasks only."""
    now = time.time() if now is None else now
    rules, error = resolve_rules()
    state = limit_snapshot(rules, now)
    records = codex_records()
    accounts_by_agent = {a: node_accounts(a) for a in PAID_AGENTS}
    per_account: dict[str, dict] = {}
    out = []
    for key, entry in rules["models"].items():
        agent = key.split(":", 1)[0]
        if agent not in PAID_AGENTS:
            continue
        until, scope = limit_for(rules, state, agent, None, key)
        if not until and (state.get("hard") or {}).get(key):
            until, scope = state["hard"][key], "daily"
        used = today_tokens(rules, key, now)
        soft = entry.get("daily_tokens_soft")
        status_name = "limited" if until else ("soft" if soft is not None and used >= soft else "ok")
        rows = []
        for account in sorted(accounts_by_agent.get(agent) or {}):
            if account not in per_account:
                per_account[account] = limit_snapshot(rules, now, account)
            a_until, a_scope = limit_for(rules, per_account[account], agent, None, key)
            if not a_until and (per_account[account].get("hard") or {}).get(key):
                a_until, a_scope = per_account[account]["hard"][key], "daily"
            row = {"account": account, "node": accounts_by_agent[agent][account]["node"],
                   "label": accounts_by_agent[agent][account]["label"],
                   "state": "limited" if a_until else "ok", "limited_until": a_until, "limit_scope": a_scope,
                   "running": running_count(key, account=account)}
            if agent == "codex" and records.get(account):
                row["rate_limits"] = records[account]
            rows.append(row)
        out.append({"key": key, "agent": agent, "tier": entry.get("tier"), "state": status_name,
                    # limited_until / limit_scope: the aggregate, set when ANY account is limited
                    "limited_until": until, "limit_scope": scope, "any_account_limited_until": until,
                    "accounts": rows, "running": running_count(key),
                    "max_concurrent": entry.get("max_concurrent"), "today_tokens": used, "soft": soft,
                    "hard": entry.get("daily_tokens_hard")})
    return {"fleet_only": True, "models": out, "rules_version": rules_row()["version"],
            "rules_invalid": error is not None, "rules_error": error}
