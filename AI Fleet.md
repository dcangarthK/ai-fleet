Writing the handover now. First, a snapshot of every running piece so the doc is accurate.

Your Claude account hit its 5-hour limit (resets at 2:30 PM), stopping the Fable G4 review, the G3 benchmark, the fixes batch and the G6 build. I'm checking what partial work each one left so the handover is precise.

The handover is ready: [docs/HANDOVER.md](docs/HANDOVER.md), committed on `feat/agent-tasks` as `a545d8c`. A memory note points to it, so a new session in this project finds it straight away.

**Note:** your Claude account just hit its 5-hour limit (resets **2:30 PM**). The fleet's Claude tasks use the same account, so the Fable review of G4, the G3 benchmark and the fixes batch all stopped with "You've hit your limit", and G6's build failed the same way. Nothing was lost. Codex is back at **1 PM**.

**What the doc covers:**
1. **What the system is,** and the live services on this Mac: controller, runner, Ollama, LiteLLM, the fleet Claude profile, and their ports, configs and restart commands.
2. **Your standing instructions:** the orchestrator only delegates; Claude and Codex review, with Fable 5.1 standing in for Codex; never approve blocked tasks; no merge to `main` without your OK.
3. **Where every doc is:** CLAUDE.md, README, all the specs, the five grounding plans, the eval results and memory.
4. **The delegation toolkit:** ready-to-use API calls for pipelines, tasks, resuming a builder, and retrying reviews, plus the patterns that work (Fable reviews, Claude-run merges, local-model drafts).
5. **Exactly where each piece of work stands:**
   - **G4:** built and gate passed; still needs a review, then a merge. Its diff exceeds the runner's 200 KB limit, so the doc says how to review it.
   - **Fleet fixes batch:** four fixes, to resend. One of them must land before benchmark results are committed.
   - **G3 benchmark:** two of three models done. So far qwen2.5-coder:14b scored 96.7 with 0% hallucinations; phi4:14b 86.7 with 6.7%.
   - **G6:** to restart.
   - **G2 benchmark:** not started.
6. **Known issues:**
   - shared account limits;
   - Codex can't commit;
   - the diff size cap;
   - timing-sensitive tests;
   - glm-4.7-flash is slow on big diffs;
   - Laptop 2 hasn't joined yet;
   - your open decisions.
7. **A "look before acting" block** of commands to check the live state first.

**To hand over:** in the other Claude account, open this project (`~/ai-fleet`) and ask it to read `docs/HANDOVER.md` and continue from section 6. Fleet Claude tasks will keep failing until 2:30 PM, because they run on your account rather than the new one. So until then the new session can only orchestrate, plus use Codex from 1 PM and the local models.

-----
I've added both to [docs/HANDOVER.md](docs/HANDOVER.md), committed as `0013f8f`.

**New §4a: where the fleet runs and how to start it**
- **Where:** everything runs on this MacBook (the master) under your user account; nothing runs in the cloud. A table maps each path:
  - the repo `~/ai-fleet`, which the live controller and runner run from;
  - `fleet.db`;
  - the LiteLLM folder, with secrets in `.env`, which must never be printed;
  - `~/.ai-fleet`: the runner's config, token and task worktrees;
  - `~/.claude-fleet`: the fleet's Claude login;
  - the launchd files and the log files.
- **Start-up, in order, each step with a health check:**
  1. Docker and the LiteLLM containers
  2. Ollama
  3. the controller and runner, via `bash master/setup.sh --services-only`
- **Also:** how to restart or stop each piece, the console URL and its pages, and the two gotchas we hit (the `~/.cache`/`~/.docker` permissions, and the battery rule).

**New §4b: copy-paste starters for the remaining work**

Ready-made commands, in order:
- **A.** Resend the fleet-fixes batch to its Claude builder.
- **B.** Review G4 with Codex after 1 PM (or Fable), including how to handle its oversized diff.
- **C.** Restart G6 as a new pipeline from its saved brief.
- **D.** Finish the G3 benchmark (glm-4.7-flash) and commit the results, once fix A is merged.
- **E.** Run the G2 reviewer benchmark, which decides whether any local model becomes a trusted reviewer.

It also covers how to watch work without polling by hand, and how merges are delegated to Claude tasks.

The new session should read §4a to check the fleet is up, §6 for where things stand, then work through §4b. Claude-account tasks resume after 2:30 PM; Codex is available from 1 PM.
