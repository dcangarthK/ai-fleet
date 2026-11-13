#!/usr/bin/env python3
"""Read-only audit of TCP 11435 (legacy node inventory server), SPEC-networking.md phase N0.

Lists listeners only (one fixed `lsof` call, or a saved lsof listing via --file / stdin). Changes nothing.
Exposure: loopback, tailnet (100.64.0.0/10 or fd7a:115c:a1e0::/48), wildcard, or other (LAN etc).
Exit 0 on a clean read; 1 if a wildcard/other listener exists; 2 if lsof cannot run.
"""
import ipaddress
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


def main(argv):
    if len(argv) > 2 and argv[1] == "--file":
        with open(argv[2]) as f:
            text = f.read()
    elif len(argv) > 1 and argv[1] == "-":
        text = sys.stdin.read()
    else:
        try:
            r = subprocess.run(LSOF_CMD, capture_output=True, text=True, timeout=15)
        except (OSError, subprocess.TimeoutExpired) as e:
            print(f"lsof failed: {e}", file=sys.stderr)
            return 2
        text = r.stdout  # lsof exits 1 when nothing matches; not an error
    listeners = parse_lsof(text)
    print(report(listeners))
    return 1 if any(x["exposure"] in ("wildcard", "other") for x in listeners) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
