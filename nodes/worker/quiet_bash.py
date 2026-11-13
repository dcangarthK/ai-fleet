#!/usr/bin/env python3
"""PreToolUse hook for Bash (Part H req 5). Standard library only.

Hook mode (no args): reads the hook JSON on stdin. For test commands it answers with
hookSpecificOutput.updatedInput.command rewritten to call this script in wrap mode; any other command
produces no output (exit 0, unchanged).

Also: bare `git log` gets `-n 30`; `git diff` with flags only is run in head mode (first 300 lines, then a
"truncated, full: LOGFILE" line).

Wrap mode: quiet_bash.py --wrap LOGFILE -- COMMAND (or --head LOGFILE -- COMMAND for head mode). Runs COMMAND with bash, saves the full output to
LOGFILE, prints a short summary and exits with the command's exit code (a failure is never hidden).
"""
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time

MAX_LINES = 100
MAX_LINE_CHARS = 500
HEAD_LINES = 300
MAX_TEST_RUNS = 2  # the third run of the same test command is refused
# A test runner at the start of a segment (optionally after VAR=x assignments).
TEST_RE = re.compile(r"^\s*(?:\w+=\S*\s+)*(python3?\s+-m\s+unittest\b|(?:\./)?scripts/gate\.sh\b|(?:npx\s+)?vitest\b)")
_private_dir = None


def split_segments(cmd):
    """Split on && || ; | outside quotes."""
    segs, cur, quote, i = [], [], None, 0
    while i < len(cmd):
        c = cmd[i]
        if quote:
            cur.append(c)
            if c == "\\" and quote == '"' and i + 1 < len(cmd):
                i += 1
                cur.append(cmd[i])
            elif c == quote:
                quote = None
        elif c in "'\"":
            quote = c
            cur.append(c)
        elif c == "\\" and i + 1 < len(cmd):
            cur.append(c)
            i += 1
            cur.append(cmd[i])
        elif c in ";|&":
            if c != "&" or (i + 1 < len(cmd) and cmd[i + 1] == "&"):
                segs.append("".join(cur))
                cur = []
                if i + 1 < len(cmd) and cmd[i + 1] == c:
                    i += 1
            else:
                cur.append(c)
        else:
            cur.append(c)
        i += 1
    segs.append("".join(cur))
    return segs


def is_test_command(cmd):
    return any(TEST_RE.match(seg) for seg in split_segments(cmd or ""))


def git_kind(cmd):
    """'log' for bare `git log`, 'diff' for `git diff` with flags only, else None (whole command only)."""
    try:
        toks = shlex.split(cmd or "")
    except ValueError:
        return None
    if toks == ["git", "log"]:
        return "log"
    if len(toks) >= 2 and toks[:2] == ["git", "diff"] and all(t.startswith("-") for t in toks[2:]) \
            and not any(c in (cmd or "") for c in ";|&<>$`("):
        return "diff"
    return None


def job_dir():
    global _private_dir
    d = os.environ.get("FLEET_JOB_DIR")
    if d:
        os.makedirs(d, exist_ok=True)
        return d
    if _private_dir is None:
        _private_dir = tempfile.mkdtemp(prefix="fleet-quiet-bash-")
    return _private_dir


def repeat_count(cmd):
    """How many times this exact test command has been asked for in this job, this one included (Part H 3.6).
    Counted in test-runs.json in the job dir; whitespace differences don't make a new command. A counter that
    can't be read or written counts as a first run: it must never block work by mistake."""
    key = " ".join((cmd or "").split())
    path = os.path.join(job_dir(), "test-runs.json")
    try:
        with open(path, encoding="utf-8") as f:
            counts = json.load(f)
        if not isinstance(counts, dict):
            counts = {}
    except (OSError, ValueError):
        counts = {}
    counts[key] = counts.get(key, 0) + 1
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(counts, f)
    except OSError:
        return 1
    return counts[key]


def new_log_path():
    return os.path.join(job_dir(), "test-%d-%d.log" % (int(time.time() * 1000), os.getpid()))


def rewrite_command(cmd, log, mode="--wrap"):
    return "python3 %s %s %s -- %s" % (
        shlex.quote(os.path.abspath(__file__)), mode, shlex.quote(log), shlex.quote(cmd))


