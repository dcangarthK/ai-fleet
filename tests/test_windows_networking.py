"""Phase N3 Windows helpers and owner scripts use fixture data only."""

import importlib.util
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WINDOWS = ROOT / "nodes" / "windows"
spec = importlib.util.spec_from_file_location("windows_networking", WINDOWS / "networking.py")
networking = importlib.util.module_from_spec(spec)
spec.loader.exec_module(networking)


class DefenderProposalTests(unittest.TestCase):
    def test_only_runner_port_and_concrete_master_addresses_are_proposed(self):
        rule = networking.defender_rule(["100.99.3.8", "100.99.3.8", "100.72.1.4"])
        self.assertEqual([11436], rule["local_ports"])
        self.assertEqual(["100.99.3.8", "100.72.1.4"], rule["remote_addresses"])
        self.assertNotIn(11434, rule["local_ports"])
        self.assertNotIn(12434, rule["local_ports"])
        self.assertNotIn("100.64.0.0/10", rule["remote_addresses"])

    def test_cidr_lan_ipv6_or_empty_master_address_is_refused(self):
        for addresses in ([], ["100.64.0.0/10"], ["192.168.1.5"], ["fd7a:115c:a1e0::1"]):
            with self.subTest(addresses=addresses):
                with self.assertRaises(ValueError):
                    networking.defender_rule(addresses)

    def test_owner_script_has_an_explicit_approval_gate_before_mutation(self):
        text = (WINDOWS / "set-defender-runner-rule.ps1").read_text()
        approval = text.index("if (-not $Approve)")
        mutation = text.index("Remove-NetFirewallRule")
        self.assertLess(approval, mutation)
        self.assertIn("[switch]$DryRun", text)
        self.assertIn("if ($DryRun)", text)
        self.assertIn("-LocalPort 11436", text)
        self.assertNotIn("11434", text)
        self.assertNotIn("12434", text)
        self.assertNotIn("100.64.0.0/10", text)


class DmrFixtureTests(unittest.TestCase):
    def test_fixture_table_is_dmr_catalog_with_unknown_fields_preserved(self):
        output = "MODEL ID SIZE\nai/qwen3:latest sha256:abc 4GB\nshared-name\n"
        self.assertEqual([
            {"engine": "dmr", "reference": "ai/qwen3:latest", "digest": "sha256:abc"},
            {"engine": "dmr", "reference": "shared-name", "digest": None},
        ], networking.parse_docker_model_ls(output))


class RunnerAvailabilityTests(unittest.TestCase):
    def test_fake_logon_boot_and_service_cases_are_classified(self):
        logon = networking.runner_availability({"State": "Ready", "Triggers": ["LogonTrigger"], "RestartOnFailure": True}, None)
        boot = networking.runner_availability({"State": "Ready", "Triggers": ["BootTrigger"], "RestartOnFailure": True}, None)
        service = networking.runner_availability(None, {"Status": "Running", "StartType": "Automatic"})
        self.assertEqual("available-after-owner-logon", logon["availability"])
        self.assertEqual("available-before-owner-logon", boot["availability"])
        self.assertEqual("windows-service", service["source"])

    def test_unknown_task_never_claims_service_availability(self):
        result = networking.runner_availability({"State": "Ready", "Triggers": []}, None)
        self.assertEqual("unknown", result["availability"])


class OnboardingHookTests(unittest.TestCase):
    def test_onboarding_uses_loopback_and_opt_in_dmr_config(self):
        text = (ROOT / "nodes" / "onboard.ps1").read_text()
        self.assertIn('[switch]$EnableDmr', text)
        self.assertIn('"OLLAMA_HOST", "127.0.0.1:11434"', text)
        self.assertIn('"OLLAMA_URL=http://127.0.0.1:11434"', text)
        self.assertIn('base_url = "http://127.0.0.1:12434"', text)
        self.assertIn("Defender Firewall was not changed", text)
        self.assertNotIn("AI Fleet Info", text)
        self.assertNotIn("http://${ip}:11434", text)


if __name__ == "__main__":
    unittest.main()
