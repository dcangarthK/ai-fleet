"""Networking spec phase N2: read-only firewall posture collectors, the Ollama relay and /health schema.
Fakes only: command output comes from tests/fixtures/posture/, no real socket, pf, firewall or engine is touched.
Run on its own: python3 -m unittest tests/test_runner_posture.py"""
import importlib.util
import io
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "tests" / "fixtures" / "posture"
spec = importlib.util.spec_from_file_location("fleet_runner_posture", ROOT / "nodes" / "runner.py")
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)

LSOF = runner.LSOF
PF = runner.PFCTL
APP = (runner.SOCKETFILTER, "--getglobalstate")
NETSTAT = runner.NETSTAT4
NETSTAT6 = runner.NETSTAT6
PROFILES = runner.NETSH_PROFILES
RULES = runner.NETSH_RULES


def fixture(name):
    return (FIX / name).read_text()


class FakeRun:
    """A command runner answering from a table; records every argv so tests can assert what was run.
    A value of None models a permission error or a missing command."""
    def __init__(self, table):
        self.table, self.calls = table, []

    def __call__(self, argv, timeout=3):
        self.calls.append(tuple(argv))
        return self.table.get(tuple(argv))


def mac(lsof="lsof_good.txt", pf="pfctl_enabled.txt", app="socketfilterfw_on.txt"):
    return FakeRun({LSOF: fixture(lsof) if lsof else None, PF: fixture(pf) if pf else None,
                    APP: fixture(app) if app else None})


def windows(rules="netsh_rules_good.txt", profiles="netsh_profiles_on.txt", netstat="netstat_win_good.txt",
            netstat6="netstat_win_good6.txt"):
    return FakeRun({NETSTAT: fixture(netstat) if netstat else None,
                    NETSTAT6: fixture(netstat6) if netstat6 else None,
                    PROFILES: fixture(profiles) if profiles else None,
                    RULES: fixture(rules) if rules else None})


def collect(run, platform):
    return runner.collect_posture(run, platform=platform, wall=lambda: 1000.0)


class ListenerParsing(unittest.TestCase):
    def test_lsof_ipv4_ipv6_tailnet_wildcard(self):
        rows = runner.parse_listeners(fixture("lsof_good.txt"))
        by_port = {(r["port"], r["family"]): r["addr"] for r in rows}
        self.assertEqual(by_port[(11434, 4)], "127.0.0.1")
        self.assertEqual(by_port[(11434, 6)], "::1")
        self.assertEqual(by_port[(11436, 4)], "100.101.102.103")
        self.assertEqual(by_port[(49152, 4)], "*")

    def test_wildcard_star_on_ipv6_row_is_family_6(self):
        rows = runner.parse_listeners(fixture("lsof_ipv6_wildcard.txt"))
        self.assertIn({"addr": "*", "port": 11436, "family": 6}, rows)

    def test_windows_netstat(self):
        rows = runner.parse_listeners(fixture("netstat_win_good.txt") + fixture("netstat_win_good6.txt"))
        self.assertIn({"addr": "100.88.77.66", "port": 11436, "family": 4}, rows)
        self.assertIn({"addr": "::", "port": 445, "family": 6}, rows)

    def test_ss_output(self):
        rows = runner.parse_listeners("LISTEN 0 4096 127.0.0.1:11434 0.0.0.0:*\nLISTEN 0 4096 [::]:11436 [::]:*\n")
        self.assertEqual([(r["addr"], r["port"]) for r in rows], [("127.0.0.1", 11434), ("::", 11436)])

    def test_nothing_parses_to_none(self):
        self.assertIsNone(runner.parse_listeners(None))
        self.assertIsNone(runner.parse_listeners(""))

    def test_address_kinds(self):
        kinds = {a: runner.address_kind(a) for a in ("127.0.0.1", "::1", "*", "0.0.0.0", "::", "100.101.102.103",
                                                     "fd7a:115c:a1e0::1", "192.168.1.20", "10.0.0.5")}
        self.assertEqual(kinds, {"127.0.0.1": "loopback", "::1": "loopback", "*": "wildcard", "0.0.0.0": "wildcard",
                                 "::": "wildcard", "100.101.102.103": "tailnet", "fd7a:115c:a1e0::1": "tailnet",
                                 "192.168.1.20": "other", "10.0.0.5": "other"})


