"""Part I intelligent controller core (docs/SPEC-intelligent-controller.md sections 3-6).

Pure ranking, estimate, forecast and suggestion functions plus small helpers that take a sqlite connection.
No FastAPI import. Nothing here overrides a budget rule: `rank` only ever removes eligibility.
"""
from __future__ import annotations

import json
import statistics
import time
from datetime import datetime, timezone
from fnmatch import fnmatchcase

CANDIDATES = ["local:fast", "local:coder", "claude:claude-haiku-5-5", "claude:claude-sonnet-5-5",
              "claude:claude-opus-5-5", "codex"]  # never Fable: it is paid (docs/SPEC-token-gaps-2026-11-17.md G1)

KIND_PREFERENCE = {
    "build": ["claude:claude-sonnet-5-5", "codex"],
    "fix": ["claude:claude-sonnet-5-5", "codex"],
    "test": ["claude:claude-sonnet-5-5", "claude:claude-haiku-5-5"],
    "review": ["claude:claude-sonnet-5-5", "claude:claude-haiku-5-5"],
    "re_review": ["claude:claude-haiku-5-5", "claude:claude-sonnet-5-5"],
    "final_review": ["codex", "claude:claude-opus-5-5"],
    "security_review": ["codex", "claude:claude-opus-5-5"],
    "merge": ["claude:claude-haiku-5-5", "claude:claude-sonnet-5-5"],
    "benchmark": ["claude:claude-haiku-5-5", "claude:claude-sonnet-5-5"],
    "check": ["claude:claude-haiku-5-5", "claude:claude-sonnet-5-5"],
    "draft": ["local:coder", "local:fast", "claude:claude-haiku-5-5", "claude:claude-sonnet-5-5"],
    "lead": ["claude:claude-sonnet-5-5", "claude:claude-opus-5-5"],
}
PREF_POINTS = (40, 30, 20, 10)
FABLE_NEVER = {"build", "fix", "test", "merge", "benchmark", "draft", "check"}
CODEX_KINDS = {"final_review", "security_review", "build", "lead"}  # allowlist: Codex does nothing else
LOCAL_KINDS = {"draft", "check", "review"}
TIER_COST = {"cheap": 0, "standard": -5, "premium": -15, "scarce": -10}
DEFAULT_ESTIMATE = {"build": 400_000, "fix": 250_000, "test": 150_000, "review": 150_000, "re_review": 60_000,
                    "final_review": 250_000, "security_review": 250_000, "merge": 40_000, "benchmark": 80_000,
                    "check": 30_000, "draft": 60_000, "lead": 300_000}
DEFAULT_TOKENS = 100_000
TERMINAL = ("done", "failed", "cancelled")
WASTED_STATES = ("capped", "failed", "cancelled")
# 2026-11-16 20:00 SAST (UTC+2)
INTEL_EPOCH = datetime(2026, 11, 16, 18, 0, tzinfo=timezone.utc).timestamp()

SCHEMA = """CREATE TABLE IF NOT EXISTS intel_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, task_id TEXT, kind TEXT, action TEXT, chosen TEXT,
    candidates TEXT, reasons TEXT, est_tokens INTEGER, est_usd REAL)"""


def split_key(key: str) -> tuple[str, str | None]:
    agent, sep, model = key.partition(":")
    return agent, (model if sep else None)


def key_of(agent: str | None, model: str | None) -> str:
    """Candidate-style identity: `codex`, `claude:<model>`, `local:<role>`."""
    if not agent:
        return ""
    return f"{agent}:{model}" if model and agent != "codex" else agent


def _rule_for(rules: dict, key: str) -> dict | None:
    """The entry of the longest `rules["models"]` pattern matching key (fnmatch, as budget.match_key does)."""
    best, size = None, -1
    for pat, entry in (rules.get("models") or {}).items():
        if fnmatchcase(key, pat) and len(pat) > size:
            best, size = entry, len(pat)
    return best


def _kind_ok(entry: dict, kind: str) -> bool:
    kinds = entry.get("kinds", ["*"])
    return "*" in kinds or kind in kinds


# ---------- R1 ranking ----------

def _guard(agent: str, model: str | None, key: str, kind: str) -> str | None:
    """The hard model guard admission applies (budget.model_guard, no budget_override), so rank and admit agree."""
    import budget
    return budget.model_guard(agent, model, kind, None) or budget.model_guard(agent, key, kind, None)


