"""Pure helpers for the Windows networking owner scripts.

These functions deliberately do not invoke PowerShell, Docker, or the network.  The
scripts which collect real machine state pass their observations in here so the
validation and presentation logic remains testable with fixtures.
"""

from __future__ import annotations

import ipaddress
import json
from collections.abc import Iterable, Mapping
from typing import Any


RUNNER_PORT = 11436
MODEL_PORTS = frozenset({11434, 12434})
TAILNET_V4 = ipaddress.ip_network("100.64.0.0/10")


def master_tailnet_addresses(values: Iterable[str]) -> list[str]:
    """Return unique, concrete Tailnet IPv4 addresses or raise ValueError.

    A CIDR, wildcard, LAN address, or IPv6 value cannot become a Defender
    RemoteAddress.  This prevents accidentally turning a narrow rule into a
    whole-tailnet rule.
    """
    addresses: list[str] = []
    for raw in values:
        try:
            address = ipaddress.ip_address(str(raw).strip())
        except ValueError as exc:
            raise ValueError(f"invalid master tailnet address: {raw!r}") from exc
        if address.version != 4 or address not in TAILNET_V4:
            raise ValueError(f"not a Tailnet IPv4 address: {raw!r}")
        text = str(address)
        if text not in addresses:
            addresses.append(text)
    if not addresses:
        raise ValueError("at least one master tailnet address is required")
    return addresses


def defender_rule(values: Iterable[str]) -> dict[str, Any]:
    """Build the only inbound rule this phase is permitted to propose."""
    return {
        "display_name": "AI Fleet Runner 11436",
        "direction": "Inbound",
        "action": "Allow",
        "protocol": "TCP",
        "local_ports": [RUNNER_PORT],
        "remote_addresses": master_tailnet_addresses(values),
    }


def powershell_rule_summary(rule: Mapping[str, Any]) -> str:
    """Render factual proposed-rule text without rendering an executable command."""
    return (
        f"{rule['display_name']}: {rule['direction']} {rule['action']} "
        f"{rule['protocol']} local port {','.join(str(port) for port in rule['local_ports'])} "
        f"from {','.join(rule['remote_addresses'])}"
    )


def parse_docker_model_ls(output: str | None) -> list[dict[str, str | None]]:
    """Parse Docker's fixture-compatible table or JSON-lines output conservatively."""
    if not output:
        return []
    models: list[dict[str, str | None]] = []
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        reference = digest = None
        if line.startswith("{"):
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            reference = record.get("Model") or record.get("MODEL") or record.get("Name")
            digest = record.get("ID") or record.get("Digest")
        elif not line.upper().startswith("MODEL"):
            parts = line.split()
            if parts:
                reference = parts[0]
                digest = parts[1] if len(parts) > 1 else None
        if isinstance(reference, str) and reference:
            models.append({"engine": "dmr", "reference": reference, "digest": digest if isinstance(digest, str) else None})
    return models


def runner_availability(task: Mapping[str, Any] | None, service: Mapping[str, Any] | None) -> dict[str, str]:
    """Classify fake/read-only task and service observations for the Live surface."""
    service = service or {}
    if str(service.get("Status", "")).lower() == "running" and str(service.get("StartType", "")).lower() in {"auto", "automatic"}:
        return {"availability": "available-before-owner-logon", "source": "windows-service", "detail": "automatic Windows service is running"}
    task = task or {}
    state = str(task.get("State", "Unknown"))
    triggers = " ".join(str(item) for item in task.get("Triggers", []))
    restart = bool(task.get("RestartOnFailure", False))
    if "Logon" in triggers:
        detail = "per-user scheduled task; available after owner logon"
        if state.lower() == "running":
            detail += "; currently running"
        return {"availability": "available-after-owner-logon", "source": "scheduled-task", "detail": detail}
    if "Boot" in triggers and restart:
        return {"availability": "available-before-owner-logon", "source": "scheduled-task", "detail": "boot-triggered scheduled task with restart enabled"}
    return {"availability": "unknown", "source": "read-only-check", "detail": "no proven boot service or restartable boot task"}