class MacPosture(unittest.TestCase):
    def test_pf_enabled_without_inspected_rules_is_a_warning_never_a_pass(self):
        out = collect(mac(), "darwin")
        self.assertEqual((out["status"], out["backend"]), ("warn", "macos-pf"))
        self.assertTrue(any("pf rules not inspected" in d for d in out["details"]))
        self.assertEqual((out["runner_tailnet_only"], out["engine_loopback_only"], out["ipv6_wildcard"]),
                         (True, True, False))
        self.assertEqual((out["checked_at"], out["age_s"]), (1000.0, 0.0))

    def test_application_firewall_alone_is_a_warning_it_cannot_limit_sources(self):
        out = collect(mac(pf="pfctl_disabled.txt"), "darwin")
        self.assertEqual((out["status"], out["backend"]), ("warn", "macos-application-firewall"))
        self.assertTrue(any("cannot restrict by source" in d for d in out["details"]))

    def test_both_disabled_warns(self):
        out = collect(mac(pf="pfctl_disabled.txt", app="socketfilterfw_off.txt"), "darwin")
        self.assertEqual(out["status"], "warn")
        self.assertIn("no host firewall enabled", out["details"])

    def test_pf_unreadable_and_app_firewall_off_warns(self):
        out = collect(mac(pf=None, app="socketfilterfw_off.txt"), "darwin")
        self.assertEqual((out["status"], out["backend"]), ("warn", "macos-application-firewall"))
        self.assertTrue(any("pf unreadable" in d for d in out["details"]))
        self.assertIn("pf: unreadable", out["details"])

    def test_pf_permission_denied_with_app_firewall_on_still_reports_it(self):
        out = collect(mac(pf=None), "darwin")
        self.assertEqual((out["status"], out["backend"]), ("warn", "macos-application-firewall"))

    def test_everything_unreadable_is_unknown_not_inferred(self):
        out = collect(mac(lsof=None, pf=None, app=None), "darwin")
        self.assertEqual(out["status"], "unknown")
        self.assertEqual((out["runner_tailnet_only"], out["engine_loopback_only"], out["ipv6_wildcard"]),
                         (None, None, None))

    def test_engine_on_wildcard_fails(self):
        out = collect(mac(lsof="lsof_engine_wildcard.txt"), "darwin")
        self.assertEqual((out["status"], out["engine_loopback_only"]), ("fail", False))
        self.assertTrue(any("engine port 11434 bound to *" in d for d in out["details"]))

    def test_runner_on_wildcard_fails(self):
        out = collect(mac(lsof="lsof_runner_lan.txt"), "darwin")
        self.assertEqual((out["status"], out["runner_tailnet_only"]), ("fail", False))

    def test_ipv6_wildcard_listener_fails(self):
        out = collect(mac(lsof="lsof_ipv6_wildcard.txt"), "darwin")
        self.assertEqual(out["status"], "fail")
        self.assertTrue(out["ipv6_wildcard"])

    def test_no_engine_seen_is_unknown_not_loopback_only(self):
        """lsof as a normal user misses other users' sockets: no engine row must not read as loopback-only."""
        run = FakeRun({LSOF: "ruby 1 u 3u IPv4 0x1 0t0 TCP 100.101.102.103:11436 (LISTEN)\n",
                       PF: fixture("pfctl_enabled.txt"), APP: fixture("socketfilterfw_on.txt")})
        out = collect(run, "darwin")
        self.assertEqual((out["status"], out["engine_loopback_only"]), ("unknown", None))

    def test_custom_engine_port_is_watched(self):
        run = FakeRun({LSOF: "ollama 1 u 3u IPv4 0x1 0t0 TCP *:9999 (LISTEN)\n"
                       "x 1 u 3u IPv4 0x1 0t0 TCP 100.101.102.103:11436 (LISTEN)\n",
                       PF: fixture("pfctl_enabled.txt"), APP: fixture("socketfilterfw_on.txt")})
        out = runner.collect_posture(run, platform="darwin", engine_ports=(11434, 9999), wall=lambda: 1.0)
        self.assertEqual((out["status"], out["engine_loopback_only"]), ("fail", False))

    def test_runner_not_seen_is_unknown(self):
        run = FakeRun({LSOF: "ollama 1 u 3u IPv4 0x1 0t0 TCP 127.0.0.1:11434 (LISTEN)\n",
                       PF: fixture("pfctl_enabled.txt"), APP: fixture("socketfilterfw_on.txt")})
        out = collect(run, "darwin")
        self.assertEqual((out["status"], out["runner_tailnet_only"]), ("unknown", None))


