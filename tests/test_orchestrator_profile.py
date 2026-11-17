"""Lean orchestrator launch profile (orchestrator/*, scripts/orchestrator.sh).
Run: python3 -m unittest -v tests/test_orchestrator_profile.py
"""
import json
import os
import re
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DISABLED = (
    ["frontend-design@claude-plugins-official", "ui-ux-pro-max@ui-ux-pro-max-skill",
     "accesslint@accesslint-marketplace", "superpowers@claude-plugins-official"]
    + [n + "@designer-skills" for n in (
        "design-research design-systems ux-strategy ui-design interaction-design "
        "prototyping-testing design-ops designer-toolkit").split()]
    + [n + "@bencium-marketplace" for n in (
        "bencium-controlled-ux-designer bencium-innovative-ux-designer bencium-impact-designer "
        "design-audit typography relationship-design adaptive-communication negentropy-lens "
        "bencium-aeo bencium-code-conventions renaissance-architecture human-architect-mindset").split()]
)
SECRET = re.compile(r"sk-[A-Za-z0-9]{10,}|(api[_-]?key|token|secret|password)\"?\s*[:=]", re.I)


def read(rel):
    with open(os.path.join(ROOT, rel)) as f:
        return f.read()


class OrchestratorProfileTests(unittest.TestCase):
    def test_json_parses(self):
        for rel in ("orchestrator/mcp.json", "orchestrator/settings.json"):
            json.loads(read(rel))

    def test_mcp_only_local_llm(self):
        servers = json.loads(read("orchestrator/mcp.json"))["mcpServers"]
        self.assertEqual(list(servers), ["local-llm"])
        self.assertIn("master/mcp/server.py", servers["local-llm"]["args"])

    def test_no_abs_paths_or_secrets(self):
        for rel in ("orchestrator/mcp.json", "orchestrator/settings.json", "scripts/orchestrator.sh"):
            text = read(rel)
            self.assertNotIn("/Users/", text, rel)
            self.assertIsNone(SECRET.search(text), rel)

    def test_plugins(self):
        ep = json.loads(read("orchestrator/settings.json"))["enabledPlugins"]
        for name in DISABLED:
            self.assertIs(ep.get(name), False, name)
        self.assertEqual([k for k in ep if k.startswith("claude-mem") and ep[k] is False], [])

    def test_launcher(self):
        text = read("scripts/orchestrator.sh")
        for s in ("--strict-mcp-config", "orchestrator/mcp.json", "orchestrator/settings.json", "exec claude"):
            self.assertIn(s, text)
        self.assertTrue(os.access(os.path.join(ROOT, "scripts/orchestrator.sh"), os.X_OK))

    @unittest.skipIf(os.name == "nt", "sh launcher")
    def test_launcher_behaviour(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "rec.txt")
            fake = os.path.join(d, "claude")
            with open(fake, "w") as f:
                f.write('#!/bin/sh\npwd > "$REC"\nfor a in "$@"; do printf \'%s\\n\' "$a" >> "$REC"; done\n')
            os.chmod(fake, 0o755)
            env = dict(os.environ, PATH=d + os.pathsep + os.environ["PATH"], REC=out)
            subprocess.run(["sh", os.path.join(ROOT, "scripts/orchestrator.sh"), "-p", "a b", "x"],
                           cwd=d, env=env, check=True)
            with open(out) as f:
                lines = f.read().split("\n")[:-1]
            self.assertEqual(os.path.realpath(lines[0]), os.path.realpath(ROOT))
            self.assertEqual(lines[1:], ["--strict-mcp-config", "--mcp-config", "orchestrator/mcp.json",
                                         "--settings", "orchestrator/settings.json",
                                         "--setting-sources", "project,local", "-p", "a b", "x"])

    def test_restated_user_settings(self):
        s = json.loads(read("orchestrator/settings.json"))
        self.assertEqual(s["autoCompactWindow"], 200000)
        self.assertEqual(s["env"]["CLAUDE_CODE_AUTO_COMPACT_WINDOW"], "200000")
        self.assertEqual(s["permissions"]["deny"], ["Bash(git push *)"])
        self.assertEqual(s["permissions"]["defaultMode"], "auto")
        self.assertNotIn("allow", s["permissions"])
        for event, script in (("SessionStart", "caveman-activate.js"),
                              ("UserPromptSubmit", "caveman-mode-tracker.js")):
            groups = s["hooks"][event]
            self.assertEqual(len(groups), 1)
            self.assertNotIn("matcher", groups[0])
            h = groups[0]["hooks"][0]
            self.assertEqual(h["type"], "command")
            self.assertEqual(h["command"], 'node "$HOME/.claude/hooks/%s"' % script)
        self.assertNotIn("/Users/", read("orchestrator/settings.json"))

    def test_user_source_excluded_claude_mem_on(self):
        self.assertIn("--setting-sources project,local", read("scripts/orchestrator.sh"))
        ep = json.loads(read("orchestrator/settings.json"))["enabledPlugins"]
        self.assertIs(ep.get("claude-mem@thedotmack"), True)


if __name__ == "__main__":
    unittest.main()
