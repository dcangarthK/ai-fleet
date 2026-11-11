"""Shows one fleet event as a macOS notification. Reads the event JSON on stdin.

The controller attaches "notification": {title, subtitle, message}; events without one aren't shown.
Text goes to osascript as arguments, never pasted into the script, so nothing needs escaping.
"""
import json
import shutil
import subprocess
import sys

if not shutil.which("osascript"):
    sys.exit(0)
note = (json.load(sys.stdin) or {}).get("notification")
if not note:
    sys.exit(0)
subprocess.run([
    "osascript",
    "-e", "on run argv",
    "-e", "display notification (item 3 of argv) with title (item 1 of argv) subtitle (item 2 of argv)",
    "-e", "end run",
    str(note.get("title") or "AI Fleet")[:120], str(note.get("subtitle") or "")[:120], str(note.get("message") or "")[:240],
], check=False, timeout=10)