class WindowsPosture(unittest.TestCase):
    def test_pass_with_scoped_runner_rule(self):
        out = collect(windows(), "win32")
        self.assertEqual((out["status"], out["backend"]), ("pass", "windows-defender"))

    def test_engine_ports_admitted_fails(self):
        out = collect(windows(rules="netsh_rules_engine_open.txt"), "win32")
        self.assertEqual(out["status"], "fail")
        self.assertTrue(any("engine port(s) 11434, 11435" in d for d in out["details"]))

    def test_ipv6_wildcard_engine_fails(self):
        out = collect(windows(netstat6="netstat_win_wildcard6.txt"), "win32")
        self.assertEqual(out["status"], "fail")
        self.assertTrue(out["ipv6_wildcard"])

    def test_unreadable_ipv6_table_is_unknown_not_pass(self):
        out = collect(windows(netstat6=None), "win32")
        self.assertEqual(out["status"], "unknown")

    def test_app_rule_for_another_program_does_not_fail(self):
        out = collect(windows(rules="netsh_rules_program_app.txt"), "win32")
        self.assertEqual(out["status"], "pass", out["details"])

    def test_any_port_rule_for_an_engine_program_fails(self):
        out = collect(windows(rules="netsh_rules_engine_program.txt"), "win32")
        self.assertEqual(out["status"], "fail")

    def test_runner_rule_from_a_lan_subnet_warns(self):
        out = collect(windows(rules="netsh_rules_runner_subnet.txt"), "win32")
        self.assertEqual(out["status"], "warn")
        self.assertTrue(any("outside the master's tailnet" in d for d in out["details"]))

    def test_runner_rule_from_whole_tailnet_subnet_warns(self):
        out = collect(windows(rules="netsh_rules_runner_tailnet_subnet.txt"), "win32")
        self.assertEqual(out["status"], "warn")
        self.assertTrue(any("outside the master's tailnet" in d for d in out["details"]), out["details"])

    def test_remote_entry_forms(self):
        for entry in ("100.64.1.2", "100.64.1.2/32", "100.64.1.2/255.255.255.255",
                      "100.64.1.1-100.64.1.1", "fd7a:115c:a1e0::1/128"):
            self.assertTrue(runner._remote_in_tailnet(entry), entry)
        for entry in ("Any", "LocalSubnet", "Internet", "0.0.0.0/0", "192.168.0.0/16", "100.0.0.0/8", "100.64.0.0/10", "100.64.1.0/24",
                      "100.64.1.1-100.64.1.9", "fd7a:115c:a1e0::/48",
                      "100.64.1.1-101.0.0.1", "garbage"):
            self.assertFalse(runner._remote_in_tailnet(entry), entry)

    def test_runner_allowed_from_any_source_warns_and_disabled_rules_are_ignored(self):
        out = collect(windows(rules="netsh_rules_runner_any.txt"), "win32")
        self.assertEqual(out["status"], "warn")
        self.assertTrue(any("outside the master's tailnet" in d for d in out["details"]))
        self.assertFalse(any("engine port" in d for d in out["details"]))

    def test_profile_off_warns(self):
        out = collect(windows(profiles="netsh_profiles_public_off.txt"), "win32")
        self.assertEqual(out["status"], "warn")
        self.assertTrue(any("Defender Firewall off for: public" in d for d in out["details"]))

    def test_unreadable_defender_is_unknown(self):
        out = collect(windows(profiles=None, rules=None), "win32")
        self.assertEqual((out["status"], out["backend"]), ("unknown", "unknown"))

    def test_rules_unreadable_warns_instead_of_passing(self):
        out = collect(windows(rules=None), "win32")
        self.assertEqual(out["status"], "warn")

    def test_port_range_rule_covers_engine_port(self):
        self.assertEqual(runner._ports_admitted(["11430-11440"], (11434, 12434)), {11434})
        self.assertEqual(runner._ports_admitted(["Any"], (11434,)), {11434})
        self.assertEqual(runner._ports_admitted(["80,443"], (11434,)), set())


