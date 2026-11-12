#!/usr/bin/env python3
"""PreToolUse hook for Bash (Part H req 5). Standard library only.

Hook mode (no args): reads the hook JSON on stdin. For test commands it answers with
hookSpecificOutput.updatedInput.command rewritten to call this script in wrap mode; any other command
produces no output (exit 0, unchanged).

Wrap mode: quiet_bash.py --wrap LOGFILE -- COMMAND. Runs COMMAND with bash, saves the full output to
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
TEST_RE = re.compile(r"(python3?\s+-m\s+unittest|scripts/gate\.sh|\bvitest\b)")


def is_test_command(cmd):
    return bool(TEST_RE.search(cmd or ""))


def job_dir():
    d = os.environ.get("FLEET_JOB_DIR") or os.path.join(tempfile.gettempdir(), "fleet-quiet-bash")
    os.makedirs(d, exist_ok=True)
    return d


def new_log_path():
    return os.path.join(job_dir(), "test-%d-%d.log" % (int(time.time() * 1000), os.getpid()))


def rewrite_command(cmd, log):
    return "python3 %s --wrap %s -- %s" % (
        shlex.quote(os.path.abspath(__file__)), shlex.quote(log), shlex.quote(cmd))


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
    out = out[:MAX_LINES - 1]
    out.append("full output: %s" % log)
    return "\n".join(out)


def run_wrapped(log, cmd):
    p = subprocess.run(["bash", "-c", cmd], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    text = p.stdout.decode("utf-8", "replace")
    with open(log, "w", encoding="utf-8") as f:
        f.write(text)
    print(summarize(text, p.returncode, log))
    return p.returncode


def hook_output(payload):
    """The hook's JSON reply, or None to leave the command unchanged."""
    if payload.get("tool_name") != "Bash":
        return None
    ti = payload.get("tool_input") or {}
    cmd = ti.get("command") or ""
    if not is_test_command(cmd) or ("quiet_bash.py" in cmd and "--wrap" in cmd):
        return None
    new = dict(ti)
    new["command"] = rewrite_command(cmd, new_log_path())
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow",
                                   "updatedInput": new}}


def main(argv):
    if len(argv) >= 4 and argv[1] == "--wrap" and argv[3] == "--":
        return run_wrapped(argv[2], " ".join(argv[4:]))
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
