# Lean worker profile (Part H req 1, 5, 9)

Files here are inert until the runner wires them in. Per kind, add these arguments to `claude -p`:

    --settings nodes/worker/settings-<kind>.json \
    --strict-mcp-config --mcp-config nodes/worker/mcp-empty.json \
    --allowedTools <tools> --model <model> --effort <effort>

Set `FLEET_JOB_DIR=<job dir>` in the worker env so `quiet_bash.py` saves full test output there.
The hook command in the settings files is `python3 nodes/worker/quiet_bash.py`; the runner must run it with the
repo root as cwd, or substitute the absolute path.

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