class ReadOnly(unittest.TestCase):
    def test_only_allowlisted_commands_run_on_every_platform(self):
        for platform, run in (("darwin", mac()), ("win32", windows()), ("linux", FakeRun({}))):
            collect(run, platform)
            self.assertTrue(run.calls, platform)
            for argv in run.calls:
                self.assertIn(argv, runner.POSTURE_COMMANDS, "%s ran %s" % (platform, argv))

    def test_allowlist_has_no_mutating_verb(self):
        for argv in runner.POSTURE_COMMANDS:
            text = " ".join(argv).lower()
            for bad in ("-f ", "-e", "enable", "disable", " add", " delete", " set ", "--setglobalstate", "-a ",
                        "pfctl -f", "pfctl -e", "pfctl -d"):
                self.assertNotIn(bad, " " + text + " ")

    def test_guard_refuses_a_mutating_command(self):
        run = FakeRun({})
        guarded = runner.read_only_runner(run)
        for argv in (["pfctl", "-e"], ["pfctl", "-f", "/etc/pf.conf"], [runner.SOCKETFILTER, "--setglobalstate", "off"],
                     ["netsh", "advfirewall", "set", "allprofiles", "state", "off"],
                     ["netsh", "advfirewall", "firewall", "add", "rule", "name=x"], ["sudo", "pfctl", "-d"]):
            with self.assertRaises(PermissionError):
                guarded(argv)
        self.assertEqual(run.calls, [], "a refused command never reaches the runner")

    def test_collector_that_is_handed_a_mutating_run_cannot_use_it_for_other_commands(self):
        seen = []

        def run(argv, timeout=3):
            seen.append(tuple(argv))
            return None
        collect(run, "darwin")
        self.assertTrue(all(a in runner.POSTURE_COMMANDS for a in seen))


class CommandRunnerContract(unittest.TestCase):
    def test_read_only_runner_passes_timeout_through_and_windows_rules_get_the_long_one(self):
        timeouts = {}

        def run(argv, timeout=None):
            timeouts[tuple(argv)] = timeout
            return None
        runner.collect_posture(run, platform="win32", wall=lambda: 1.0)
        self.assertEqual(timeouts[RULES], runner.POSTURE_RULES_TIMEOUT)
        self.assertEqual(timeouts[runner.NETSTAT4], runner.POSTURE_TIMEOUT)
        self.assertEqual(timeouts[runner.NETSTAT6], runner.POSTURE_TIMEOUT)

    def test_every_command_is_an_absolute_path(self):
        import os
        for argv in runner.POSTURE_COMMANDS:
            self.assertTrue(os.path.isabs(argv[0]) or argv[0].startswith("C:") or argv[0][1:3] == ":\\", argv)

    def test_windows_listing_needs_both_families(self):
        for kwargs in ({"netstat": None}, {"netstat6": None}):
            self.assertEqual(collect(windows(**kwargs), "win32")["status"], "unknown", kwargs)


