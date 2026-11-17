#!/usr/bin/env python3
"""Run fixed CLAUDE.md/skill/brief harness cases through the fleet task API.

The controller remains the source of truth: each case is a normal task with
kind=benchmark, its gate is read from the task record, and token counts come
from the task usage record (with /api/tokens as a richer fallback).
"""
import argparse
import datetime as dt
import json
import os
import re
import shlex
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_CASES = HERE / "cases" / "harness.json"
DEFAULT_OUT = HERE / "results"


def request(base: str, method: str, path: str, token: str, body=None, timeout=60):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(base.rstrip("/") + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise RuntimeError(f"{method} {path}: HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"{method} {path}: {exc.reason}") from exc


def load_cases(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list) or not 5 <= len(data) <= 10:
        raise ValueError("harness cases must be a list of 5-10 cases")
    ids = set()
    for case in data:
        if not isinstance(case, dict) or not all(isinstance(case.get(k), str) and case[k].strip()
                                                  for k in ("id", "title", "prompt", "gate")):
            raise ValueError("each harness case needs non-empty id, title, prompt, and gate")
        if case["id"] in ids:
            raise ValueError(f"duplicate harness case id: {case['id']}")
        ids.add(case["id"])
    return data


def token_counts(task: dict, telemetry: dict | None = None) -> dict:
    """Return task-reported counts, preferring event telemetry when it is present."""
    usage = task.get("usage") if isinstance(task.get("usage"), dict) else {}
    telemetry = telemetry if isinstance(telemetry, dict) else {}
    def number(value):
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0
    cache = number(telemetry.get("cache_read")) + number(telemetry.get("cache_write"))
    return {"input": number(telemetry.get("input")) or number(usage.get("input")),
            "output": number(telemetry.get("output")) or number(usage.get("output")),
            "cache": cache or number(usage.get("cached"))}


def add_tokens(rounds: list[dict]) -> dict:
    """Sum individual runner-attempt usage records without relying on session-total semantics."""
    return {name: sum(tokens.get(name, 0) for tokens in rounds) for name in ("input", "output", "cache")}


def record_case(case: dict, task: dict, telemetry: dict | None = None,
                round_tokens: list[dict] | None = None) -> dict:
    gate = task.get("gate_result") if isinstance(task.get("gate_result"), dict) else {}
    gate_passed = gate.get("state") == "passed"
    rounds = task.get("runs") if isinstance(task.get("runs"), int) and task["runs"] >= 0 else 0
    event_tokens = token_counts({}, telemetry)
    tokens = event_tokens if any(event_tokens.values()) else add_tokens(round_tokens or [])
    if not any(tokens.values()):
        tokens = token_counts(task)
    return {"case_id": case["id"], "title": case["title"], "task_id": task.get("id"),
            "state": task.get("state"), "gate_passed": gate_passed, "rounds": rounds,
            "fix_rounds": max(0, rounds - 1), "tokens": tokens}


def summary(cases: list[dict]) -> dict:
    n = len(cases)
    total = lambda name: sum(c["tokens"][name] for c in cases)
    return {"cases": n, "gate_passed": sum(c["gate_passed"] for c in cases),
            "gate_pass_rate": (sum(c["gate_passed"] for c in cases) / n) if n else 0,
            "rounds": sum(c["rounds"] for c in cases), "fix_rounds": sum(c["fix_rounds"] for c in cases),
            "tokens": {name: total(name) for name in ("input", "output", "cache")}}


def compare(old: dict, new: dict) -> str:
    a, b = old["summary"], new["summary"]
    lines = [f"old: {old.get('variant', 'unknown')}  new: {new.get('variant', 'unknown')}",
             "metric                 old        new      delta",
             f"gate pass rate       {a['gate_pass_rate']:.1%}      {b['gate_pass_rate']:.1%}    {b['gate_pass_rate'] - a['gate_pass_rate']:+.1%}",
             f"fix rounds            {a['fix_rounds']:>4}       {b['fix_rounds']:>4}     {b['fix_rounds'] - a['fix_rounds']:+d}"]
    for name in ("input", "output", "cache"):
        lines.append(f"{name + ' tokens':<20} {a['tokens'][name]:>7}    {b['tokens'][name]:>7}  {b['tokens'][name] - a['tokens'][name]:+7}")
    return "\n".join(lines)


def wait_for_task(base: str, token: str, task_id: str, timeout: int) -> dict:
    query = urllib.parse.urlencode({"ids": task_id, "timeout": timeout, "mode": "all"})
    wait = request(base, "GET", f"/api/tasks/wait?{query}", token, timeout=timeout + 30)
    if wait.get("timed_out"):
        raise RuntimeError(f"task {task_id} did not settle within {timeout}s")
    return request(base, "GET", f"/api/tasks/{urllib.parse.quote(task_id, safe='')}", token)


def variant_prompt(ref: str, prompt: str) -> str:
    """Pin work to the variant after the runner creates its isolated task worktree."""
    return (f"This is harness variant ref {ref!r}. Before inspecting or editing files, run "
            f"git checkout --detach {shlex.quote(ref)} in the current worktree. Do not switch to another ref.\n\n{prompt}")


def run(args) -> dict:
    cases = load_cases(Path(args.cases))
    records = []
    for case in cases:
        body = {"agent": args.agent, "title": f"Harness {case['id']}",
                "prompt": variant_prompt(args.variant_ref, case["prompt"]),
                "repo": args.repo, "gate": args.gate or case["gate"], "mode": "auto-edit",
                "worktree": True, "kind": "benchmark"}
        if args.model:
            body["model"] = args.model
        if args.node:
            body["node"] = args.node
        if args.effort:
            body["effort"] = args.effort
        created = request(args.controller, "POST", "/api/tasks", args.token, body)
        task = wait_for_task(args.controller, args.token, created["id"], args.timeout)
        round_tokens = [token_counts(task)]
        # A failed gate leaves an auto-edit task resumable in its existing worktree. Reusing that controller path
        # is what makes "fix rounds" comparable across instruction variants instead of merely counting first tries.
        while (task.get("gate_result") or {}).get("state") == "failed" and (task.get("runs") or 0) < args.max_rounds:
            request(args.controller, "POST", f"/api/tasks/{urllib.parse.quote(created['id'], safe='')}/message",
                    args.token, {"text": "The configured gate failed. Inspect its output and fix the failures. The runner reruns "
                                       "the gate after you finish; do not run it yourself."})
            task = wait_for_task(args.controller, args.token, created["id"], args.timeout)
            round_tokens.append(token_counts(task))
        try:
            telemetry = request(args.controller, "GET", f"/api/tokens/{created['id']}", args.token)
        except RuntimeError:
            telemetry = None  # Not every agent emits an event log; task usage is still authoritative.
        record = record_case(case, task, telemetry, round_tokens)
        records.append(record)
        print(f"{record['case_id']}: gate={'pass' if record['gate_passed'] else 'fail'} rounds={record['rounds']}")
    return {"schema": 1, "variant": args.variant, "variant_ref": args.variant_ref, "agent": args.agent,
            "effort": args.effort, "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "cases": records, "summary": summary(records)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("variant", nargs="?", help="label for the CLAUDE.md, skill, or brief variant")
    parser.add_argument("--cases", default=str(DEFAULT_CASES))
    parser.add_argument("--repo", help="absolute repository path on the selected runner")
    parser.add_argument("--controller", default=os.environ.get("FLEET_CONTROLLER", "http://127.0.0.1:8701"))
    parser.add_argument("--token", default=os.environ.get("FLEET_TOKEN", ""))
    parser.add_argument("--agent", default="claude", help="claude or ollama (codex and auto are refused)")
    parser.add_argument("--model")
    parser.add_argument("--node")
    parser.add_argument("--variant-ref", help="Git ref checked out in every benchmark worktree")
    parser.add_argument("--gate", help="override every case's gate profile")
    parser.add_argument("--effort", choices=("low", "medium", "high"), help="effort sent to each benchmark task")
    parser.add_argument("--timeout", type=int, default=1800)
    parser.add_argument("--max-rounds", type=int, default=3, help="initial attempt plus gate-fix resumes per case")
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--compare", nargs=2, metavar=("OLD", "NEW"), help="print two run files side by side")
    args = parser.parse_args(argv)
    if args.compare:
        old, new = (json.loads(Path(path).read_text(encoding="utf-8")) for path in args.compare)
        print(compare(old, new))
        return 0
    if not args.variant or not args.variant_ref or not args.repo:
        parser.error("variant, --variant-ref, and --repo are required unless --compare is used")
    if args.max_rounds < 1:
        parser.error("--max-rounds must be at least 1")
    if args.agent not in ("claude", "ollama"):
        parser.error("--agent must be claude or ollama; auto and codex are refused for benchmarks")
    if not re.fullmatch(r"[A-Za-z0-9._/][A-Za-z0-9._/-]{0,127}", args.variant_ref):
        parser.error("--variant-ref must be a plain git ref (letters, digits, . _ / -; not starting with -)")
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", args.variant):
        parser.error("variant must be 1-64 chars of letters, digits, dot, dash or underscore")
    if not args.token:
        parser.error("--token or FLEET_TOKEN is required")
    result = run(args)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = out / f"harness-{args.variant}-{stamp}.json"
    path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