def summarize(text, rc, log):
    """Short view of a test run's output. The exit code is always on the first line."""
    lines = text.splitlines()
    out = []
    ran = next((l.strip() for l in lines if re.match(r"Ran \d+ tests? in ", l)), "")
    verdict = next((l.strip() for l in reversed(lines) if re.match(r"(OK|FAILED)\b", l)), "")
    # unittest failure blocks: "FAIL: name" / "ERROR: name"; first error = last line of the block
    blocks, cur = [], None
    for l in lines:
        m = re.match(r"(FAIL|ERROR): (.+)", l)
        if m:
            cur = [m.group(1) + ": " + m.group(2), []]
            blocks.append(cur)
        elif re.match(r"(={20,}|Ran \d+ tests? in )", l):
            cur = None
        elif cur is not None and l.strip() and not l.startswith("-" * 20):
            cur[1].append(l.strip())
    if rc == 0:
        counts = ran or verdict or "no test summary parsed"
        return "exit=0 ok: %s; full output: %s" % (counts, log)
    head = "exit=%d FAILED" % rc
    if ran or verdict:
        head += ": %s %s" % (ran, verdict)
    out.append(head.strip())
    if blocks:
        for name, body in blocks:
            out.append(name)
            if body:
                out.append("  " + body[-1])
    else:
        # vitest, gate.sh or unparsed: lines that look like failures, else the tail
        pat = re.compile(r"(FAIL|Error|error|failed|Tests\s+\d)")
        hits = [l for l in lines if pat.search(l)]
        out.extend(hits if hits else lines[-30:])
    out = [l if len(l) <= MAX_LINE_CHARS else l[:MAX_LINE_CHARS] + "...[cut]" for l in out[:MAX_LINES - 1]]
    out.append("full output: %s" % log)
    return "\n".join(out)


def run_wrapped(log, cmd):
    p = subprocess.run(["bash", "-c", cmd], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    text = p.stdout.decode("utf-8", "replace")
    try:
        with open(log, "w", encoding="utf-8") as f:
            f.write(text)
    except OSError:
        sys.stdout.write(text)  # cannot keep a log: show everything rather than hide it
        return p.returncode
    print(summarize(text, p.returncode, log))
    return p.returncode


def run_head(log, cmd):
    p = subprocess.run(["bash", "-c", cmd], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    text = p.stdout.decode("utf-8", "replace")
    lines = text.splitlines()
    if len(lines) <= HEAD_LINES:
        sys.stdout.write(text)
        return p.returncode
    try:
        with open(log, "w", encoding="utf-8") as f:
            f.write(text)
    except OSError:
        sys.stdout.write(text)
        return p.returncode
    print("\n".join(lines[:HEAD_LINES]))
    print("truncated, full: %s" % log)
    return p.returncode


def hook_output(payload):
    """The hook's JSON reply, or None to leave the command unchanged."""
    if payload.get("tool_name") != "Bash":
        return None
    ti = payload.get("tool_input") or {}
    cmd = ti.get("command") or ""
    if "quiet_bash.py" in cmd and ("--wrap" in cmd or "--head" in cmd):
        return None
    new = dict(ti)
    gk = git_kind(cmd)
    if gk == "log":
        new["command"] = "git log -n 30"
    elif gk == "diff":
        new["command"] = rewrite_command(cmd, new_log_path(), "--head")
    elif is_test_command(cmd):
        if repeat_count(cmd) > MAX_TEST_RUNS:
            return {"hookSpecificOutput": {
                "hookEventName": "PreToolUse", "permissionDecision": "deny",
                "permissionDecisionReason": "the gate will run this (this exact test command already ran %d times; "
                                            "the runner runs the gate once after you finish)" % MAX_TEST_RUNS}}
        new["command"] = rewrite_command(cmd, new_log_path())
    else:
        return None
    # updatedInput only: no permissionDecision, so normal permission rules still apply
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "updatedInput": new}}


def main(argv):
    if len(argv) >= 4 and argv[1] == "--wrap" and argv[3] == "--":
        return run_wrapped(argv[2], " ".join(argv[4:]))
    if len(argv) >= 4 and argv[1] == "--head" and argv[3] == "--":
        return run_head(argv[2], " ".join(argv[4:]))
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0  # never block the tool on a bad hook input
    out = hook_output(payload)
    if out:
        print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
