"""Phase N4 optional gateway adapter (docs/SPEC-networking.md §9, §10 N4). Nothing here talks to a real router."""
from .adapter import (NAMESPACE, Capabilities, GatewayAdapter, Rule, RealDriverStub, OpenWrtDriver,
                      OPNsenseDriver)
from .fake import FakeRouter
from .plan import Policy, build_plan, diff_plan, render_diff
from .apply import ApplyResult, apply_plan, enrolment_allowed

__all__ = ["NAMESPACE", "Capabilities", "GatewayAdapter", "Rule", "RealDriverStub", "OpenWrtDriver",
           "OPNsenseDriver", "FakeRouter", "Policy", "build_plan", "diff_plan", "render_diff",
           "ApplyResult", "apply_plan", "enrolment_allowed"]
