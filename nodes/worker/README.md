# Lean worker profile (Part H req 1, 5, 9)

Files here are inert until the runner wires them in. Per kind, launch `claude -p` with:

    <LAUNCH_ARGS from profile.py> \
    --settings <rendered settings-<kind>.json> \
    --mcp-config nodes/worker/mcp-empty.json \
    --allowedTools <tools> --model <model> --effort <effort>

`LAUNCH_ARGS` = `--setting-sources project --strict-mcp-config --disable-slash-commands`. Without
`--setting-sources` omitting `user`, an empty `enabledPlugins` merges with ~/.claude/settings.json and the user's
plugins, skills and hooks still load. (Flags checked in `claude --help` only; that omitting `user` also stops
user-installed plugins is UNVERIFIED by a live run.)

Hook path: the settings files hold `python3 {{QUIET_BASH}}`. The runner must write a rendered copy
(`profile.render_settings(text, <absolute path of quiet_bash.py>)`) into the job dir and pass that to `--settings`;
never pass the raw file (the placeholder is not a path, and a relative path breaks when cwd is a worktree).

Set `FLEET_JOB_DIR=<job dir>` in the worker env so `quiet_bash.py` saves full test output there (else it uses a
fresh private temp dir).

| kind | settings file | --allowedTools | model | effort |
|---|---|---|---|---|
| build | settings-build.json | Read Edit Write Bash Grep Glob | sonnet | medium |
| fix | settings-fix.json | Read Edit Write Bash Grep Glob | sonnet | medium |
| review | settings-review.json | Read Grep Glob | sonnet (Fable/Codex per Budget page) | medium |
| test | settings-test.json | Bash | haiku | low |
| re_review | settings-review.json | Read Grep Glob | same as review | low |

Models and efforts follow the CLAUDE.md budget table; the Budget page is the source of truth.

Idle features (req 9) set in the files: `promptSuggestionEnabled`, `autoDreamEnabled`, `awaySummaryEnabled`,
`spinnerTipsEnabled`, `autoMemoryEnabled` all false, and `env.CLAUDE_CODE_GOAL_CHECKIN_MINUTES=0`. These names
were found in the installed Claude Code 2.1.322 binary. The cross-session inbound `hold` setting could not be
verified there, so it is not set.

## quiet_bash (req 5)
Test runs (`python3 -m unittest`, `scripts/gate.sh`, `vitest`) are summarised only when they are the start of the
command or of a segment after `&&`, `||`, `;` or `|`; `grep vitest` or a commit message mentioning them pass
through. Bare `git log` gets `-n 30`; `git diff` without a path output is piped through `head -n 300` with a
`truncated, full: <path>` line. Each output line is capped at 500 chars. Hook replies carry `updatedInput` only,
no `permissionDecision`, so normal permission rules still decide.
