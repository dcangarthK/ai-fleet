"""Apply with canary and rollback. Dry run by default; applying needs the explicit owner_approved flag."""
from __future__ import annotations

from dataclasses import dataclass, field

from .adapter import Capabilities, GatewayAdapter
from .plan import Policy, build_plan, diff_plan, merge, render_diff


@dataclass
class ApplyResult:
    status: str                      # dry_run | applied | rolled_back | unsupported | unreachable | refused | noop
    diff: str = ""
    skipped: list = field(default_factory=list)
    detail: str = ""


def apply_plan(adapter: GatewayAdapter, policy: Policy, canaries: list, *, owner_approved: bool = False,
               dry_run: bool = True, confirm_window_s: float = 60.0) -> ApplyResult:
    """canaries: [(src, dst, port, expect_reachable)]. Any mismatch after staging rolls everything back."""
    try:
        caps: Capabilities = adapter.discover()
        if not caps.reachable:
            return ApplyResult("unreachable", detail="router not reachable; nothing changed")
        desired, skipped = build_plan(policy, caps)
        if not caps.can_enforce:
            return ApplyResult("unsupported", skipped=skipped + ["router cannot enforce staged WAN policy"])
        current = adapter.read_rules()
        text = render_diff(diff_plan(current, desired))
        if dry_run:
            return ApplyResult("dry_run", text, skipped)
        if not owner_approved:
            return ApplyResult("refused", text, skipped, "owner approval flag not set")
        if text == "no changes":
            return ApplyResult("noop", text, skipped)
        adapter.stage(merge(current, desired), confirm_window_s)
        for src, dst, port, expect in canaries:
            if adapter.probe(src, dst, port) != expect:
                adapter.rollback()
                return ApplyResult("rolled_back", text, skipped, f"canary {src}->{dst}:{port} expected {expect}")
        adapter.confirm()
        return ApplyResult("applied", text, skipped)
    except Exception as e:  # driver failure: restore last-known-good best-effort, report, never raise
        try:
            adapter.rollback()
        except Exception:
            pass
        return ApplyResult("rolled_back", detail=f"{type(e).__name__}: {e}")


def enrolment_allowed(adapter: GatewayAdapter | None) -> bool:
    """Router state is advisory. Enrolment never depends on it, whatever the router says or does."""
    try:
        if adapter is not None:
            adapter.discover()
    except Exception:
        pass
    return True
