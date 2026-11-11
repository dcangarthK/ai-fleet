"""Runner side of account pools and limit handoff (docs/SPEC-limit-handoff.md 3.1, 4.3; AC 1, 7, 14, 15, 17).
Real git, fake agent CLIs, temp directories only (HOME is pointed at a temp dir, so no real profile is touched).
Run: python3 -m unittest -v tests/test_runner_handoff.py
"""
import json
import os
import subprocess
import time
import unittest
from pathlib import Path

from tests.test_runner_budget import Base, runner

SRC, NEW = "t-0000000a", "t-0000000b"


class ProfileBase(Base):
    def setUp(self):
        self.saved_home = os.environ.get("HOME")
        self.saved_claude_dir = os.environ.get("CLAUDE_CONFIG_DIR")
        super().setUp()
        self.home = self.base / "home"
        self.home.mkdir()
        os.environ["HOME"] = str(self.home)
        (self.home / ".claude-fleet").mkdir()
        (self.home / ".claude-fleet-2").mkdir()
        self.write_config({"claude": [{"id": "fleet", "dir": "~/.claude-fleet", "label": "owner-max"},
                                      {"id": "spare", "dir": "~/.claude-fleet-2", "label": "spare"},
                                      {"id": "gone", "dir": "~/.claude-fleet-3", "label": "gone"},
                                      {"id": "outside", "dir": str(self.base / "elsewhere"), "label": "x"}],
                           "codex": [{"id": "fleet", "dir": "~/.codex-fleet", "label": "owner-plus"}]})

    def tearDown(self):
        super().tearDown()
        for name, value in (("HOME", self.saved_home), ("CLAUDE_CONFIG_DIR", self.saved_claude_dir)):
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def write_config(self, profiles):
        config = {"max_mode": "auto-edit", "allowed_roots": [str(self.code)]}
        if profiles is not None:
            config["profiles"] = profiles
        (self.fleet / "runner.json").write_text(json.dumps(config))
        self.m = runner.JobManager(self.fleet, "http://127.0.0.1:1")
        self.m.cli_help = lambda agent: ""

    def wait(self, job_id):
        for _ in range(200):
            job = self.m.jobs[job_id]
            self.m.refresh(job)
            if job["state"] != "running":
                return job
            time.sleep(0.05)
        raise AssertionError("job still running")


