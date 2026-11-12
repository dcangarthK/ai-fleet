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

# Placeholder in settings-*.json hook commands; the runner substitutes the absolute path of quiet_bash.py.
QUIET_BASH_PLACEHOLDER = "{{QUIET_BASH}}"


def render_settings(text, quiet_bash_path):
    """Settings file text with the hook script placeholder replaced by an absolute path."""
    return text.replace(QUIET_BASH_PLACEHOLDER, quiet_bash_path)
