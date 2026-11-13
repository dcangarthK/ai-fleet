"""Gateway adapter interface. Discovery is read-only; changes are staged, then confirmed or rolled back.

Real drivers are stubs: they raise NotImplementedError and need an explicit owner-approval flag even to be
constructed for use. No driver here opens a socket, runs a command or reads credentials.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

NAMESPACE = "aifleet-"  # every rule the fleet owns carries this id prefix; nothing else is ever touched


@dataclass(frozen=True)
class Rule:
    id: str
    action: str          # "allow" | "deny"
    src: str             # zone/VLAN/host label
    dst: str
    proto: str = "any"
    port: str = "any"

    @property
    def managed(self) -> bool:
        return self.id.startswith(NAMESPACE)


@dataclass
class Capabilities:
    """What discovery observed. Unknown stays None/False; nothing is inferred."""
    reachable: bool = False
    vendor: str = "unknown"
    vlan: bool = False
    wan_egress_rules: bool = False
    staged_apply: bool = False      # router supports apply-with-auto-revert (confirm window)
    rule_comments: bool = False
    notes: list = field(default_factory=list)

    @property
    def can_enforce(self) -> bool:
        return self.reachable and self.wan_egress_rules and self.staged_apply


class GatewayAdapter(ABC):
    @abstractmethod
    def discover(self) -> Capabilities:
        """Read-only. Must not change router state."""

    @abstractmethod
    def read_rules(self) -> list[Rule]:
        """Read-only: every rule on the router, managed or not."""

    @abstractmethod
    def stage(self, rules: list[Rule], confirm_window_s: float) -> None:
        """Install the full rule list as pending; auto-reverts to last-known-good unless confirm() arrives."""

    @abstractmethod
    def confirm(self) -> None:
        """Make the staged config the new last-known-good."""

    @abstractmethod
    def rollback(self) -> None:
        """Discard staged config, restore last-known-good."""

    @abstractmethod
    def probe(self, src: str, dst: str, port: str) -> bool:
        """Canary: can src reach dst:port right now? Read-only observation."""


class RealDriverStub(GatewayAdapter):
    """Base for real drivers. Not implemented on purpose; owner approval flag is required before any future use."""
    vendor = "unknown"

    def __init__(self, owner_approved: bool = False):
        self.owner_approved = owner_approved

    def _refuse(self, what: str):
        if not self.owner_approved:
            raise PermissionError(f"{self.vendor} driver: {what} needs owner approval flag")
        raise NotImplementedError(f"{self.vendor} driver: {what} is not implemented")

    def discover(self): self._refuse("discover")
    def read_rules(self): self._refuse("read_rules")
    def stage(self, rules, confirm_window_s): self._refuse("stage")
    def confirm(self): self._refuse("confirm")
    def rollback(self): self._refuse("rollback")
    def probe(self, src, dst, port): self._refuse("probe")


class OpenWrtDriver(RealDriverStub):
    vendor = "openwrt"


class OPNsenseDriver(RealDriverStub):
    vendor = "opnsense"
