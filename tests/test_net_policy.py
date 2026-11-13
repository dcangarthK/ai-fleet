"""Phase N0: tailnet policy matrix (SPEC-networking.md section 4) and TCP 11435 audit. Fakes/fixtures only.
Run alone: python3 -m unittest tests/test_net_policy.py"""
import contextlib
import io
import json
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
import importlib.util  # noqa: E402


def _load(name, path):
    # load by file path so sys.path order (e.g. master/controller/policy.py) can never shadow it
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


np = _load("fleet_netpolicy_net_policy", ROOT / "netpolicy" / "net_policy.py")
audit = _load("fleet_audit_11435", ROOT / "scripts" / "audit_11435.py")

FIX = ROOT / "tests" / "fixtures"
RAW_PORTS = [11434, 11435, 12434, 1234, 8000, 8080]
SRCS = ["tag:master", "tag:llm-node", "member", "admin", "untagged-node"]
DSTS = ["tag:master", "tag:llm-node"]


class PolicyMatrix(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.p = np.load_policy()

    def ok(self, src, dst, port):
        return np.allowed(self.p, src, dst, port)

    def test_file_parses_and_own_tests_pass(self):
        self.assertEqual(np.run_policy_tests(self.p), [])

    def test_master_to_worker_runner(self):
        self.assertTrue(self.ok("tag:master", "tag:llm-node", 11436))

    def test_worker_to_master_8443(self):
        self.assertTrue(self.ok("tag:llm-node", "tag:master", 8443))

    def test_member_to_master_8443(self):
        self.assertTrue(self.ok("member", "tag:master", 8443))

    def test_admin_ssh(self):
        for dst in DSTS:
            self.assertTrue(self.ok("admin", dst, 22))

    def test_non_admin_no_ssh(self):
        for src in ("tag:master", "tag:llm-node", "member", "untagged-node"):
            for dst in DSTS:
                self.assertFalse(self.ok(src, dst, 22), (src, dst))

    def test_raw_engine_and_inventory_ports_denied_for_everyone(self):
        for src in SRCS:
            for dst in DSTS:
                for port in RAW_PORTS:
                    self.assertFalse(self.ok(src, dst, port), (src, dst, port))

    def test_worker_to_worker_denied(self):
        for port in [11436, 22, 8443] + RAW_PORTS:
            self.assertFalse(self.ok("tag:llm-node", "tag:llm-node", port), port)

    def test_members_cannot_reach_workers(self):
        for src in ("member", "untagged-node"):
            for port in [11436, 8443] + RAW_PORTS:
                self.assertFalse(self.ok(src, "tag:llm-node", port), (src, port))

    def test_master_to_worker_only_runner(self):
        self.assertFalse(self.ok("tag:master", "tag:llm-node", 8443))

    def test_worker_to_master_only_8443(self):
        for port in [22, 8700, 4000, 11436] + RAW_PORTS:
            self.assertFalse(self.ok("tag:llm-node", "tag:master", port), port)

    def test_enrol_before_tag(self):
        self.assertTrue(self.ok("untagged-node", "tag:master", 8443))
        self.assertFalse(self.ok("untagged-node", "tag:llm-node", 11436))
        self.assertTrue(self.ok("tag:llm-node", "tag:master", 8443))

    def test_tagged_devices_are_not_members(self):
        pol = {"grants": [{"src": ["autogroup:member"], "dst": ["tag:master"], "ip": ["tcp:8443"]}]}
        self.assertFalse(np.allowed(pol, "tag:llm-node", "tag:master", 8443))

    def test_unknown_source_and_wildcards_fail_closed(self):
        self.assertFalse(self.ok("stranger", "tag:master", 8443))
        wild = {"grants": [{"src": ["autogroup:member"], "dst": ["tag:master"], "ip": ["*"]}]}
        self.assertFalse(np.allowed(wild, "member", "tag:master", 8443))

    def test_exact_allow_set_is_the_matrix(self):
        expected = {("tag:master", "tag:llm-node", 11436), ("tag:llm-node", "tag:master", 8443),
                    ("member", "tag:master", 8443), ("untagged-node", "tag:master", 8443),
                    ("admin", "tag:master", 8443),
                    ("admin", "tag:master", 22), ("admin", "tag:llm-node", 22)}
        got = {(s, d, port) for s in SRCS for d in DSTS
               for port in [22, 8443, 8700, 4000, 11436] + RAW_PORTS if np.allowed(self.p, s, d, port)}
        self.assertEqual(got, expected)

    def test_hujson_stripping(self):
        t = '{"a": "x // not comment", /* c */ "b": [1, 2,], // tail\n}'
        self.assertEqual(json.loads(np.strip_hujson(t)), {"a": "x // not comment", "b": [1, 2]})


class Audit11435(unittest.TestCase):
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
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(audit.main(["x", "--file", str(FIX / "lsof_11435_mixed.txt")]), 1)
            self.assertEqual(audit.main(["x", "--file", str(FIX / "lsof_11435_empty.txt")]), 0)


if __name__ == "__main__":
    unittest.main()
