"""Runner side of Part E (docs/SPEC-stall-reroute.md 4 and 12): root cause of t-253101a7 (a job outliving
max_job_minutes, GET /jobs/{id} answering 500), per-job activity, and the "jobs" list in /health.
Run: python3 -m unittest -v tests/test_runner_stall.py
"""
import importlib.util
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("fleet_runner_stall", ROOT / "nodes" / "runner.py")
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class CpuParsing(unittest.TestCase):
    def test_formats(self):
        parse = runner.parse_cpu_time
        self.assertEqual(parse("0:01.50"), 1.5)
        self.assertEqual(parse("12:34"), 754.0)
        self.assertEqual(parse("12:34.25"), 754.25)
        self.assertEqual(parse("1:02:03"), 3723.0)
        self.assertEqual(parse("1-02:03:04"), 86400 + 7384.0)
        self.assertEqual(parse("  0:00.00 "), 0.0)
        for bad in ("", "abc", "1:2:3:4", None, "-1:00"):
            self.assertIsNone(parse(bad))

    def test_group_cpu_sums_only_the_group(self):
        out = "  100 0:01.50\n  100 1:00\n  200 9:00\n  garbage\n  100 x\n"
        self.assertEqual(runner.group_cpu_seconds(100, run=lambda argv, timeout=3: out), 61.5)
        self.assertIsNone(runner.group_cpu_seconds(100, run=lambda argv, timeout=3: None))
        self.assertIsNone(runner.group_cpu_seconds(0, run=lambda argv, timeout=3: out))
        self.assertEqual({100: 61.5, 200: 540.0}, runner.group_cpu_map(run=lambda argv, timeout=3: out))


