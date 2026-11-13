"""Phase N0: tailnet policy matrix (SPEC-networking.md sections 4, 14) and TCP 11435 audit. Fakes/fixtures only.
Two files are checked: netpolicy/tailnet-policy.hujson (applied; keeps the master -> worker engine ports the
controller still uses) and netpolicy/tailnet-policy-target.hujson (deferred narrowed end state, not applied).
Enrol-before-tag here is an ACL lookup only, not a simulation of the enrolment flow.
Run alone: python3 -m unittest tests/test_net_policy.py"""
import contextlib
import copy
import importlib.util
import io
import json
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _load(name, path):
    # load by file path so sys.path order (e.g. master/controller/policy.py) can never shadow it
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


netpol = _load("fleet_netpolicy_net_policy", ROOT / "netpolicy" / "net_policy.py")
audit = _load("fleet_audit_11435", ROOT / "scripts" / "audit_11435.py")

FIX = ROOT / "tests" / "fixtures"
RAW_PORTS = [11434, 11435, 12434, 1234, 8000, 8080]
LEGACY_MASTER_PORTS = {11434, 11435, 11436, 8000, 1234, 8080}
SRCS = ["tag:master", "tag:llm-node", "member", "admin", "untagged-node"]
DSTS = ["tag:master", "tag:llm-node"]
PROBE = [22, 8443, 8700, 4000, 11436] + RAW_PORTS

COMMON = {("tag:llm-node", "tag:master", 8443), ("member", "tag:master", 8443),
          ("untagged-node", "tag:master", 8443), ("admin", "tag:master", 8443),
          ("admin", "tag:master", 22), ("admin", "tag:llm-node", 22)}
EXPECT_APPLIED = COMMON | {("tag:master", "tag:llm-node", p) for p in LEGACY_MASTER_PORTS}
EXPECT_TARGET = COMMON | {("tag:master", "tag:llm-node", 11436)}


class Matrix:
    expected = None
    path = None

    @classmethod
    def setUpClass(cls):
        cls.p = netpol.load_policy(cls.path)

    def ok(self, src, dst, port):
        return netpol.allowed(self.p, src, dst, port)

    def test_file_parses_and_own_tests_pass(self):
        self.assertEqual(netpol.run_policy_tests(self.p), [])

    def test_exact_allow_set_is_the_matrix(self):
        got = {(s, d, port) for s in SRCS for d in DSTS for port in PROBE if self.ok(s, d, port)}
        self.assertEqual(got, self.expected)

    def test_required_rows(self):
        self.assertTrue(self.ok("tag:master", "tag:llm-node", 11436))
        self.assertTrue(self.ok("tag:llm-node", "tag:master", 8443))
        self.assertTrue(self.ok("member", "tag:master", 8443))
        for dst in DSTS:
            self.assertTrue(self.ok("admin", dst, 22))

    def test_worker_to_worker_denied(self):
        for port in PROBE:
            self.assertFalse(self.ok("tag:llm-node", "tag:llm-node", port), port)

    def test_non_admin_no_ssh(self):
        for src in ("tag:master", "tag:llm-node", "member", "untagged-node"):
            for dst in DSTS:
                self.assertFalse(self.ok(src, dst, 22), (src, dst))

    def test_only_master_reaches_raw_ports_and_nothing_reaches_12434(self):
        for src in SRCS:
            for dst in DSTS:
                for port in RAW_PORTS:
                    if (src, dst) == ("tag:master", "tag:llm-node") and port in LEGACY_MASTER_PORTS - {11436}:
                        continue
                    self.assertFalse(self.ok(src, dst, port), (src, dst, port))

    def test_members_cannot_reach_workers(self):
        for src in ("member", "untagged-node"):
            for port in PROBE:
                self.assertFalse(self.ok(src, "tag:llm-node", port), (src, port))

    def test_worker_to_master_only_8443(self):
        for port in PROBE:
            self.assertEqual(self.ok("tag:llm-node", "tag:master", port), port == 8443, port)

    def test_enrol_before_tag_acl_lookup_only(self):
        self.assertTrue(self.ok("untagged-node", "tag:master", 8443))
        self.assertFalse(self.ok("untagged-node", "tag:llm-node", 11436))


class AppliedPolicy(Matrix, unittest.TestCase):
    expected = EXPECT_APPLIED
    path = netpol.POLICY_PATH

    def test_controller_ports_still_granted(self):
        for port in (11434, 11435, 8000, 1234):
            self.assertTrue(self.ok("tag:master", "tag:llm-node", port), port)


class TargetPolicy(Matrix, unittest.TestCase):
    expected = EXPECT_TARGET
    path = netpol.TARGET_PATH

    def test_every_grant_port_is_in_the_matrix(self):
        for g in self.p["grants"]:
            for spec in g["ip"]:
                self.assertIn(int(spec.split(":")[1]), {11436, 8443, 22}, spec)


