"""Phase N4 gateway adapter (docs/SPEC-networking.md §10 N4). FakeRouter only; nothing touches a real router.
Run: python3 -m unittest -v tests/test_gateway.py
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "master" / "controller"))

from gateway import (NAMESPACE, Capabilities, FakeRouter, OpenWrtDriver, OPNsenseDriver, Policy, Rule,
                     apply_plan, build_plan, diff_plan, enrolment_allowed, render_diff)

OWNER = [Rule("owner-1", "deny", "guest", "lan"), Rule("owner-2", "allow", "lan", "wan", "tcp", "443")]
POLICY = Policy(wan_allow=[("wan", "tcp", "443")], block_wan_default=True)
CANARY_OK = [("workers", "wan", "443", True), ("workers", "workers", "11436", False)]


class Clock:
    t = 0.0
    def __call__(self): return self.t


class GatewayTests(unittest.TestCase):
    def test_dry_run_changes_nothing(self):
        r = FakeRouter(OWNER)
        res = apply_plan(r, POLICY, CANARY_OK)
        self.assertEqual(res.status, "dry_run")
        self.assertIn("+ aifleet-deny-lateral", res.diff)
        self.assertEqual(r.lkg, OWNER)
        self.assertNotIn("stage", r.calls)

    def test_apply_needs_owner_flag(self):
        r = FakeRouter(OWNER)
        self.assertEqual(apply_plan(r, POLICY, CANARY_OK, dry_run=False).status, "refused")
        self.assertEqual(r.lkg, OWNER)

    def test_unrelated_rules_survive_and_plan_is_namespaced(self):
        r = FakeRouter(OWNER)
        res = apply_plan(r, POLICY, CANARY_OK, dry_run=False, owner_approved=True)
        self.assertEqual(res.status, "applied")
        self.assertTrue(all(o in r.lkg for o in OWNER))
        self.assertTrue(all(x.managed for x in r.lkg if x not in OWNER))
        # removing the policy later removes only managed rules
        res = apply_plan(r, Policy(deny_lateral=False), [], dry_run=False, owner_approved=True)
        self.assertEqual(res.status, "applied")
        self.assertEqual(r.lkg, OWNER)

    def test_diff_rejects_rules_outside_namespace(self):
        with self.assertRaises(ValueError):
            diff_plan([], [Rule("owner-x", "deny", "a", "b")])
        self.assertEqual(render_diff(diff_plan([], [])), "no changes")

    def test_bad_canary_rolls_back(self):
        r = FakeRouter(OWNER)
        bad = [("workers", "wan", "443", False)]  # expects unreachable, but the allow rule makes it reachable
        res = apply_plan(r, POLICY, bad, dry_run=False, owner_approved=True)
        self.assertEqual(res.status, "rolled_back")
        self.assertEqual(r.lkg, OWNER)
        self.assertIsNone(r.pending)

    def test_driver_error_mid_apply_rolls_back(self):
        r = FakeRouter(OWNER)
        def boom(*a): raise RuntimeError("link dropped")
        r.probe = boom
        res = apply_plan(r, POLICY, CANARY_OK, dry_run=False, owner_approved=True)
        self.assertEqual(res.status, "rolled_back")
        self.assertEqual(r.lkg, OWNER)
        self.assertIsNone(r.pending)

    def test_controller_loss_keeps_last_known_good(self):
        clock = Clock()
        r = FakeRouter(OWNER, now=clock)
        apply_plan(r, POLICY, CANARY_OK, dry_run=False, owner_approved=True)
        good = list(r.lkg)
        # controller disappears after a confirmed apply: forwarding unchanged however long it is gone
        clock.t = 10_000
        self.assertEqual(r.read_rules(), good)
        # controller dies between stage and confirm: router reverts itself to last-known-good
        r.stage(OWNER, 60)
        clock.t += 61
        self.assertEqual(r.read_rules(), good)
        self.assertIn("auto_revert", r.calls)

    def test_missing_features_skip_without_failing(self):
        caps = Capabilities(reachable=True, vendor="fake")  # no vlan, no wan rules, no staging
        rules, skipped = build_plan(POLICY, caps)
        self.assertEqual(rules, [])
        self.assertEqual(len(skipped), 2)
        r = FakeRouter(OWNER, caps=caps)
        res = apply_plan(r, POLICY, CANARY_OK, dry_run=False, owner_approved=True)
        self.assertEqual(res.status, "unsupported")
        self.assertEqual(r.lkg, OWNER)

    def test_unreachable_router_never_blocks_enrolment(self):
        r = FakeRouter(OWNER, caps=Capabilities(reachable=False))
        self.assertEqual(apply_plan(r, POLICY, CANARY_OK).status, "unreachable")
        self.assertTrue(enrolment_allowed(r))
        self.assertTrue(enrolment_allowed(None))
        def boom(): raise OSError("down")
        r.discover = boom
        self.assertTrue(enrolment_allowed(r))

    def test_real_drivers_execute_nothing(self):
        for cls in (OpenWrtDriver, OPNsenseDriver):
            with self.assertRaises(PermissionError):
                cls().discover()
            d = cls(owner_approved=True)
            for call in (d.discover, d.read_rules, d.confirm, d.rollback):
                with self.assertRaises(NotImplementedError):
                    call()
            with self.assertRaises(NotImplementedError):
                d.stage([], 1)
            with self.assertRaises(NotImplementedError):
                d.probe("a", "b", "1")
        # a stub driver through apply_plan fails safe, no exception escapes
        res = apply_plan(OpenWrtDriver(owner_approved=True), POLICY, [], dry_run=False, owner_approved=True)
        self.assertEqual(res.status, "rolled_back")

    def test_wan_default_deny_refused_without_allowlist(self):
        rules, skipped = build_plan(Policy(block_wan_default=True), FakeRouter().discover())
        self.assertFalse(any(x.id == f"{NAMESPACE}wan-deny-default" for x in rules))
        self.assertTrue(any("empty allowlist" in s for s in skipped))


if __name__ == "__main__":
    unittest.main()
