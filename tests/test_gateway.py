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
        res = apply_plan(r, Policy(deny_lateral=False), [("workers", "wan", "443", True)], dry_run=False, owner_approved=True)
        self.assertEqual(res.status, "applied")
        self.assertEqual(r.lkg, OWNER)

    def test_diff_rejects_rules_outside_namespace(self):
        with self.assertRaises(ValueError):
            diff_plan([], [Rule("owner-x", "deny", "a", "b")])
        self.assertEqual(render_diff(diff_plan([], [])), "no changes")

    def test_bad_canary_rolls_back(self):
        r = FakeRouter(OWNER)
        r.blocked.add(("workers", "master", "8443"))  # canary expects reachable, router says no
        bad = [("workers", "wan", "443", True), ("workers", "master", "8443", True)]
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
        res = apply_plan(OpenWrtDriver(owner_approved=True), POLICY, CANARY_OK, dry_run=False, owner_approved=True)
        self.assertEqual(res.status, "error")  # nothing staged, so no rollback was attempted

    def test_dry_run_error_never_calls_rollback(self):
        for name in ("discover", "read_rules"):
            r = FakeRouter(OWNER)
            def boom(*a): raise OSError("down")
            setattr(r, name, boom)
            res = apply_plan(r, POLICY, CANARY_OK)
            self.assertEqual(res.status, "error")
            self.assertNotIn("rollback", r.calls)
            self.assertNotIn("stage", r.calls)
            self.assertEqual(r.lkg, OWNER)

    def test_error_before_stage_with_approval_never_rolls_back(self):
        r = FakeRouter(OWNER)
        def boom(*a): raise OSError("down")
        r.read_rules = boom
        res = apply_plan(r, POLICY, CANARY_OK, dry_run=False, owner_approved=True)
        self.assertEqual(res.status, "error")
        self.assertNotIn("rollback", r.calls)

    def test_empty_or_negative_only_canaries_refused(self):
        for canaries in ([], [("workers", "workers", "11436", False)]):
            r = FakeRouter(OWNER)
            res = apply_plan(r, POLICY, canaries, dry_run=False, owner_approved=True)
            self.assertEqual(res.status, "refused")
            self.assertNotIn("stage", r.calls)

    def test_window_expiring_during_canaries_is_not_applied(self):
        clock = Clock()
        r = FakeRouter(OWNER, now=clock)
        orig = r.probe
        def slow_probe(*a):
            out = orig(*a)
            clock.t += 100  # canary took longer than the confirm window
            return out
        r.probe = slow_probe
        res = apply_plan(r, POLICY, [("workers", "wan", "443", True)], dry_run=False, owner_approved=True)
        self.assertEqual(res.status, "reverted")
        self.assertEqual(r.lkg, OWNER)

    def test_window_expiring_at_confirm_is_not_applied(self):
        clock = Clock()
        r = FakeRouter(OWNER, now=clock)
        orig = r.confirm
        def late_confirm():
            clock.t += 100
            orig()
        r.confirm = late_confirm
        res = apply_plan(r, POLICY, CANARY_OK, dry_run=False, owner_approved=True)
        self.assertEqual(res.status, "reverted")
        self.assertEqual(r.lkg, OWNER)

    def test_owner_deny_beats_overlapping_fleet_allow(self):
        owner = [Rule("owner-deny", "deny", "workers", "wan", "tcp", "443")]
        r = FakeRouter(owner)
        res = apply_plan(r, POLICY, [("workers", "master", "8443", True)], dry_run=False, owner_approved=True)
        self.assertEqual(res.status, "applied")
        ids = [x.id for x in r.lkg]
        self.assertLess(ids.index("owner-deny"), ids.index(f"{NAMESPACE}wan-allow-wan-tcp-443"))
        self.assertFalse(r.probe("workers", "wan", "443"))

    def test_fleet_deny_stays_ahead_of_owner_catch_all_allow(self):
        owner = [Rule("owner-any", "allow", "any", "any")]
        r = FakeRouter(owner)
        res = apply_plan(r, Policy(), CANARY_OK, dry_run=False, owner_approved=True)
        self.assertEqual(res.status, "applied")
        self.assertEqual(r.lkg[0].id, f"{NAMESPACE}deny-lateral")
        self.assertEqual(r.lkg[1], owner[0])
        self.assertFalse(r.probe("workers", "workers", "11436"))
        self.assertTrue(r.probe("workers", "wan", "443"))

    def test_default_wan_deny_goes_last_after_fleet_allows(self):
        r = FakeRouter(OWNER)
        apply_plan(r, POLICY, CANARY_OK, dry_run=False, owner_approved=True)
        ids = [x.id for x in r.lkg]
        self.assertEqual(ids[-1], f"{NAMESPACE}wan-deny-default")
        self.assertLess(ids.index(f"{NAMESPACE}wan-allow-wan-tcp-443"), len(ids) - 1)

    def test_post_confirm_mismatch_reports_truthfully(self):
        r = FakeRouter(OWNER)
        orig = r.read_rules
        state = {"confirmed": False}
        oc = r.confirm
        def confirm():
            oc()
            state["confirmed"] = True
        def read():
            rules = orig()
            return rules + [Rule("surprise", "deny", "x", "y")] if state["confirmed"] else rules
        r.confirm, r.read_rules = confirm, read
        res = apply_plan(r, POLICY, CANARY_OK, dry_run=False, owner_approved=True)
        self.assertEqual(res.status, "needs_owner_check")
        self.assertEqual(r.calls.count("rollback"), 0)

    def test_duplicate_slug_ids_refused(self):
        pol = Policy(wan_allow=[("a.b", "tcp", "1"), ("a-b", "tcp", "1")])
        with self.assertRaises(ValueError):
            build_plan(pol, FakeRouter().discover())
        r = FakeRouter(OWNER)
        res = apply_plan(r, pol, CANARY_OK, dry_run=False, owner_approved=True)
        self.assertEqual(res.status, "error")
        self.assertNotIn("stage", r.calls)

    def test_unsupported_router_still_previews_diff(self):
        caps = Capabilities(reachable=True, vlan=True)  # vlan only
        res = apply_plan(FakeRouter(OWNER, caps=caps), POLICY, CANARY_OK)
        self.assertEqual(res.status, "unsupported")
        self.assertIn("+ aifleet-deny-lateral", res.diff)

    def test_allow_rule_ids_ignore_order(self):
        caps = FakeRouter().discover()
        a, _ = build_plan(Policy(wan_allow=[("a", "tcp", "1"), ("b", "udp", "2")]), caps)
        b, _ = build_plan(Policy(wan_allow=[("b", "udp", "2"), ("a", "tcp", "1")]), caps)
        self.assertEqual({x.id for x in a}, {x.id for x in b})

    def test_enrolment_does_not_call_router(self):
        r = FakeRouter(OWNER)
        self.assertTrue(enrolment_allowed(r))
        self.assertEqual(r.calls, [])

    def test_dry_run_notes_prefix_ownership(self):
        res = apply_plan(FakeRouter(OWNER), POLICY, CANARY_OK)
        self.assertIn("aifleet-", res.detail)

    def test_wan_default_deny_refused_without_allowlist(self):
        rules, skipped = build_plan(Policy(block_wan_default=True), FakeRouter().discover())
        self.assertFalse(any(x.id == f"{NAMESPACE}wan-deny-default" for x in rules))
        self.assertTrue(any("empty allowlist" in s for s in skipped))


if __name__ == "__main__":
    unittest.main()