def rank(kind: str, *, rules: dict, stats: dict, headroom: dict, load: dict, prompt_bytes: int = 0,
         exclude=(), history=(), allowed=None) -> list[dict]:
    """`allowed(key, kind)` decides rules eligibility for a candidate; callers pass one that tests the concrete
    identity the task will be spawned as (budget.match_key/kind_allowed), so ranking equals admission."""
    near = (rules.get("limits") or {}).get("near_limit_percent", 85)
    prefs = KIND_PREFERENCE.get(kind, [])
    now = time.time()
    out = []
    for key in CANDIDATES:
        agent, model = split_key(key)
        head = headroom.get(key) or {}
        used = head.get("used_percent")
        entry = _rule_for(rules, key)
        no, why, score = [], [], 0.0
        ok_by_rules = allowed(key, kind) if allowed else (entry is None or _kind_ok(entry, kind))
        if not ok_by_rules:
            no.append(f"{kind} not allowed for {key} by rules")
        guard = _guard(agent, model, key, kind)
        if guard:
            no.append(guard)
        if agent == "claude" and "fable" in (model or "") and kind in FABLE_NEVER:
            no.append(f"rule 0: Fable never does {kind}")
        if agent == "codex" and kind not in CODEX_KINDS:
            no.append(f"Codex only does {', '.join(sorted(CODEX_KINDS))}, not {kind}")
        if agent == "local":
            if kind not in LOCAL_KINDS:
                no.append(f"local roles only do draft, check, first-pass review, not {kind}")
            if not head.get("routed"):
                no.append(f"{key} not routed")
        if (head.get("limited_until") or 0) > now:
            no.append("limited until " + time.strftime("%H:%M", time.localtime(head["limited_until"])))
        if used is not None and used >= near:
            no.append(f"5h at {used:g}% (>= {near})")
        if key in exclude:
            no.append("excluded (independence: same as the builder)")
        if prefs and key in prefs[:4]:
            pts = PREF_POINTS[prefs.index(key)]
            score += pts
            why.append(f"preferred for {kind}" if prefs.index(key) == 0 else f"#{prefs.index(key) + 1} for {kind}")
        st = stats.get((kind, key)) or {}
        done, bad = st.get("done", 0), st.get("bad", 0)
        if done + bad < 3:
            score += 15
            why.append(f"no history for {kind}")
        else:
            score += 30 * (done + 1) / (done + bad + 2)
            why.append(f"done {done} / bad {bad} for {kind}")
        if used:
            score -= 0.3 * used
            why.append(f"5h at {used:g}%")
        running = load.get(key, 0)
        if running:
            score -= 10 * running
            why.append(f"{running} running")
        if entry is not None and running >= entry.get("max_concurrent", 10**6):
            score -= 100
            why.append("at max_concurrent")
        tier = TIER_COST.get((entry or {}).get("tier"), 0)
        if tier:
            score += tier
            why.append(f"tier {(entry or {}).get('tier')} {tier}")
        est = estimate(kind, key, history, prompt_bytes)
        out.append({"key": key, "agent": agent, "model": model, "score": round(score, 2), "eligible": not no,
                    "est_tokens": est["tokens"], "est_usd": est["usd"], "reasons": no + why})
    out.sort(key=lambda c: (not c["eligible"], -c["score"]))
    return out


# ---------- R2 estimate ----------

def _median(vals):
    vals = [v for v in vals if v is not None]
    return statistics.median(vals) if vals else None


def estimate(kind: str, key: str, history, prompt_bytes: int = 0) -> dict:
    same_key = [r for r in history if r.get("key") == key]
    rows = [r for r in same_key if r.get("kind") == kind]
    basis = "kind"
    if not rows:
        rows, basis = same_key, "model"
    extra = prompt_bytes // 4
    if not rows:
        return {"tokens": DEFAULT_ESTIMATE.get(kind, DEFAULT_TOKENS) + extra, "usd": None, "basis": "default",
                "samples": 0, "cached_tokens": 0}
    tokens = _median([(r.get("input") or 0) + (r.get("output") or 0) for r in rows])
    cached = _median([r.get("cached") or 0 for r in rows])
    usd = None if key == "codex" else _median([r.get("cost_usd") for r in rows])
    return {"tokens": int(tokens) + extra, "usd": usd, "basis": basis, "samples": len(rows),
            "cached_tokens": int(cached or 0)}


