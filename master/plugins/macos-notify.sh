#!/bin/sh
# Reads one event as JSON on stdin and shows a macOS notification. Silently does nothing elsewhere.
command -v osascript >/dev/null 2>&1 || exit 0
python3 -c '
import json, sys, subprocess
e = json.load(sys.stdin)
d = e.get("data") or {}
state = e["type"].split(".", 1)[1].replace("_", " ")
title = (d.get("title") or e.get("task_id") or "task")[:80]
msg = f"{title}: {state}"
esc = lambda s: s.replace("\\", "\\\\").replace("\"", "\\\"")
subprocess.run(["osascript", "-e", f"display notification \"{esc(msg)}\" with title \"AI Fleet\""])
'