class Strictness(unittest.TestCase):
    """Over-broad or unknown forms must raise, never be skipped as 'denied'."""

    def base(self):
        return copy.deepcopy(netpol.load_policy(netpol.TARGET_PATH))

    def assertRejected(self, mutate):
        p = self.base()
        mutate(p)
        with self.assertRaises(netpol.PolicyError):
            netpol.allowed(p, "tag:llm-node", "tag:llm-node", 11434)

    def test_ip_wildcard_forms(self):
        for form in ("*", "tcp:*", "tcp:11000-12000", "11434", "udp:11434", "tcp:99999x"):
            self.assertRejected(lambda p, f=form: p["grants"].append(
                {"src": ["tag:llm-node"], "dst": ["tag:llm-node"], "ip": [f]}))

    def test_selector_wildcards(self):
        for sel in ("*", "autogroup:tagged", "autogroup:internet", "group:x", "user@example.com"):
            self.assertRejected(lambda p, s=sel: p["grants"].append(
                {"src": [s], "dst": ["tag:llm-node"], "ip": ["tcp:11434"]}))
            self.assertRejected(lambda p, s=sel: p["grants"].append(
                {"src": ["tag:llm-node"], "dst": [s], "ip": ["tcp:11434"]}))

    def test_legacy_acls_and_unknown_keys(self):
        for key, val in (("acls", [{"action": "accept", "src": ["*"], "dst": ["*:*"]}]),
                         ("ssh", []), ("postures", {}), ("autoApprovers", {})):
            self.assertRejected(lambda p, k=key, v=val: p.__setitem__(k, v))

    def test_unknown_grant_key(self):
        self.assertRejected(lambda p: p["grants"].append(
            {"src": ["tag:llm-node"], "dst": ["tag:llm-node"], "ip": ["tcp:11434"], "app": {}}))

    def test_unknown_source_kind_raises(self):
        with self.assertRaises(netpol.PolicyError):
            netpol.allowed(self.base(), "stranger", "tag:master", 8443)

    def test_run_policy_tests_rejects_non_tag_source(self):
        p = self.base()
        p["tests"].append({"src": "owner@example.com", "accept": ["tag:master:8443"]})
        with self.assertRaises(netpol.PolicyError):
            netpol.run_policy_tests(p)

    def test_owner_identity_placeholder_stays_commented_out(self):
        text = (ROOT / "netpolicy" / "tailnet-policy.hujson").read_text()
        self.assertIn("OWNER_EMAIL", text)
        self.assertNotIn("OWNER_EMAIL", json.dumps(netpol.load_policy(netpol.POLICY_PATH)))

    def test_hujson_stripping(self):
        t = '{"a": "x // not comment", /* c */ "b": [1, 2,], // tail\n}'
        self.assertEqual(json.loads(netpol.strip_hujson(t)), {"a": "x // not comment", "b": [1, 2]})


def lsof_result(stdout="", stderr="", rc=0):
    return lambda cmd: (rc, stdout, stderr)


class Audit11435(unittest.TestCase):
    def run_main(self, argv, **kw):
        with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()):
            rc = audit.main(argv, **kw)
        return rc, out.getvalue()

    def test_parse_mixed_fixture(self):
        ls = audit.parse_lsof((FIX / "lsof_11435_mixed.txt").read_text())
        self.assertEqual([x["exposure"] for x in ls],
                         ["loopback", "tailnet", "wildcard", "loopback", "other"])
        self.assertEqual(ls[0]["pid"], "4101")

    def test_empty_fixture(self):
        ls = audit.parse_lsof((FIX / "lsof_11435_empty.txt").read_text())
        self.assertEqual(ls, [])
        self.assertIn("No listener", audit.report(ls))

    def test_classify(self):
        for host, want in [("127.0.0.1", "loopback"), ("[::1]", "loopback"), ("*", "wildcard"),
                           ("[::]", "wildcard"), ("100.64.0.1", "tailnet"), ("100.127.255.254", "tailnet"),
                           ("100.128.0.1", "other"), ("10.0.0.5", "other"), ("host", "other")]:
            self.assertEqual(audit.classify(host), want, host)

    def test_report_counts(self):
        ls = audit.parse_lsof((FIX / "lsof_11435_mixed.txt").read_text())
        self.assertIn("5 listener(s)", audit.report(ls))

    def test_command_is_read_only_listing(self):
        self.assertEqual(audit.LSOF_CMD[0], "lsof")
        self.assertIn("-sTCP:LISTEN", audit.LSOF_CMD)

    def test_main_with_fixture_files(self):
        self.assertEqual(self.run_main(["x", "--file", str(FIX / "lsof_11435_mixed.txt")])[0], 1)
        self.assertEqual(self.run_main(["x", "--file", str(FIX / "lsof_11435_empty.txt")])[0], 0)

    def test_live_root_empty_is_clean(self):
        rc, out = self.run_main(["x"], run=lsof_result(rc=1), euid=0)
        self.assertEqual(rc, 0)
        self.assertIn("No listener", out)

    def test_live_non_root_empty_is_unverified(self):
        rc, out = self.run_main(["x"], run=lsof_result(rc=1), euid=501)
        self.assertEqual(rc, 2)
        self.assertIn("UNVERIFIED", out)

    def test_live_non_root_with_wildcard_still_flags(self):
        text = (FIX / "lsof_11435_mixed.txt").read_text()
        self.assertEqual(self.run_main(["x"], run=lsof_result(text, rc=0), euid=501)[0], 1)

    def test_lsof_error_rc_or_stderr_is_unverified(self):
        self.assertEqual(self.run_main(["x"], run=lsof_result(rc=2), euid=0)[0], 2)
        self.assertEqual(self.run_main(["x"], run=lsof_result(stderr="lsof: WARNING", rc=1), euid=0)[0], 2)

    def test_missing_lsof_is_unverified(self):
        def boom(cmd):
            raise FileNotFoundError("lsof")
        self.assertEqual(self.run_main(["x"], run=boom, euid=0)[0], 2)


if __name__ == "__main__":
    unittest.main()
