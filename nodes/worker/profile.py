"""Worker launch profile constants (Part H req 1). The runner builds `claude -p` arguments from these.

Flags come from `claude --help` (2.1.x): --setting-sources takes a comma list of user, project, local; leaving
`user` out stops ~/.claude/settings.json (plugins, hooks, skills) merging into the worker. --strict-mcp-config
ignores every MCP config but --mcp-config. --disable-slash-commands disables all skills and commands.
UNVERIFIED by a live run: that omitting `user` also stops user-installed plugins loading.
"""

# Passed on every worker launch, before the per-kind --settings file.
LAUNCH_ARGS = (
    "--setting-sources", "project",
    "--strict-mcp-config",
    "--disable-slash-commands",
)

# The tool set per worker profile (spec Part H 3.1). The runner passes it as `--tools` (what the model sees, which is
# what fills turn 1) and as `--allowedTools` (what needs no approval), so a Haiku test task can run Bash.
TOOLS = {
    "build": ["Read", "Edit", "Write", "Bash", "Grep", "Glob"],
    "fix": ["Read", "Edit", "Write", "Bash", "Grep", "Glob"],
    "review": ["Read", "Grep", "Glob"],
    "test": ["Bash"],
}

# What a build/fix/test job's plain Bash is pre-approved as (auto-edit and full only; supervised pre-approves
# nothing): the interpreters and runners the gate uses, not every shell command.
BASH_RULES = [
    "Bash(python3:*)", "Bash(git:*)", "Bash(uv run:*)", "Bash(scripts/gate.sh:*)", "Bash(./scripts/gate.sh:*)",
    "Bash(npx vitest:*)",
]

# Placeholder in settings-*.json hook commands; the runner substitutes the absolute path of quiet_bash.py.
QUIET_BASH_PLACEHOLDER = "{{QUIET_BASH}}"


def render_settings(text, quiet_bash_path):
    """Settings file text with the hook script placeholder replaced by an absolute path."""
    return text.replace(QUIET_BASH_PLACEHOLDER, quiet_bash_path)