def history(conn, since: float) -> list[dict]:
    """Per-task usage rows; cost_usd is MAX per task because agent_usage.cost_usd is cumulative (E6)."""
    rows = conn.execute(
        "SELECT a.task_id, COALESCE(MAX(a.kind), MAX(t.kind)) AS kind, MAX(a.agent) AS agent, MAX(a.model) AS model, "
        "SUM(a.input) AS i, SUM(a.output) AS o, SUM(a.cached) AS c, MAX(a.cost_usd) AS usd FROM agent_usage a "
        "LEFT JOIN tasks t ON t.id=a.task_id WHERE a.ts>=? GROUP BY a.task_id", (since,)).fetchall()
    return [{"kind": r["kind"], "key": key_of(r["agent"], r["model"]), "input": r["i"] or 0, "output": r["o"] or 0,
             "cached": r["c"] or 0, "cost_usd": r["usd"]} for r in rows if r["kind"]]


# ---------- R3 forecast ----------

def forecast(accounts: dict, now: float) -> dict:
    out = {}
    for name, a in accounts.items():
        used = a.get("used_5h") or 0
        week = a.get("used_week") or 0
        resets = a.get("resets_5h")
        recent = sorted(s for s in a.get("samples") or [] if s[0] >= now - 3600)
        rate = None
        if len(recent) >= 2 and recent[-1][0] > recent[0][0]:
            rate = (recent[-1][1] - recent[0][1]) / ((recent[-1][0] - recent[0][0]) / 3600)
        hours = max(0.0, (resets - now) / 3600) if resets else 0.0
        projected = min(100, used + rate * hours) if rate is not None else used
        exhausts = None
        if rate and rate > 0 and used < 100:
            at = now + (100 - used) / rate * 3600
            exhausts = at if not resets or at < resets else None
        if used >= 100 or week >= 100:
            status = "limited"
        elif projected >= 85 or used >= 85:
            status = "at_risk"
        else:
            status = "ok"
        out[name] = {"status": status, "used_5h": used, "projected_5h": projected, "exhausts_at": exhausts,
                     "resets_5h": resets, "advice": ""}
    for name, f in out.items():
        until = time.strftime("%H:%M", time.localtime(f["resets_5h"])) if f["resets_5h"] else "its reset"
        others = [n for n, o in out.items() if n != name and o["status"] == "ok"]
        if f["status"] == "ok":
            f["advice"] = f"no action needed on {name}"
        else:
            f["advice"] = f"hold new paid work on {name} until {until}" + (f"; use {others[0]}" if others else "")
    return out


# ---------- R5 suggestions ----------

def _entry(c: dict, kind: str) -> dict:
    return {"agent": c["agent"], "model": c["model"], "kind": kind, "why": "; ".join(c["reasons"][:2])}


def suggest(kind: str, refused_key: str, rules: dict, headroom: dict, load: dict, security: bool = False,
            round_reviewer: str | None = None, allowed=None) -> list[dict]:
    """R5. A refused Codex re_review: the round reviewer's model if eligible, else the top re_review candidate;
    plus codex security_review only when security=True."""
    ranked = rank(kind, rules=rules, stats={}, headroom=headroom, load=load, exclude=(refused_key,), allowed=allowed)
    cands = [c for c in ranked if c["eligible"]]
    special = kind == "re_review" and split_key(refused_key)[0] == "codex"
    if special and round_reviewer:
        first = [c for c in cands if c["key"] == round_reviewer]
        if first:
            cands = [dict(first[0], reasons=["original round reviewer", *first[0]["reasons"]]),
                     *[c for c in cands if c["key"] != round_reviewer]]
    picks = [_entry(c, kind) for c in cands]
    picks = picks[:2] if special else picks[:3]
    if special and security:
        sec = [c for c in rank("security_review", rules=rules, stats={}, headroom=headroom, load=load, allowed=allowed)
               if c["key"] == "codex" and c["eligible"]]
        if sec:
            picks.append(_entry(sec[0], "security_review"))
    seen, out = set(), []
    for p in picks:
        k = (p["agent"], p["model"], p["kind"])
        if k not in seen:
            seen.add(k)
            out.append(p)
    return out