class Profiles(ProfileBase):
    BODY = {"agent": "claude", "prompt": "x", "mode": "read-only"}

    def body(self, job_id, **kw):
        return {"id": job_id, "cwd": str(self.code)} | self.BODY | kw

    def test_a_known_profile_sets_the_config_dir_from_the_runners_own_config(self):
        job = self.m.create(self.body("p1", profile="spare"))
        self.assertEqual((job["profile"], job["account_label"]), ("spare", "spare"))
        env = self.m.agent_env("claude", {"CLAUDE_CONFIG_DIR": "/inherited"}, "spare")
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], str(self.home / ".claude-fleet-2"))
        # "fleet" always means the environment the runner inherits, whatever directory is listed for it
        self.assertEqual(self.m.agent_env("claude", {"CLAUDE_CONFIG_DIR": "/inherited"}, "fleet")["CLAUDE_CONFIG_DIR"],
                         "/inherited")
        fleet = self.m.create(self.body("p2", profile="fleet"))
        self.assertEqual((fleet["profile"], fleet["account_label"]), ("fleet", "owner-max"))

    def test_codex_profiles_set_codex_home_and_fleet_keeps_the_inherited_setup(self):
        self.write_config({"codex": [{"id": "fleet", "dir": "~/.codex-fleet", "label": "owner-plus"},
                                     {"id": "two", "dir": "~/.codex-two", "label": "two"}]})
        (self.home / ".codex-two").mkdir()
        self.assertEqual(self.m.agent_env("codex", {}, "two")["CODEX_HOME"], str(self.home / ".codex-two"))
        self.assertNotIn("CODEX_HOME", self.m.agent_env("codex", {}, "fleet"))  # owner's Codex setup unchanged

    def test_unknown_ids_and_paths_are_refused(self):
        for bad in ("nope", "~/.claude-fleet-2", str(self.home / ".claude-fleet-2"), "../spare", 7, "outside"):
            with self.assertRaises(runner.HttpError, msg=bad) as caught:
                self.m.create(self.body("bad-%d" % abs(hash(str(bad))), profile=bad))
            self.assertEqual(caught.exception.status, 400)
        for field in ("profile_dir", "config_dir", "claude_config_dir", "codex_home"):
            with self.assertRaises(runner.HttpError):
                self.m.create(self.body("f-" + field.replace("_", "-"), **{field: str(self.home)}))
        self.assertFalse(any(job.get("profile") == "nope" for job in self.m.jobs.values()))

    def test_health_reports_ids_labels_and_readiness_only(self):
        report = self.m.profile_report()
        self.assertEqual(report["claude"], [{"id": "fleet", "label": "owner-max", "ready": True},
                                            {"id": "spare", "label": "spare", "ready": True},
                                            {"id": "gone", "label": "gone", "ready": False}])
        self.assertEqual(report["codex"], [{"id": "fleet", "label": "owner-plus", "ready": True}])
        text = json.dumps(report)
        self.assertNotIn(str(self.home), text)
        self.assertNotIn(".claude-fleet", text)
        self.assertNotIn("dir", text)

    def test_resume_reuses_the_stored_profile(self):
        job = self.m.create(self.body("r1", mode="auto-edit", profile="spare"))
        self.wait("r1")
        seen = []
        original = self.m.agent_env
        self.m.agent_env = lambda agent, base, profile=None: seen.append(profile) or original(agent, base, profile)
        self.m.message(self.m.jobs["r1"], {"text": "go on"})
        self.assertEqual(seen, ["spare"])
        self.assertEqual(self.m.public(self.m.jobs["r1"])["profile"], "spare")
        self.wait("r1")
        with self.assertRaises(runner.HttpError) as caught:
            self.m.message(self.m.jobs["r1"], {"text": "go on", "profile": "fleet"})
        self.assertEqual(caught.exception.status, 400)
        self.assertEqual(job["id"], "r1")

    def test_a_runner_without_a_profiles_block_keeps_todays_behaviour(self):
        self.write_config(None)
        self.assertEqual(self.m.profile_report()["claude"], [{"id": "fleet", "label": None, "ready": True}])
        job = self.m.create(self.body("old1", agent="codex"))
        self.assertEqual(job["profile"], "shared")  # as before: nothing signed in on the fleet Codex profile
        with self.assertRaises(runner.HttpError):
            self.m.create(self.body("old2", profile="spare"))
        self.assertEqual(self.m.create(self.body("old3", profile="fleet"))["profile"], "fleet")


class GitBase(ProfileBase):
    def git(self, *args, cwd=None, check=True):
        return subprocess.run(["git", *args], cwd=cwd or self.repo, check=check, capture_output=True, text=True).stdout

    def setUp(self):
        super().setUp()
        self.repo = self.code / "repo"
        self.repo.mkdir()
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "t@example.test")
        self.git("config", "user.name", "T")
        (self.repo / "a.txt").write_text("one\n")
        self.git("add", "a.txt")
        self.git("commit", "-qm", "init")
        self.m.create({"id": SRC, "agent": "claude", "prompt": "x", "mode": "auto-edit", "repo": str(self.repo),
                       "worktree": True, "profile": "fleet"})
        self.src = self.wait(SRC)
        self.cwd = Path(self.src["cwd"])

    def checkpoint(self, **body):
        return self.m.checkpoint(self.m.jobs[SRC], {"label": "claude/claude-opus-5-5"} | body)

    def refused(self, fn, status=409):
        with self.assertRaises(runner.HttpError) as caught:
            fn()
        self.assertEqual(caught.exception.status, status, caught.exception.detail)
        return caught.exception.detail


