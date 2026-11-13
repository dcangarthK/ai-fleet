"""Apply with canary and rollback. Dry run by default; applying needs the explicit owner_approved flag.

Two approval flags exist on purpose: `apply_plan(owner_approved=...)` gates whether the controller may stage a change
at all; a real driver's own `owner_approved` gates whether that driver may talk to the router. Both must be set.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .adapter import NAMESPACE, Capabilities, GatewayAdapter
from .plan import Policy, build_plan, diff_plan, merge, render_diff

PREFIX_NOTE = f"rules whose id starts with {NAMESPACE!r} are treated as fleet-owned and may be replaced or removed"


@dataclass
class ApplyResult:
    # dry_run | applied | rolled_back | reverted | unsupported | unreachable | refused | noop | error
    status: str
    diff: str = ""
    skipped: list = field(default_factory=list)
    detail: str = ""


def apply_plan(adapter: GatewayAdapter, policy: Policy, canaries: list, *, owner_approved: bool = False,
               dry_run: bool = True, confirm_window_s: float = 60.0) -> ApplyResult:
    """canaries: [(src, dst, port, expect_reachable)]. A real apply needs at least one expect-reachable canary.
    Rollback is only ever called after this function itself staged a change."""
    staged = False
    try:
        caps: Capabilities = adapter.discover()
        if not caps.reachable:
            return ApplyResult("unreachable", detail="router not reachable; nothing changed")
        desired, skipped = build_plan(policy, caps)
        current = adapter.read_rules()
        text = render_diff(diff_plan(current, desired))
        if not caps.can_enforce:  # still show the owner what the supported part would do
            return ApplyResult("unsupported", text, skipped + ["router cannot enforce staged WAN policy"])
        if dry_run:
            return ApplyResult("dry_run", text, skipped, PREFIX_NOTE)
        if not owner_approved:
            return ApplyResult("refused", text, skipped, "owner approval flag not set")
        if not any(c[3] for c in canaries):
            return ApplyResult("refused", text, skipped, "need at least one canary expecting reachable")
        if text == "no changes":
            return ApplyResult("noop", text, skipped)
        want = merge(current, desired)
        adapter.stage(want, confirm_window_s)
        staged = True
        for src, dst, port, expect in canaries:
            if adapter.probe(src, dst, port) != expect:
                adapter.rollback()
                return ApplyResult("rolled_back", text, skipped, f"canary {src}->{dst}:{port} expected {expect}")
        if adapter.read_rules() != want:  # confirm window ended before canaries finished: they tested old rules
            adapter.rollback()
            return ApplyResult("reverted", text, skipped, "confirm window expired before canaries finished")
        adapter.confirm()
        if adapter.read_rules() != want:
            return ApplyResult("reverted", text, skipped, "router did not keep the confirmed rules")
        return ApplyResult("applied", text, skipped)
    except Exception as e:  # report, never raise; restore last-known-good only if we staged something
        if staged:
            try:
                adapter.rollback()
            except Exception:
                pass
            return ApplyResult("rolled_back", detail=f"{type(e).__name__}: {e}")
        return ApplyResult("error", detail=f"{type(e).__name__}: {e}; router untouched")


def enrolment_allowed(adapter: GatewayAdapter | None = None) -> bool:
    """Router state is advisory. Enrolment never depends on it and never calls the router."""
    return True
