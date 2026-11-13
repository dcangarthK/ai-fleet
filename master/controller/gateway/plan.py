"""Namespaced WAN/VLAN policy plan and dry-run diff. Pure functions; never touches a router."""
from __future__ import annotations

from dataclasses import dataclass, field

from .adapter import NAMESPACE, Capabilities, Rule


@dataclass
class Policy:
    worker_vlan: str = "workers"
    wan: str = "wan"
    # Measured egress allowlist (spec §9: do not enable until measured). Entries: (dst, proto, port).
    wan_allow: list = field(default_factory=list)
    deny_lateral: bool = True
    block_wan_default: bool = False   # stays False until the owner has measured the allowlist


def build_plan(policy: Policy, caps: Capabilities) -> tuple[list[Rule], list[str]]:
    """Return (desired managed rules, skipped reasons). Unsupported features are skipped, never fatal."""
    rules: list[Rule] = []
    skipped: list[str] = []
    if not caps.vlan:
        skipped.append("vlan unsupported: lateral deny skipped")
    elif policy.deny_lateral:
        rules.append(Rule(f"{NAMESPACE}deny-lateral", "deny", policy.worker_vlan, policy.worker_vlan))
    if not caps.wan_egress_rules:
        skipped.append("wan egress rules unsupported: egress policy skipped")
    else:
        for i, (dst, proto, port) in enumerate(policy.wan_allow):
            rules.append(Rule(f"{NAMESPACE}wan-allow-{i}", "allow", policy.worker_vlan, dst, proto, port))
        if policy.block_wan_default:
            if not policy.wan_allow:
                skipped.append("wan default deny refused: empty allowlist")
            else:
                rules.append(Rule(f"{NAMESPACE}wan-deny-default", "deny", policy.worker_vlan, policy.wan))
    return rules, skipped


def merge(current: list[Rule], desired: list[Rule]) -> list[Rule]:
    """Full list to install: every unmanaged rule untouched and in order, managed rules replaced by desired."""
    return desired + [r for r in current if not r.managed]


def diff_plan(current: list[Rule], desired: list[Rule]) -> dict:
    cur = {r.id: r for r in current if r.managed}
    des = {r.id: r for r in desired}
    bad = [r.id for r in desired if not r.managed]
    if bad:
        raise ValueError(f"rules outside namespace {NAMESPACE!r}: {bad}")
    return {"add": [des[i] for i in des if i not in cur],
            "remove": [cur[i] for i in cur if i not in des],
            "change": [des[i] for i in des if i in cur and cur[i] != des[i]]}


def render_diff(d: dict) -> str:
    lines = []
    for key, sign in (("add", "+"), ("remove", "-"), ("change", "~")):
        for r in d[key]:
            lines.append(f"{sign} {r.id}: {r.action} {r.src} -> {r.dst} {r.proto}/{r.port}")
    return "\n".join(lines) or "no changes"
