"""In-memory router for tests. Keeps its own state, so losing the controller changes nothing on it."""
from __future__ import annotations

from .adapter import Capabilities, GatewayAdapter, Rule


class FakeRouter(GatewayAdapter):
    def __init__(self, rules=None, caps: Capabilities | None = None, now=lambda: 0.0):
        self.caps = caps or Capabilities(reachable=True, vendor="fake", vlan=True, wan_egress_rules=True,
                                         staged_apply=True, rule_comments=True)
        self.lkg: list[Rule] = list(rules or [])     # last-known-good, the forwarding in effect when idle
        self.pending: list[Rule] | None = None
        self.deadline = 0.0
        self.now = now
        self.calls: list[str] = []
        self.blocked: set = set()                    # (src, dst, port) the canary must see as unreachable

    # time-driven auto revert, what a real router does when confirm never arrives
    def tick(self):
        if self.pending is not None and self.now() >= self.deadline:
            self.pending = None
            self.calls.append("auto_revert")

    def _active(self):
        return self.pending if self.pending is not None else self.lkg

    def discover(self):
        self.calls.append("discover")
        return self.caps

    def read_rules(self):
        self.calls.append("read_rules")
        self.tick()
        return list(self._active())

    def stage(self, rules, confirm_window_s):
        self.calls.append("stage")
        self.pending = list(rules)
        self.deadline = self.now() + confirm_window_s

    def confirm(self):
        self.calls.append("confirm")
        self.tick()
        if self.pending is not None:
            self.lkg, self.pending = self.pending, None

    def rollback(self):
        self.calls.append("rollback")
        self.pending = None

    def probe(self, src, dst, port):
        self.calls.append("probe")
        self.tick()
        if (src, dst, port) in self.blocked:
            return False
        verdict = None
        for r in self._active():  # first match wins, default allow like a plain router
            if r.src in (src, "any") and r.dst in (dst, "any") and r.port in (port, "any"):
                verdict = r.action
                break
        return verdict != "deny"
