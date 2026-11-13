#!/usr/bin/env python3
"""Read-only audit of TCP 11435 (legacy node inventory server), SPEC-networking.md phase N0.

Lists listeners only (one fixed `lsof` call, or a saved lsof listing via --file / stdin). Changes nothing.
Exposure: loopback, tailnet (100.64.0.0/10 or fd7a:115c:a1e0::/48), wildcard, or other (LAN etc).
Exit 1 if a wildcard/other listener is seen (even when not root).
Otherwise exit 0 only for a root live run (or a saved listing via --file/stdin).
Exit 2 = UNVERIFIED: lsof missing, failed or wrote to stderr, or a live run as non-root.
Non-root lsof sees only your own user's sockets, so its results are partial: use sudo.
Windows has no lsof, so it always exits 2 there.
"""
import ipaddress
import os
import subprocess
import sys

PORT = 11435
LSOF_CMD = ["lsof", "-nP", f"-iTCP:{PORT}", "-sTCP:LISTEN"]
_TS4 = ipaddress.ip_network("100.64.0.0/10")
_TS6 = ipaddress.ip_network("fd7a:115c:a1e0::/48")


def classify(host):
    if host in ("*", "0.0.0.0", "::", "[::]"):
        return "wildcard"
    try:
        ip = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return "other"
    if ip.is_loopback:
        return "loopback"
    if ip in _TS4 or ip in _TS6:
        return "tailnet"
    if ip.is_unspecified:
        return "wildcard"
    return "other"


def parse_lsof(text):
    """Parse `lsof -nP` output into listener dicts for PORT. Malformed lines are skipped."""
    found = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 10 or parts[0] == "COMMAND" or parts[-1] != "(LISTEN)":
            continue
        host, sep, port = parts[-2].rpartition(":")
        if not sep or port != str(PORT):
            continue
        found.append({"command": parts[0], "pid": parts[1], "user": parts[2], "type": parts[4],
                      "host": host, "port": PORT, "exposure": classify(host)})
    return found


def report(listeners):
    if not listeners:
        return f"No listener on TCP {PORT}."
    lines = [f"{len(listeners)} listener(s) on TCP {PORT}:"]
    for x in listeners:
        lines.append(f"  {x['command']} pid={x['pid']} user={x['user']} {x['type']} "
                     f"{x['host']}:{x['port']} -> {x['exposure']}")
    return "\n".join(lines)


def _run_lsof(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    return r.returncode, r.stdout, r.stderr


def main(argv, run=_run_lsof, euid=None):
    """`run` and `euid` are injectable so tests use fakes."""
    live = False
    if len(argv) > 2 and argv[1] == "--file":
        with open(argv[2]) as f:
            text = f.read()
    elif len(argv) > 1 and argv[1] == "-":
        text = sys.stdin.read()
    else:
        live = True
        try:
            rc, text, err = run(LSOF_CMD)
        except (OSError, subprocess.TimeoutExpired) as e:
            print(f"UNVERIFIED: lsof failed: {e}", file=sys.stderr)
            return 2
        # lsof exits 1 when nothing matches; any other code or any stderr output
        # means we cannot trust the read
        if rc > 1 or err.strip():
            print(f"UNVERIFIED: lsof rc={rc} stderr={err.strip()[:200]!r}", file=sys.stderr)
            return 2
    listeners = parse_lsof(text)
    print(report(listeners))
    if any(x["exposure"] in ("wildcard", "other") for x in listeners):
        return 1
    if live and (os.geteuid() if euid is None else euid) != 0:
        # a wildcard/other listener already returned 1 above; anything else is a partial view
        print("UNVERIFIED: not root, so lsof only sees your own user's sockets. Re-run with sudo.")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
