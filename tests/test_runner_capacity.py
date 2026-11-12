"""Runner side of Part G requirement 1 (docs/SPEC-capacity-placement.md): live capacity in the heartbeat.
Claude usage comes from the newest stream-json `rate_limit_event` in jobs/<id>/events.jsonl, keyed by the job's
profile; Codex from its rollout `rate_limits`. Run: python3 -m unittest -v tests/test_runner_capacity.py
"""
import json
import os
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests.test_runner_budget import Base, runner  # noqa: E402


def event(five=0.42, seven=0.10, status="allowed", reset5=1763000000, reset7=1763500000, **extra):
    info = {"status": status, "unifiedWindows": {"five_hour": {"utilization": five, "resetsAt": reset5},
                                                "seven_day": {"utilization": seven, "resetsAt": reset7}}}
    info.update(extra)
    return {"type": "rate_limit_event", "rate_limit_info": info}


class ClaudeUsage(Base):
    def setUp(self):
        super().setUp()
        self.m.profile_config = {"claude": [{"id": "fleet", "dir": None, "label": None},
                                            {"id": "spare", "dir": None, "label": "spare"}],
                                 "codex": [{"id": "fleet", "dir": None, "label": None}]}

    def write_job(self, job_id, profile, lines, age_s=0, agent="claude"):
        folder = self.m.jobs_dir / job_id
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "state.json").write_text(json.dumps({"id": job_id, "agent": agent, "profile": profile,
                                                       "state": "exited"}))
        path = folder / "events.jsonl"
        path.write_text("\n".join(json.dumps(x) if isinstance(x, dict) else x for x in lines) + "\n")
        stamp = time.time() - age_s
        os.utime(path, (stamp, stamp))

    def usage(self, profile="fleet"):
        return {e["profile"]: e for e in self.m.capacity_report() if e["agent"] == "claude"}.get(profile)

    def test_newest_event_gives_both_windows_in_percent(self):
        self.write_job("j-1", "fleet", [{"type": "system"}, event(five=0.10), "not json", event(five=0.42, seven=0.5)])
        got = self.usage()["usage"]
        by = {w["window"]: w for w in got}
        self.assertAlmostEqual(by["5h"]["used_percent"], 42.0)
        self.assertAlmostEqual(by["weekly"]["used_percent"], 50.0)
        self.assertEqual((by["5h"]["resets_at"], by["weekly"]["resets_at"]), (1763000000, 1763500000))
        self.assertEqual(by["5h"]["source"], "stream-json")
        self.assertLess(by["5h"]["age_s"], 5)  # observed_at = the file's mtime

    def test_each_profile_has_its_own_usage_and_the_newest_job_wins(self):
        self.write_job("j-old", "spare", [event(five=0.9)], age_s=7200)
        self.write_job("j-new", "spare", [event(five=0.2)], age_s=60)
        self.write_job("j-fleet", "fleet", [event(five=0.5)], age_s=10)
        self.assertAlmostEqual(self.usage("spare")["usage"][0]["used_percent"], 20.0)
        self.assertAlmostEqual(self.usage("fleet")["usage"][0]["used_percent"], 50.0)
        self.assertGreater(self.usage("spare")["usage"][0]["age_s"], 59)

    def test_no_event_means_no_usage_not_a_guess(self):
        self.write_job("j-1", "fleet", [{"type": "system"}, {"type": "result", "result": "ok"}])
        self.assertEqual(self.usage()["usage"], [])
        self.assertIsNone(self.usage("nobody"))

    def test_other_agents_jobs_are_ignored(self):
        self.write_job("j-codex", "fleet", [event(five=0.99)], agent="codex")
        self.assertEqual(self.usage()["usage"], [])

    def test_rejected_event_reports_an_exact_reset(self):
        self.write_job("j-1", "fleet", [event(five=1.0, status="rejected", resetsAt=1763001234)])
        got = self.usage()
        self.assertEqual(got["rejected_until"], 1763001234)

    def test_utilization_out_of_range_or_wrong_type_is_dropped(self):
        self.write_job("j-1", "fleet", [event(five="lots", seven=-1)])
        self.assertEqual(self.usage()["usage"], [])

    def test_slots_free_is_max_jobs_minus_running(self):
        self.m.max_jobs = 4
        self.assertEqual(self.usage()["slots_free"], 4)
        self.m.jobs["j-run"] = {"id": "j-run", "state": "running", "agent": "claude"}
        try:
            self.assertEqual(self.usage()["slots_free"], 3)
        finally:
            self.m.jobs.pop("j-run")

    def test_codex_rate_limits_become_usage_windows(self):
        self.m.codex_rate_limits_report = lambda: [{"profile": "fleet", "seen_at": time.time() - 30,
                                                    "primary": {"used_percent": 3.0, "resets_at": 1762999999,
                                                                "window_minutes": 300},
                                                    "secondary": {"used_percent": 56.0, "resets_at": 1763500000,
                                                                  "window_minutes": 10080}}]
        codex = {e["profile"]: e for e in self.m.capacity_report() if e["agent"] == "codex"}["fleet"]
        by = {w["window"]: w for w in codex["usage"]}
        self.assertEqual((by["5h"]["used_percent"], by["weekly"]["used_percent"]), (3.0, 56.0))
        self.assertEqual(by["5h"]["source"], "rollout")
        self.assertGreaterEqual(by["5h"]["age_s"], 29)

    def test_health_report_carries_capacity(self):
        self.write_job("j-1", "fleet", [event()])
        runner.find_agents = lambda _url, **_kw: {"claude": None, "codex": None, "ollama": None}  # restored by Base
        report = runner.health_report(self.m)
        self.assertIn("capacity", report)
        self.assertTrue(any(e["agent"] == "claude" and e["usage"] for e in report["capacity"]))


if __name__ == "__main__":
    unittest.main()