class Serialised(unittest.TestCase):
    def test_check_now_and_background_collection_never_overlap(self):
        import threading
        import time
        from unittest import mock
        active, peak, guard = [0], [0], threading.Lock()

        def run(argv, timeout=3):
            with guard:
                active[0] += 1
                peak[0] = max(peak[0], active[0])
            time.sleep(0.01)
            with guard:
                active[0] -= 1
            return None
        collector = runner.PostureCollector(run=run, platform="darwin", wall=lambda: 1.0, mono=lambda: 0.0)
        with mock.patch.object(runner, "POSTURE_MIN_GAP", 0):
            threads = [threading.Thread(target=collector.once), threading.Thread(target=collector.check_now),
                       threading.Thread(target=collector.check_now)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        self.assertEqual(peak[0], 1)


class Collector(unittest.TestCase):
    def make(self, run, clock):
        wall = lambda: 5000.0 + clock[0]
        return runner.PostureCollector(run=run, platform="darwin", wall=wall, mono=lambda: clock[0])

    def test_snapshot_before_first_collection_is_unknown(self):
        snap = self.make(mac(), [0.0]).snapshot()
        self.assertEqual((snap["status"], snap["age_s"], snap["checked_at"]), ("unknown", None, None))

    def test_age_grows_from_the_observation(self):
        clock = [0.0]
        collector = self.make(mac(), clock)
        collector.once()
        clock[0] = 42.5
        snap = collector.snapshot()
        self.assertEqual((snap["status"], snap["age_s"], snap["checked_at"]), ("warn", 42.5, 5000.0))

    def test_check_now_is_rate_limited_and_read_only(self):
        clock, run = [0.0], mac()
        collector = self.make(run, clock)
        collector.check_now()
        first = len(run.calls)
        collector.check_now()
        self.assertEqual(len(run.calls), first, "a second check inside the minimum gap reuses the observation")
        clock[0] = runner.POSTURE_MIN_GAP + 1
        collector.check_now()
        self.assertGreater(len(run.calls), first)
        self.assertTrue(all(argv in runner.POSTURE_COMMANDS for argv in run.calls))

    def test_exception_in_a_command_becomes_unknown_and_does_not_raise(self):
        def broken(argv, timeout=3):
            raise PermissionError("denied")
        snap = self.make(broken, [0.0]).once()
        self.assertEqual(snap["status"], "unknown")
        self.assertIn("collector error: PermissionError", snap["details"])

    def test_slow_collector_does_not_delay_health(self):
        """/health only reads the cache: a collector that has not finished leaves an unknown snapshot at once."""
        collector = self.make(mac(), [0.0])
        manager = type("M", (), {"posture": collector})()
        self.assertEqual(runner.posture_of(manager)["status"], "unknown")
        self.assertEqual(runner.posture_of(object())["details"], ["posture collector not running"])


class HealthSchema(unittest.TestCase):
    KEYS = {"checked_at", "age_s", "status", "backend", "runner_tailnet_only", "engine_loopback_only",
            "ipv6_wildcard", "details"}

    def test_unknown_object_has_the_documented_fields(self):
        self.assertEqual(set(runner.posture_unknown()), self.KEYS)

    def test_collected_object_has_the_documented_fields_and_json_serialises(self):
        out = collect(mac(), "darwin")
        self.assertEqual(set(out), self.KEYS)
        self.assertIn(out["status"], ("pass", "warn", "fail", "unknown"))
        self.assertIn(out["backend"], ("macos-pf", "macos-application-firewall", "windows-defender", "unknown"))
        json.dumps(out)

    def test_no_secret_or_process_identity_in_the_object(self):
        text = json.dumps(collect(mac(), "darwin")).lower()
        for leaked in ("ollama", "python3", "dev ", "pid", "0x1a2b"):
            self.assertNotIn(leaked, text)


class FakeEngine:
    def __init__(self, reply):
        self.reply, self.requests = reply, []

    def __call__(self, request, timeout=None):
        self.requests.append(request)
        body = json.dumps(self.reply).encode()

        class Response(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *_):
                return None
        return Response(body)


class OllamaRelay(unittest.TestCase):
    def call(self, body, url="http://127.0.0.1:11434", reply=None):
        engine = FakeEngine(reply if reply is not None else {"message": {"content": "hi"}})
        return runner.ollama_relay(url, body, urlopen=engine), engine

    def chat(self, **over):
        body = {"model": "qwen3:8b", "stream": False, "messages": [{"role": "user", "content": "x"}]}
        body.update(over)
        return {"path": "/api/chat", "body": body}

    def test_chat_goes_to_the_loopback_engine_url_only(self):
        out, engine = self.call(self.chat())
        self.assertEqual(out, {"message": {"content": "hi"}})
        self.assertEqual(engine.requests[0].full_url, "http://127.0.0.1:11434/api/chat")

    def test_refuses_a_non_loopback_engine_url(self):
        with self.assertRaises(runner.HttpError) as ctx:
            self.call(self.chat(), url="http://100.101.102.103:11434")
        self.assertEqual(ctx.exception.status, 502)

    def test_refuses_other_paths_streaming_and_extra_keys(self):
        for body in ({"path": "/api/pull", "body": {"model": "x"}}, {"path": "/api/delete", "body": {"model": "x"}},
                     {"path": "/v1/models", "body": None}, {"path": "http://evil/api/chat", "body": {}},
                     self.chat(stream=True), {"path": "/api/chat", "body": {"model": "x:1", "stream": False,
                                                                           "messages": [], "system": "x", "template": "y"}},
                     self.chat(model="../../etc"), self.chat(model="")):
            with self.assertRaises(runner.HttpError, msg=str(body)) as ctx:
                self.call(body)
            self.assertEqual(ctx.exception.status, 400)

    def test_generate_only_allows_unload(self):
        self.call({"path": "/api/generate", "body": {"model": "m:1", "keep_alive": 0}})
        for body in ({"model": "m:1", "prompt": "go", "keep_alive": 0}, {"model": "m:1", "keep_alive": "5m"},
                     {"model": "m:1"}):
            with self.assertRaises(runner.HttpError):
                self.call({"path": "/api/generate", "body": body})

    def test_ps_and_tags_are_get_requests_without_a_body(self):
        for path in ("/api/ps", "/api/tags"):
            _, engine = self.call({"path": path, "body": {"model": "ignored"}})
            self.assertEqual((engine.requests[0].get_method(), engine.requests[0].data), ("GET", None))

    def test_engine_down_is_a_502(self):
        def down(request, timeout=None):
            raise OSError("refused")
        with self.assertRaises(runner.HttpError) as ctx:
            runner.ollama_relay("http://127.0.0.1:11434", self.chat(), urlopen=down)
        self.assertEqual(ctx.exception.status, 502)


class DmrRelay(unittest.TestCase):
    URL = "http://127.0.0.1:12434/engines/v1"

    def call(self, body, url=None, reply=None):
        engine = FakeEngine(reply if reply is not None else {"choices": []})
        return runner.dmr_relay(url or self.URL, body, urlopen=engine), engine

    def chat(self, **over):
        body = {"model": "ai/gemma3:4B-Q4_0", "stream": False, "temperature": 0, "seed": 1,
                "messages": [{"role": "user", "content": "x"}]}
        body.update(over)
        return {"path": "/chat/completions", "body": body}

    def test_chat_and_models_paths(self):
        _, engine = self.call(self.chat(response_format={"type": "json_object"}, max_tokens=5))
        self.assertEqual((engine.requests[0].full_url, engine.requests[0].get_method()),
                         (self.URL + "/chat/completions", "POST"))
        _, engine = self.call({"path": "/models", "body": {"model": "ignored"}})
        self.assertEqual((engine.requests[0].full_url, engine.requests[0].get_method(), engine.requests[0].data),
                         (self.URL + "/models", "GET", None))

    def test_rejections_are_400(self):
        for body in ({"path": "/api/chat", "body": {}}, {"path": "http://evil/models", "body": None},
                     self.chat(stream=True), self.chat(tools=[]), self.chat(messages="x"),
                     self.chat(model="../../etc"), self.chat(model=""), self.chat(model="a b"),
                     self.chat(model="a\nb"), self.chat(model=5)):
            with self.assertRaises(runner.HttpError, msg=str(body)) as ctx:
                self.call(body)
            self.assertEqual(ctx.exception.status, 400)

    def test_refuses_a_non_loopback_base(self):
        with self.assertRaises(runner.HttpError) as ctx:
            self.call(self.chat(), url="http://100.101.102.103:12434/engines/v1")
        self.assertEqual(ctx.exception.status, 502)

    def test_engine_down_is_a_502(self):
        def down(request, timeout=None):
            raise OSError("refused")
        with self.assertRaises(runner.HttpError) as ctx:
            runner.dmr_relay(self.URL, self.chat(), urlopen=down)
        self.assertEqual(ctx.exception.status, 502)


if __name__ == "__main__":
    unittest.main()
