"""Phase N3 Windows helpers and owner scripts use fixture data only."""

import importlib.util
import shutil
import subprocess
import tempfile
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

    def test_cidr_lan_ipv6_shorthand_or_empty_master_address_is_refused(self):
        for addresses in ([], ["100.64.0.0/10"], ["192.168.1.5"], ["fd7a:115c:a1e0::1"], ["100.64.1"]):
            with self.subTest(addresses=addresses):
                with self.assertRaises(ValueError):
                    networking.defender_rule(addresses)

    def test_owner_script_has_an_explicit_approval_gate_before_mutation(self):
        text = (WINDOWS / "set-defender-runner-rule.ps1").read_text()
        approval = text.index("if (-not $Approve -or $DryRun)")
        mutation = text.index("Remove-NetFirewallRule")
        self.assertLess(approval, mutation)
        self.assertIn("[switch]$DryRun", text)
        self.assertIn("-InterfaceAlias $TailscaleInterfaceAlias", text)
        self.assertIn("-Profile $profile", text)
        self.assertIn("$legacy | Remove-NetFirewallRule", text)
        self.assertIn("$helper rule @MasterTailnetIp", text)
        self.assertIn("-LocalPort 11436", text)
        self.assertNotIn("12434", text)

    @unittest.skipUnless(shutil.which("pwsh"), "PowerShell unavailable; static gate above is the fallback")
    def test_dry_run_stubs_prove_no_firewall_mutator_runs(self):
        script = WINDOWS / "set-defender-runner-rule.ps1"
        wrapper = """
function Get-NetConnectionProfile { [pscustomobject]@{ NetworkCategory = 'Private' } }
function Get-NetFirewallRule { @() }
function Remove-NetFirewallRule { throw 'MUTATOR CALLED' }
function New-NetFirewallRule { throw 'MUTATOR CALLED' }
& $args[0] -MasterTailnetIp 100.99.3.8 -TailscaleInterfaceAlias Tailscale -DryRun
"""
        result = subprocess.run([shutil.which("pwsh"), "-NoProfile", "-Command", wrapper, str(script)],
                                text=True, capture_output=True, timeout=20)
        self.assertEqual(0, result.returncode, result.stderr + result.stdout)
        self.assertNotIn("MUTATOR CALLED", result.stderr + result.stdout)


class DmrFixtureTests(unittest.TestCase):
    def test_fixture_table_is_dmr_catalog_with_unknown_fields_preserved(self):
        output = "MODEL ID SIZE\nai/qwen3:latest 7b 0123456789ab 4GB\nshared-name\n"
        self.assertEqual([
            {"engine": "dmr", "reference": "ai/qwen3:latest", "digest": "0123456789ab", "size": None,
             "warm": "unknown", "warm_source": None},
        ], networking.parse_docker_model_ls(output))


class RunnerAvailabilityTests(unittest.TestCase):
    def test_fake_logon_boot_and_service_cases_are_classified(self):
        logon = networking.runner_availability({"State": "Ready", "Triggers": ["LogonTrigger"], "RestartOnFailure": True}, None)
        boot = networking.runner_availability({"State": "Ready", "Triggers": ["BootTrigger"], "RestartOnFailure": True}, None)
        service = networking.runner_availability(None, {"Status": "Running", "StartType": "Automatic"})
        self.assertEqual("available-after-owner-logon", logon["availability"])
        self.assertEqual("unknown", boot["availability"])
        self.assertEqual("windows-service", service["source"])

    def test_unknown_task_never_claims_service_availability(self):
        result = networking.runner_availability({"State": "Ready", "Triggers": []}, None)
        self.assertEqual("unknown", result["availability"])

    def test_service_script_uses_the_same_helper(self):
        text = (WINDOWS / "test-runner-service.ps1").read_text()
        self.assertIn('"networking.py") availability', text)


class OnboardingHookTests(unittest.TestCase):
    def test_onboarding_preserves_compatibility_paths_and_opt_in_dmr_config(self):
        text = (ROOT / "nodes" / "onboard.ps1").read_text()
        self.assertIn('[switch]$EnableDmr', text)
        self.assertIn('"OLLAMA_HOST", "${ip}:11434"', text)
        self.assertIn('"OLLAMA_URL=http://${ip}:11434"', text)
        self.assertIn('base_url = "http://127.0.0.1:12434/engines/v1"', text)
        self.assertIn("Defender Firewall was not changed", text)
        self.assertIn("AI Fleet Info", text)
        self.assertIn("http://${ip}:11435/", text)


if __name__ == "__main__":
    unittest.main()