class Checkpoint(GitBase):
    def test_commits_the_work_leaving_out_secret_files_and_reports_them(self):
        (self.cwd / "a.txt").write_text("two\n")
        (self.cwd / "new.txt").write_text("fresh\n")
        (self.cwd / ".env").write_text("TOKEN=x\n")
        (self.cwd / "server.pem").write_text("pem\n")
        (self.cwd / "sub").mkdir()
        (self.cwd / "sub" / "id_ed25519").write_text("key\n")
        (self.cwd / "my_credentials.json").write_text("{}\n")
        out = self.checkpoint()
        self.assertTrue(out["committed"])
        self.assertRegex(out["sha"], r"^[0-9a-f]{40}$")
        self.assertEqual(sorted(out["excluded"]), [".env", "my_credentials.json", "server.pem", "sub/id_ed25519"])
        files = self.git("show", "--name-only", "--format=", out["sha"], cwd=self.cwd).split()
        self.assertEqual(sorted(files), ["a.txt", "new.txt"])
        self.assertEqual(self.git("log", "-1", "--format=%s", cwd=self.cwd).strip(),
                         "checkpoint: stopped at usage limit (claude/claude-opus-5-5)")
        self.assertEqual(self.git("log", "-1", "--format=%an <%ae>", cwd=self.cwd).strip(), "AI Fleet <fleet@localhost>")
        self.assertTrue((self.cwd / ".env").exists())  # left in place, just not committed
        self.assertIn("a.txt", out["stat"])
        self.assertEqual(out["branch"], "fleet/" + SRC)

    def test_runs_no_hooks_and_needs_no_signing(self):
        hooks = self.base / "hooks"
        hooks.mkdir()
        marker = self.base / "hook-ran"
        (hooks / "pre-commit").write_text("#!/bin/sh\ntouch %s\nexit 1\n" % marker)
        (hooks / "pre-commit").chmod(0o755)
        self.git("config", "core.hooksPath", str(hooks))
        self.git("config", "commit.gpgsign", "true")
        self.git("config", "gpg.program", "/nonexistent/gpg")
        (self.cwd / "a.txt").write_text("two\n")
        self.assertTrue(self.checkpoint()["committed"])
        self.assertFalse(marker.exists())

    def test_nothing_to_commit_returns_the_branch_tip(self):
        tip = self.git("rev-parse", "HEAD", cwd=self.cwd).strip()
        out = self.checkpoint()
        self.assertEqual((out["committed"], out["sha"]), (False, tip))

    def test_refuses_a_running_job(self):
        job = self.m.jobs[SRC]
        job.update(state="running", pid=os.getpid(), exit_file="exit_code.never")
        try:
            self.refused(self.checkpoint)
        finally:
            job.update(state="exited", pid=None)

    def test_refuses_the_wrong_branch_a_merge_or_an_index_lock(self):
        tip = self.git("rev-parse", "HEAD", cwd=self.cwd).strip()
        self.git("checkout", "-q", "--detach", cwd=self.cwd)
        self.refused(self.checkpoint)
        self.git("checkout", "-q", "-b", "other", cwd=self.cwd)
        self.refused(self.checkpoint)
        self.git("checkout", "-q", "fleet/" + SRC, cwd=self.cwd)
        git_dir = Path(self.git("rev-parse", "--absolute-git-dir", cwd=self.cwd).strip())
        for name in ("MERGE_HEAD", "index.lock"):
            (git_dir / name).write_text(tip + "\n")
            (self.cwd / "a.txt").write_text("two\n")
            self.refused(self.checkpoint)
            (git_dir / name).unlink()
        (git_dir / "rebase-merge").mkdir()
        self.refused(self.checkpoint)

    def test_refuses_when_the_worktree_is_missing(self):
        self.m.cleanup(self.m.jobs[SRC])
        self.refused(self.checkpoint)
        plain = self.m.create({"id": "plain1", "agent": "claude", "prompt": "x", "mode": "read-only",
                               "cwd": str(self.code)})
        self.wait(plain["id"])
        self.refused(lambda: self.m.checkpoint(self.m.jobs["plain1"], {}))


