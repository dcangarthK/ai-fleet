# Master instructions (all Claude accounts, all models)

Installed as the user-level CLAUDE.md of every Claude profile: `~/.claude` (owner/orchestrator account),
`~/.claude-fleet` (fleet account) and `~/.claude-fleet-spare` (spare account). The source of truth is this file in
the ai-fleet repo (`docs/MASTER-INSTRUCTIONS.md`); re-copy it to all three after any change.

## Token saving
- **Caveman mode is always on.** Every session starts in caveman `full` (SessionStart hook). Write terse: no filler,
  no pleasantries, fragments are fine, technical terms exact. Write code, commit messages, PR bodies, specs and
  anything the owner will read as a document in normal prose.
- **claude-mem** is the shared memory layer. When it is installed, use it to recall earlier work before re-reading
  files or re-deriving decisions, and let it record what you learn. If it is not installed, say so once in your
  report; don't install it yourself. In a fleet task, if a claude-mem (or any) tool call is refused once, don't
  call it again: carry on without it and note it in your report. Retrying a refused tool only burns tokens.
- Send less: point at files instead of pasting them; never paste generated files, lockfiles or large logs.
- **Auto-compact at 200,000 tokens.** Every profile's settings.json sets `autoCompactWindow: 200000` and
  `env.CLAUDE_CODE_AUTO_COMPACT_WINDOW=200000` (the documented minimum is 100,000).
- **Orchestrator hygiene.** State lives in fleet.db and the stored plan, not in chat. Use a fresh session per batch,
  started from the handoff summary. `wait_tasks` is event-based (no polling loop). `wait_tasks` and `task_status`
  return state, verdict, findings and gate summary only; pass `full=True` for the whole output.
- **One chat per batch, bounded by the plan.** Open a session with `get_plan()`, close it with `put_plan(...)`, and
  start the next batch fresh from what you stored. Do not carry one conversation across several batches: every turn
  re-reads the whole conversation, so a long orchestrator chat is the most expensive thing in the fleet. On
  2026-11-16 a single orchestrator session accounted for 107 M of the day's 186 M cache-read tokens, 57% of the
  total (`docs/TOKEN-AUDIT-2026-11-16.md` 1.2). The pipeline itself is written out in the `fleet-batch` skill
  (`.claude/skills/fleet-batch/SKILL.md` in ai-fleet); follow it rather than re-deriving it in prose.

## Fleet rules that apply everywhere
- Work in ai-fleet follows the repo's CLAUDE.md (pipeline, paid-model budget, Fable never builds/fixes/tests).
- Only do agreed work. Don't read, change or delete files unrelated to the task.
- Never send credentials, keys, `.env` contents or customer data to any model or service.
- Never approve or unblock blocked tasks; that is the owner's job.
