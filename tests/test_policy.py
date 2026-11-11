"""Phase 6 merge rules (docs/SPEC-phase6-policy.md), pure functions in master/controller/policy.py.
Run: python3 -m unittest -v tests/test_policy.py
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "master" / "controller"))
import policy as P  # noqa: E402


def L(name, text=None, body=None):
    return {"layer": name, "instructions": text, "policy": body or {}}


class Validate(unittest.TestCase):
    def test_accepts_full_body(self):
        P.validate({"filesystem": {"allowed_roots": ["~/code", "/srv/x"], "denied": ["~/code/secret"]},
                    "tools": {"claude_allow": ["Read", "Bash(npm test:*)"], "claude_deny": ["WebFetch"]},
                    "network": {"agents": "deny"}, "mode_ceiling": "auto-edit",
                    "limits": {"task_minutes": 60, "max_concurrent": 2, "agent_minutes_per_day": 300,
                               "quiet_hours": ["23:00-07:00"]}})

    def test_rejects_bad_bodies(self):
        bad = [{"surprise": 1}, {"filesystem": {"allowed_roots": ["relative/path"]}},
               {"filesystem": {"allowed_roots": "~/code"}}, {"tools": {"claude_deny": ["rm -rf /"]}},
               {"network": {"agents": "maybe"}}, {"mode_ceiling": "god"}, {"limits": {"task_minutes": -1}},
               {"limits": {"quiet_hours": ["25:00-07:00"]}}, {"limits": {"max_concurrent": "two"}},
               {"filesystem": {"extra": []}}]
        for b in bad:
            with self.assertRaises(ValueError, msg=b):
                P.validate(b)


class Merge(unittest.TestCase):
    def test_instructions_concatenate_in_layer_order(self):
        r = P.merge([L("fleet", "Be careful."), L("node:laptop-2", None), L("role:coder", "Write tests first."),
                     L("agent:codex", "Use the repo's style.")])
        self.assertEqual(r["instructions"],
                         "## fleet\nBe careful.\n\n## role:coder\nWrite tests first.\n\n## agent:codex\nUse the repo's style.")

    def test_roots_intersect_and_keep_the_narrower(self):
        r = P.merge([L("fleet", body={"filesystem": {"allowed_roots": ["/home/a/code"]}}),
                     L("node:n", body={"filesystem": {"allowed_roots": ["/home/a/code/ai-fleet", "/srv"]}})])
        self.assertEqual(r["policy"]["filesystem"]["allowed_roots"], ["/home/a/code/ai-fleet"])
        r = P.merge([L("fleet", body={"filesystem": {"allowed_roots": ["/a"]}}),
                     L("node:n", body={"filesystem": {"allowed_roots": ["/b"]}})])
        self.assertEqual(r["policy"]["filesystem"]["allowed_roots"], [])  # disjoint: nothing allowed
        r = P.merge([L("fleet"), L("node:n", body={"filesystem": {"allowed_roots": ["/b"]}})])
        self.assertEqual(r["policy"]["filesystem"]["allowed_roots"], ["/b"])  # unset layers don't count

    def test_denies_union_and_deny_wins(self):
        r = P.merge([L("fleet", body={"filesystem": {"denied": ["/x"]}, "tools": {"claude_deny": ["WebFetch"]},
                                      "network": {"agents": "allow"}}),
                     L("role:coder", body={"filesystem": {"denied": ["/y"]}, "tools": {"claude_deny": ["Bash"]},
                                           "network": {"agents": "deny"}})])
        self.assertEqual(sorted(r["policy"]["filesystem"]["denied"]), ["/x", "/y"])
        self.assertEqual(sorted(r["policy"]["tools"]["claude_deny"]), ["Bash", "WebFetch"])
        self.assertEqual(r["policy"]["network"]["agents"], "deny")
        self.assertEqual(r["sources"]["network.agents"], "role:coder")

    def test_allow_lists_intersect(self):
        r = P.merge([L("fleet", body={"tools": {"claude_allow": ["Read", "Edit", "Bash"]}}),
                     L("agent:claude", body={"tools": {"claude_allow": ["Read", "Edit"]}})])
        self.assertEqual(sorted(r["policy"]["tools"]["claude_allow"]), ["Edit", "Read"])

    def test_limits_take_minimum_and_ceiling_too(self):
        r = P.merge([L("fleet", body={"mode_ceiling": "full", "limits": {"task_minutes": 120, "max_concurrent": 4}}),
                     L("node:n", body={"mode_ceiling": "auto-edit", "limits": {"task_minutes": 30}}),
                     L("task", body={"mode_ceiling": "full", "limits": {"task_minutes": 90}})])
        self.assertEqual(r["policy"]["mode_ceiling"], "auto-edit")
        self.assertEqual(r["policy"]["limits"]["task_minutes"], 30)
        self.assertEqual(r["policy"]["limits"]["max_concurrent"], 4)
        self.assertEqual(r["sources"]["limits.task_minutes"], "node:n")
        self.assertEqual(r["sources"]["mode_ceiling"], "node:n")

    def test_task_layer_cannot_loosen(self):
        r = P.merge([L("fleet", body={"network": {"agents": "deny"}, "filesystem": {"allowed_roots": ["/code"]}}),
                     L("task", body={"network": {"agents": "allow"}, "filesystem": {"allowed_roots": ["/"]}})])
        self.assertEqual(r["policy"]["network"]["agents"], "deny")
        self.assertEqual(r["policy"]["filesystem"]["allowed_roots"], ["/code"])

    def test_quiet_hours_union_and_check(self):
        r = P.merge([L("fleet", body={"limits": {"quiet_hours": ["23:00-07:00"]}}),
                     L("node:n", body={"limits": {"quiet_hours": ["12:00-13:00"]}})])
        q = r["policy"]["limits"]["quiet_hours"]
        self.assertEqual(sorted(q), ["12:00-13:00", "23:00-07:00"])
        self.assertTrue(P.in_quiet_hours(q, 23 * 60 + 30))
        self.assertTrue(P.in_quiet_hours(q, 6 * 60 + 59))
        self.assertFalse(P.in_quiet_hours(q, 7 * 60))
        self.assertTrue(P.in_quiet_hours(q, 12 * 60 + 15))
        self.assertFalse(P.in_quiet_hours([], 0))

    def test_hash_is_stable_and_content_based(self):
        a = P.merge([L("fleet", "x", {"limits": {"task_minutes": 5}})])
        b = P.merge([L("fleet", "x", {"limits": {"task_minutes": 5}})])
        c = P.merge([L("fleet", "y", {"limits": {"task_minutes": 5}})])
        self.assertEqual(a["hash"], b["hash"])
        self.assertNotEqual(a["hash"], c["hash"])
        self.assertEqual(len(a["hash"]), 64)

    def test_auto_approve_only_narrows(self):
        r = P.merge([L("fleet", body={"auto_approve": {"agents": ["claude", "codex"], "full_mode": True, "denied_tools": True}}),
                     L("node:n", body={"auto_approve": {"agents": ["codex"], "full_mode": False}})])
        self.assertEqual(r["policy"]["auto_approve"], {"agents": ["codex"], "full_mode": False, "denied_tools": True})
        with self.assertRaises(ValueError):
            P.validate({"auto_approve": {"agents": ["ollama"]}})
        with self.assertRaises(ValueError):
            P.validate({"auto_approve": {"full_mode": "yes"}})

    def test_empty(self):
        r = P.merge([])
        self.assertEqual(r["instructions"], "")
        self.assertEqual(r["policy"], {})


if __name__ == "__main__":
    unittest.main()