class BaseBranch(GitBase):
    def handoff(self, job_id=NEW, **kw):
        body = {"id": job_id, "agent": "claude", "prompt": "continue", "mode": "auto-edit", "repo": str(self.repo),
                "worktree": True, "profile": "spare", "base_branch": "fleet/" + SRC, "source_job": SRC} | kw
        return self.m.create(body)

    def test_the_new_worktree_starts_from_the_checkpoint_and_diffs_from_the_source_base(self):
        (self.cwd / "a.txt").write_text("two\n")
        (self.cwd / "new.txt").write_text("fresh\n")
        self.checkpoint()
        job = self.handoff()
        self.assertEqual(job["branch"], "fleet/" + NEW)
        self.assertEqual((Path(job["cwd"]) / "new.txt").read_text(), "fresh\n")
        self.assertEqual((Path(job["cwd"]) / "a.txt").read_text(), "two\n")
        self.assertEqual(self.m.jobs[NEW]["base"], self.m.jobs[SRC]["base"])
        self.wait(NEW)
        (Path(job["cwd"]) / "b.txt").write_text("more\n")
        diff = self.m.diff(self.m.jobs[NEW])["diff"]
        for text in ("+two", "+fresh", "+more"):  # the checkpointed work plus the new work
            self.assertIn(text, diff)
        self.assertNotEqual(self.cwd, Path(job["cwd"]))  # its own worktree

    def test_bad_base_branches_and_sources_are_refused(self):
        self.checkpoint()
        self.refused(lambda: self.handoff("t-00000001", base_branch="main"), 400)
        self.refused(lambda: self.handoff("t-00000002", base_branch="fleet/t-0000000a; rm -rf /"), 400)
        self.refused(lambda: self.handoff("t-00000003", base_branch="fleet/t-deadbeef", source_job="t-deadbeef"), 400)
        self.refused(lambda: self.handoff("t-00000004", source_job=None), 400)
        self.refused(lambda: self.handoff("t-00000005", worktree=False), 400)
        self.git("branch", "fleet/t-0000000c")  # exists in git, but no job record
        self.refused(lambda: self.handoff("t-00000006", base_branch="fleet/t-0000000c", source_job="t-0000000c"), 400)
        self.refused(lambda: self.handoff("t-00000007", base_branch="fleet/t-0000000c"), 400)  # not the source's branch
        other = self.code / "other"
        other.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=other, check=True)
        self.refused(lambda: self.handoff("t-00000008", repo=str(other)), 400)  # a different repo
        policy = {"filesystem": {"allowed_roots": [str(self.base / "nowhere")]}}
        self.refused(lambda: self.handoff("t-00000009", policy=policy), 403)  # the new task's policy is re-checked
        self.assertFalse([j for j in self.m.jobs if j.startswith("t-0000000") and j not in (SRC,)])

    def test_cleanup_of_the_source_keeps_the_checkpoint_branch(self):
        (self.cwd / "a.txt").write_text("two\n")
        self.checkpoint()
        self.handoff()
        self.wait(NEW)
        self.m.cleanup(self.m.jobs[SRC])
        self.assertFalse(self.cwd.exists())
        self.assertTrue(self.git("branch", "--list", "fleet/" + SRC).strip())


class Routes(GitBase):
    def test_checkpoint_route_and_health_flags(self):
        self.assertIn("checkpoint", runner.Handler._route.__code__.co_consts)


if __name__ == "__main__":
    unittest.main()