class Scan(unittest.TestCase):
    def test_skips_git_and_heavy_dirs_and_never_follows_symlinks(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(os.path.realpath(d)) / "wt"
            outside = Path(os.path.realpath(d)) / "outside"
            for p in (root / ".git", root / "node_modules", root / "src", outside):
                p.mkdir(parents=True)
            (root / "src" / "a.py").write_text("x")
            os.utime(root / "src" / "a.py", (100, 100))
            for p in (root / ".git" / "x", root / "node_modules" / "y"):
                p.write_text("z")
            (outside / "secret").write_text("z")  # newer than anything inside
            os.symlink(outside, root / "link")
            os.symlink(outside / "secret", root / "flink")
            newest, count, capped = runner.scan_worktree(root, 1000)
            self.assertEqual((100, False), (newest, capped))
            self.assertLess(count, 10)

    def test_a_nested_symlink_to_a_directory_outside_is_not_followed(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(os.path.realpath(d)) / "wt"
            outside = Path(os.path.realpath(d)) / "outside"
            (root / "sub").mkdir(parents=True)
            (outside / "deep").mkdir(parents=True)
            (outside / "deep" / "new").write_text("z")
            os.symlink(outside, root / "sub" / "escape")
            (root / "sub" / "a").write_text("x")
            os.utime(root / "sub" / "a", (50, 50))
            self.assertEqual((50, False), runner.scan_worktree(root, 1000)[::2])

    def test_a_root_that_is_a_symlink_is_not_scanned(self):
        with tempfile.TemporaryDirectory() as d:
            real = Path(os.path.realpath(d)) / "real"
            real.mkdir()
            (real / "f").write_text("x")
            link = Path(os.path.realpath(d)) / "link"
            os.symlink(real, link)
            newest, count, capped = runner.scan_worktree(link, 1000)
            self.assertEqual((0.0, 0, True), (newest, count, capped))

    def test_cap(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            for i in range(30):
                (root / ("f%d" % i)).write_text("x")
            self.assertTrue(runner.scan_worktree(root, 10)[2])
            self.assertFalse(runner.scan_worktree(root, 100)[2])


class Base(unittest.TestCase):
    def setUp(self):
        # a monitor thread may still be saving its job as the test ends
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.base = Path(os.path.realpath(self.temp.name))
        self.fleet = self.base / "fleet"
        self.fleet.mkdir()
        self.code = self.base / "code"
        self.code.mkdir()
        self.config = {"max_mode": "auto-edit", "allowed_roots": [str(self.code)]}

    def tearDown(self):
        self.temp.cleanup()

    def manager(self, **config):
        (self.fleet / "runner.json").write_text(json.dumps(self.config | config))
        return runner.JobManager(self.fleet, "http://127.0.0.1:1")

    def add_job(self, m, job_id="j-1", **kw):
        job = {"id": job_id, "agent": "codex", "mode": "auto-edit", "runs": 1, "session_id": "s", "model": None,
               "policy": {}, "state": "running", "pid": 999999, "exit_file": "exit_code", "cwd": str(self.code),
               "started_at": runner.now(), "run_started_at": runner.now(), "timeout_minutes": 120}
        job.update(kw)
        (self.fleet / "jobs" / job_id).mkdir(parents=True, exist_ok=True)
        m.jobs[job_id] = job
        return job

    def events(self, job_id, text, mode="w"):
        with open(self.fleet / "jobs" / job_id / "events.jsonl", mode, encoding="utf-8") as handle:
            handle.write(text)


class RootCause(Base):
    """t-253101a7: the run outlived max_job_minutes and /jobs/{id} answered 500. Two defects, both fixed:
    (1) _parse_events assumed every events.jsonl line is a JSON object (stderr lines such as `null` or `7` parse
    as JSON but are not), so refresh() raised; in the monitor thread that killed the thread before it could write
    the timeout; (2) the deadline was enforced only by that one thread, so nothing else could notice."""

    def test_non_object_event_lines_do_not_break_refresh(self):
        m = self.manager()
        job = self.add_job(m)
        self.events(job["id"], 'null\n7\n"text"\n[1]\n{"type": "item.completed", "item": "oops"}\n'
                    '{"type": "item.completed", "item": {"type": "agent_message", "text": "done"}}\n')
        public = m.public(job)  # pid 999999 is gone: running -> lost, parsing the events
        self.assertEqual("lost", public["state"])
        self.assertFalse(public["alive"])
        self.assertEqual("done", public["result"])

    def test_dead_job_never_raises_even_if_parsing_does(self):
        m = self.manager()
        job = self.add_job(m)
        with mock.patch.object(m, "_parse_events", side_effect=RuntimeError("boom")):
            public = m.public(job)
        self.assertEqual("lost", public["state"])
        self.assertEqual("lost", runner.read_json(self.fleet / "jobs" / job["id"] / "state.json")["state"])

    def test_deadline_is_enforced_without_the_monitor_thread(self):
        """No monitor thread exists for this job (as after a lost thread): a poll still applies the timeout."""
        m = self.manager(max_job_minutes=1)
        started = runner.datetime.datetime.fromtimestamp(time.time() - 3 * 3600).astimezone().isoformat()
        job = self.add_job(m, started_at=started, run_started_at=started, timeout_minutes=1, pid=os.getpid())
        with mock.patch.object(m, "_kill_group") as kill:
            public = m.public(job)
        kill.assert_called_once()
        self.assertEqual("exited", public["state"])
        self.assertEqual(124, public["exit_code"])

    def test_timeout_survives_an_exception_in_event_parsing(self):
        m = self.manager()
        job = self.add_job(m)
        with mock.patch.object(m, "_kill_group"), mock.patch.object(m, "_parse_events", side_effect=ValueError("x")):
            m._timeout(job)
        self.assertEqual(("exited", 124), (job["state"], job["exit_code"]))

    def test_monitor_thread_survives_an_exception(self):
        m = self.manager()
        started = runner.datetime.datetime.fromtimestamp(time.time() - 10).astimezone().isoformat()
        job = self.add_job(m, pid=os.getpid(), started_at=started, run_started_at=started, timeout_minutes=0.01)
        calls = []
        real = m._timeout

        def flaky(j):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("first attempt dies")
            real(j)

        with mock.patch.object(m, "_timeout", side_effect=flaky), mock.patch.object(m, "_kill_group"):
            m._start_monitor(job)
            end = time.time() + 5
            while time.time() < end and job["state"] == "running":
                time.sleep(0.05)
        self.assertGreaterEqual(len(calls), 2)
        self.assertEqual("exited", job["state"])

    def test_monitor_enforces_the_deadline_while_alive_keeps_raising(self):
        m = self.manager()
        started = runner.datetime.datetime.fromtimestamp(time.time() - 10).astimezone().isoformat()
        job = self.add_job(m, pid=os.getpid(), started_at=started, run_started_at=started, timeout_minutes=0.01)

        class Broken:
            def poll(self):
                raise OSError("poll broke")

            def wait(self, timeout=None):
                return 0

        audits = []
        with mock.patch.object(m, "_kill_group"), mock.patch.object(m, "audit", side_effect=lambda *a: audits.append(a)):
            m._start_monitor(job, Broken())
            end = time.time() + 5
            while time.time() < end and job["state"] == "running":
                time.sleep(0.05)
            self.assertEqual(("exited", 124), (job["state"], job.get("exit_code")))

    def test_monitor_errors_are_rate_limited(self):
        m = self.manager()
        job = self.add_job(m, pid=os.getpid())  # far from its deadline

        class Broken:
            def poll(self):
                raise OSError("poll broke")

            def wait(self, timeout=None):
                return 0

        audits = []
        with mock.patch.object(m, "audit", side_effect=lambda *a: audits.append(a)):
            m._start_monitor(job, Broken())
            time.sleep(1.5)  # about 7 loop passes
            job["state"] = "cancelled"  # ends the monitor
            time.sleep(0.5)
        self.assertEqual(1, len([a for a in audits if a[0] == "monitor_error"]))

    def test_timeout_falls_back_to_killing_the_pid_when_the_group_kill_fails(self):
        m = self.manager()
        job = self.add_job(m, pid=4242)
        audits = []
        with mock.patch.object(m, "_kill_group", side_effect=PermissionError("nope")), \
                mock.patch.object(runner, "process_alive", return_value=True), \
                mock.patch.object(runner.os, "kill") as kill, \
                mock.patch.object(m, "audit", side_effect=lambda *a: audits.append(a)):
            m._timeout(job)
        kill.assert_called_once_with(4242, runner.signal.SIGKILL)
        self.assertIn("kill_failed", [a[0] for a in audits])
        self.assertEqual(("exited", 124), (job["state"], job["exit_code"]))

    def test_restart_with_a_poisoned_job_still_loads(self):
        m = self.manager()
        job = self.add_job(m)
        m._save(job)
        self.events(job["id"], "null\n")
        again = runner.JobManager(self.fleet, "http://127.0.0.1:1")  # must not raise
        self.assertEqual("lost", again.jobs[job["id"]]["state"])


class Activity(Base):
    def setUp(self):
        super().setUp()
        self.m = self.manager()
        self.clock = [1000.0]
        self.m.clock = lambda: self.clock[0]
        self.cpu = [0.0]
        self.ps_calls = []

        def probe():
            self.ps_calls.append(1)
            return {os.getpid(): self.cpu[0]}
        self.m.cpu_probe = probe
        self.job = self.add_job(self.m, pid=os.getpid())

    def entry(self):
        return self.m.jobs_report()[0]

    def tick(self, seconds):
        self.clock[0] += seconds

    def test_starts_active_and_idle_s_grows_on_the_runner_clock(self):
        self.assertEqual(0, self.entry()["idle_s"])
        self.tick(100)
        e = self.entry()
        self.assertEqual(100, e["idle_s"])
        self.assertEqual({"id", "state", "alive", "gate_alive", "idle_s", "activity_src", "cpu_s"}, set(e))
        self.assertTrue(e["alive"])
        self.assertFalse(e["gate_alive"])

    def test_output_growth_resets_idle(self):
        self.events("j-1", "a\n")
        self.entry()
        self.tick(300)
        self.events("j-1", "more\n", "a")
        e = self.entry()
        self.assertEqual((0, "output"), (e["idle_s"], e["activity_src"]))
        self.tick(50)
        self.assertEqual(50, self.entry()["idle_s"])

    def test_cpu_growth_resets_idle(self):
        self.entry()
        self.tick(300)
        self.cpu[0] = 5.0
        e = self.entry()
        self.assertEqual((0, "cpu", 5.0), (e["idle_s"], e["activity_src"], e["cpu_s"]))
        self.tick(30)
        self.cpu[0] = 5.05  # background noise is not activity
        self.assertEqual(30, self.entry()["idle_s"])

    def test_file_change_resets_idle_and_scan_is_throttled(self):
        target = self.code / "a.txt"
        target.write_text("x")
        os.utime(target, (500, 500))
        self.entry()  # baseline scan
        self.tick(30)
        os.utime(target, (900, 900))
        self.assertEqual(30, self.entry()["idle_s"])  # under 60 s since the last scan: not looked at yet
        self.tick(40)
        e = self.entry()
        self.assertEqual((0, "files"), (e["idle_s"], e["activity_src"]))

    def test_over_the_cap_files_are_ignored(self):
        for i in range(5):
            (self.code / ("f%d" % i)).write_text("x")
        self.m.scan_cap = 3
        self.entry()
        self.tick(100)
        os.utime(self.code / "f0", (time.time() + 500, time.time() + 500))
        self.assertEqual(100, self.entry()["idle_s"])

    def test_gate_running_counts_as_activity_and_gate_alive(self):
        self.m.gate_threads.add("j-1")
        self.job.update({"state": "exited", "exit_code": 0, "pid": 999999})
        self.tick(500)
        e = self.entry()
        self.assertEqual(("exited", True, True, 0), (e["state"], e["alive"], e["gate_alive"], e["idle_s"]))

    def test_jobs_list_is_capped_and_skips_finished_jobs(self):
        for i in range(40):
            self.add_job(self.m, "r-%d" % i, pid=os.getpid())
        self.add_job(self.m, "done", state="exited", exit_code=0)
        report = self.m.jobs_report()
        self.assertEqual(32, len(report))
        self.assertNotIn("done", [e["id"] for e in report])
        self.assertEqual(1, len(self.ps_calls))  # one ps for all jobs, not one per job
        self.assertEqual(41, len(self.m.activity))  # jobs beyond the reported 32 keep their records
        self.tick(70)
        self.m.jobs_report()
        self.assertEqual(41, len(self.m.activity))
        self.assertEqual(2, len(self.ps_calls))

    def test_idle_of_a_job_beyond_the_cap_is_not_reset_when_it_enters_the_report(self):
        self.job["started_at"] = "2999-01-01T00:00:00+00:00"  # sorts last
        for i in range(32):
            self.add_job(self.m, "r-%d" % i, pid=os.getpid(), started_at="2000-01-01T00:00:%02d+00:00" % i)
        self.assertNotIn("j-1", [e["id"] for e in self.m.jobs_report()])
        self.tick(500)
        self.m.jobs["r-0"].update({"state": "exited"})  # frees a slot in the report
        entry = [e for e in self.m.jobs_report() if e["id"] == "j-1"][0]
        self.assertEqual(500, entry["idle_s"])

    def test_jobs_entry_has_exactly_the_contract_keys(self):
        self.assertEqual({"id", "state", "alive", "gate_alive", "idle_s", "activity_src", "cpu_s"},
                         set(self.entry()))

    def test_health_report_carries_jobs(self):
        with mock.patch.object(runner, "find_agents", return_value={}), \
                mock.patch.object(runner, "system_power", return_value={"on_battery": False, "battery_pct": None}):
            report = runner.health_report(self.m)
        self.assertEqual(["j-1"], [e["id"] for e in report["jobs"]])


if __name__ == "__main__":
    unittest.main()