# ---------- R4 decisions ----------

def ensure_schema(conn) -> None:
    conn.execute(SCHEMA)


def record(conn, *, task_id, kind, action, chosen, candidates, reasons, est_tokens=None, est_usd=None) -> int:
    cur = conn.execute("INSERT INTO intel_decisions (ts, task_id, kind, action, chosen, candidates, reasons, est_tokens, "
                 "est_usd) VALUES (?,?,?,?,?,?,?,?,?)",
                 (time.time(), task_id, kind, action, chosen, json.dumps(candidates), json.dumps(reasons),
                  est_tokens, est_usd))
    return cur.lastrowid


def decisions(conn, task_id: str | None = None, limit: int = 50) -> list[dict]:
    q, args = "SELECT * FROM intel_decisions", ()
    if task_id:
        q, args = q + " WHERE task_id=?", (task_id,)
    rows = conn.execute(q + " ORDER BY id DESC LIMIT ?", (*args, max(1, min(limit, 500)))).fetchall()
    return [dict(r) | {"candidates": json.loads(r["candidates"] or "[]"), "reasons": json.loads(r["reasons"] or "[]")}
            for r in rows]


# ---------- R8 cleanup ----------

def cleanup_candidates(tasks, is_merged) -> list[dict]:
    return [t for t in tasks if t.get("state") in TERMINAL and t.get("node_id") == "master" and t.get("worktree")
            and t.get("branch") and is_merged(t.get("repo"), t["branch"])]


# ---------- R9 savings ----------

def _window(conn, lo: float, hi: float) -> dict:
    per = conn.execute(
        "SELECT a.task_id, MAX(a.agent) AS agent, MAX(t.state) AS state, "
        "COALESCE(SUM(a.input),0)+COALESCE(SUM(a.output),0) AS tok, "
        "COALESCE(MAX(a.cost_usd),0) AS usd FROM agent_usage a LEFT JOIN tasks t ON t.id=a.task_id "
        "WHERE a.ts>=? AND a.ts<? AND a.agent IN ('claude','codex') GROUP BY a.task_id", (lo, hi)).fetchall()
    tokens = sum(r["tok"] for r in per)
    wasted = sum(r["tok"] for r in per if r["state"] in WASTED_STATES)

    def share(kind, where):
        n = conn.execute("SELECT COUNT(*) FROM tasks WHERE kind=? AND created_at>=? AND created_at<?",
                         (kind, lo, hi)).fetchone()[0]
        k = conn.execute("SELECT COUNT(*) FROM tasks WHERE kind=? AND created_at>=? AND created_at<? AND " + where,
                         (kind, lo, hi)).fetchone()[0]
        return k / n if n else None

    def count(action):
        return conn.execute("SELECT COUNT(*) FROM intel_decisions WHERE action=? AND ts>=? AND ts<?",
                            (action, lo, hi)).fetchone()[0]

    local = conn.execute("SELECT COALESCE(SUM(COALESCE(tokens_in,0)+COALESCE(tokens_out,0)),0) FROM call_usage "
                         "WHERE ts>=? AND ts<?", (lo, hi)).fetchone()[0]
    return {"paid_tasks": len(per), "paid_tokens": tokens, "tokens_per_task": tokens / len(per) if per else None,
            "wasted_tokens": wasted, "wasted_share": wasted / tokens if tokens else None,
            "final_reviews_on_codex": share("final_review", "agent='codex'"),
            "drafts_off_sonnet": share("draft", "NOT (agent='claude' AND (model LIKE '%sonnet%' OR model LIKE "
                                                "'%opus%'))"),
            "usd": sum(r["usd"] for r in per), "codex_usd": "not reported", "local_tokens": local,
            "auto_picks": count("auto_pick"), "suggestions": count("suggest"), "cleanups": count("cleanup")}


def savings(conn, since: float, baseline_since: float, baseline_until: float) -> dict:
    ensure_schema(conn)
    cur, base = _window(conn, since, 1e18), _window(conn, baseline_since, baseline_until)
    num = lambda v: isinstance(v, (int, float)) and not isinstance(v, bool)  # noqa: E731
    delta = {k: cur[k] - base[k] for k in cur if num(cur[k]) and num(base[k])}
    return {"current": cur, "baseline": base, "delta": delta}
