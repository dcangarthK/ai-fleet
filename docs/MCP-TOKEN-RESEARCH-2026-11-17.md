# MCP, CLIs and file-based comms: token research (17 Nov 2026)

Question: did MCP use in our Claude sessions cost more tokens, and what alternatives would cut fleet token usage?
Options considered: CLIs, `.md` files for agent-to-agent comms, and others.

## 1. Measured answer

### Method

- I took the two orchestrator chats from the deep dive: controller-intel (73007b3e) and ui-revamp (a70555bf).
- I counted three things:
  - **Startup context:** what the context already holds on turn 1, before any work starts.
  - **Tool results:** for each tool result, its tokens (bytes ÷ 4) times the turns it stayed in context, up to the next compaction.
  - **Worker sessions:** turn-1 context from the last 40 fleet worker sessions in `~/.claude-fleet`.

### Results

| Source of cache reads | controller-intel | ui-revamp |
|---|---:|---:|
| Total cache reads | 21.1M | 20.7M |
| **Startup context × turns** (89.8k / 90.0k tokens before the first word) | **16.0M (76%)** | **14.9M (72%)** |
| `local-llm` MCP results kept in context | 0.75M (4%) | 1.23M (6%) |
| Browser MCP results (screenshots) | — | 0.86M (4%) |
| Built-in tool results (Bash, Read, …) | 0.84M (4%) | 0.48M (2%) |
| Everything else (conversation, thinking, edits) | ~3.5M | ~3.2M |

| Worker sessions | Turn-1 context |
|---|---:|
| Median | 15k tokens |
| Max | 127k tokens |

### What this shows

1. **MCP results did cost tokens, but only 4–10% of a chat's cache reads.** `local-llm` produced 80–89% of all tool-result bytes, yet it barely shows in the totals. Compaction cuts how long each result stays in context, and the trims added in this batch cover the rest.
2. **MCP tool definitions are not the problem either.** Claude Code defers MCP tool schemas by default. Only tool names and server instructions load at startup, and a schema is fetched through ToolSearch when needed.
3. **The real cost is the 90k-token startup context, paid again on every turn.** Every orchestrator turn re-reads about 90k tokens of system prompt, instructions, skill listings, deferred-tool names and MCP server instructions, before any fleet state at all. That is about three quarters of what the chats cost. The fleet workers avoid it: they start with `--mcp-config mcp-empty.json` and lean settings at about 15k.

The 90k has not been broken down yet. To do that, run `/context` in a fresh orchestrator session. Likely contributors, from what this session loads:

- About 350 skill descriptions (design, SEO, marketing, language packs and other plugin skills the fleet never uses).
- About 250 deferred tool names across 12+ MCP servers (Chrome, Docs, scheduled tasks, claude-mem and others).
- Long MCP server instructions: claude-mem's alone is several hundred tokens.
- The claude-mem session-start digest.
- Two CLAUDE.md files, about 13 KB (roughly 3k tokens).
- The Claude Code system prompt itself.

## 2. What Anthropic's guidance says

- **Defer tool definitions; use tool search.** Anthropic reports that tool search cuts tool-definition tokens by 85% or more. Its example went from about 77k to 8.7k tokens, and accuracy rose. Claude Code does this by default, and `"alwaysLoad": false` in a server's config keeps that server's tools deferred. ([Advanced tool use](https://www.anthropic.com/engineering/advanced-tool-use), [Claude Code MCP docs](https://code.claude.com/docs/en/mcp))
- **Prefer CLIs where one exists.** Claude Code's cost guide says:
  - CLI tools such as `gh`, `aws` and `gcloud` are "still more context-efficient than MCP servers because they don't add any per-tool listing".
  - Disable servers you aren't using.
  - Run `/context` to see what is using space.
  - Keep CLAUDE.md under 200 lines.
  - Move workflow-specific instructions into skills, which load on demand.

  ([Manage costs effectively](https://code.claude.com/docs/en/costs))
- **Code execution instead of tool-call round trips.** Anthropic suggests exposing MCP servers as code APIs, so the agent writes one script that calls many tools. Intermediate results stay in the sandbox and only the final output enters context. The example went from 150k to 2k tokens (98.7%). Programmatic tool calling saved 37% on a complex research task. The platform docs measured about 38% on a 75-tool benchmark, and no gain, or 8% more cost, on strictly sequential single-call flows. ([Code execution with MCP](https://www.anthropic.com/engineering/code-execution-with-mcp), [Programmatic tool calling](https://platform.claude.com/docs/en/agents-and-tools/tool-use/programmatic-tool-calling))
- **Concise tool responses.** Return only the fields needed for the next step. A `response_format` concise/detailed switch cut tokens to about a third in Anthropic's example. Truncation and pagination steer the agent toward cheaper behaviour. ([Writing effective tools for agents](https://www.anthropic.com/engineering/writing-tools-for-agents), [Define tools](https://platform.claude.com/docs/en/agents-and-tools/tool-use/define-tools))
- **Just-in-time context and file handoffs.**
  - Agents should hold lightweight references (file paths, stored queries) and load data only when needed.
  - Subagents keep detailed work in their own context and return summaries.
  - Long-running harnesses hand work between agents through files and structured artifacts.

  ([Effective context engineering](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents), [Multi-agent research system](https://www.anthropic.com/engineering/multi-agent-research-system), [Harness design for long-running apps](https://www.anthropic.com/engineering/harness-design-long-running-apps))
- **Skills use progressive disclosure.** About 100 tokens of metadata per skill sit in context, and the body (under 5k) loads only when triggered. That is cheap per skill, but 350 skills is still roughly 35k tokens. ([Agent Skills overview](https://platform.claude.com/docs/en/agents-and-tools/agent-skills/overview), [Equipping agents with Agent Skills](https://www.anthropic.com/engineering/equipping-agents-for-the-real-world-with-agent-skills))
- **Long sessions are the main driver.** Every request re-sends the whole conversation. Prefer `/clear` to compaction for a fresh start, because compaction itself reads the whole context. ([Manage costs effectively](https://code.claude.com/docs/en/costs))

## 3. The options compared

| Option | What it saves for us | What it costs or risks | Verdict |
|---|---|---|---|
| **Keep MCP, trimmed** (done this batch: lifecycle results trimmed, `wait_tasks` trimmed) | Most of the 4–10% result share | Nothing; `full=True` gets the raw result | Keep |
| **A `fleet` CLI called through Bash instead of `local-llm` MCP** | Removes about 30 tool names and the server instructions from startup, roughly 1–2k tokens per turn (the schemas are already deferred). Output can be filtered inline (`| head`, `--json | jq`). | Loses typed arguments, the event-based `wait_tasks` long-poll unless the CLI blocks the same way, and per-tool permission scoping. Needs Bash permission for one more binary. | Small win. Worth adding next to MCP, not as a replacement. |
| **`.md` files for agent-to-agent comms** | Large payloads (findings, diffs, plans) pass as a path instead of being pasted into a prompt or result, and readers load only what they need. Anthropic recommends exactly this for artifacts. | Reading a file costs the same tokens as a tool result. It saves only when the reader skips most of it. It has no events, so agents must poll (each poll is a full-context turn, the most expensive thing we have). Race conditions and no state machine. It would duplicate `fleet.db`. | Use for artifacts only (findings, handoff, plan), referenced by path. Never for control signals: those stay in `fleet.db` and events. |
| **Run whole loops as code inside the controller** (the code-execution pattern; we have `run_pipeline`, `set_chain`) | One call runs spawn → wait → review → fix → merge, and returns a 5-line summary. This removes most orchestrator turns, which is the only lever on the 75% startup cost. | The pipeline has to be encoded once, and edge cases need human escalation | **The biggest lever.** Make it the default batch path. |
| **A lean orchestrator profile** (same idea as the workers' `mcp-empty.json`) | Takes startup from about 90k toward 20–30k, which saves about 60k × every turn: around 10M cache reads per chat at these lengths. | Must keep the fleet tools (`local-llm`, `ccd_turn`) and the `fleet-batch` skill. Design and SEO plugins move to a separate profile. | **Do this first.** Largest saving, lowest risk. |
| **Fewer, shorter orchestrator chats** (one chat per batch, `/clear` instead of compaction) | Fewer turns × baseline | Discipline; the plan state is already in `put_plan` | Already the rule; enforce it |

## 4. Recommendations, ranked by tokens saved

1. **Lean orchestrator profile.**
   - Launch orchestrator sessions with `--strict-mcp-config` and an MCP config that holds only `local-llm`.
   - Turn off the plugins and skill packs the fleet doesn't use (design, SEO, marketing, language packs).
   - Measure with `/context` before and after.
   - Target: under 30k at startup. Expected saving: about 10M cache reads per batch chat, roughly half of what one chat costs today.
2. **Run batches as one controller call.**
   - Extend `run_pipeline` or `set_chain` so a whole batch (builds → gate → review → fix rounds → merge task) runs inside the controller and returns one summary.
   - The chat then spends about 5 turns per batch instead of about 150. That cuts the remaining baseline cost by the same ratio.
3. **Keep the per-batch chat rule, and prefer `/clear` to compaction.** Each of these chats compacted 5–7 times, and every compaction reads the full context.
4. **Artifacts by path.**
   - Reviews and handoffs write findings to a file in the task worktree (or `fleet.db`).
   - Prompts and results carry the path and a one-line verdict, not the full text.
   - Control flow stays in `fleet.db` and events.
5. **Optional `fleet` CLI** with `--json` and summary output, for the orchestrator's Bash and for humans. Keep MCP for blocking waits and scoped permissions.
6. **Visual QA out of the chat.** Screenshots cost 0.86M in one chat. Send them to a Haiku `check` task (already in `fleet-batch`).

## 5. Not measured

- The exact parts of the 90k startup context. Run `/context` in a fresh orchestrator session.
- The code-execution savings on our own workload. Anthropic's 98.7% is a best case for many-step data flows, and the platform docs report no gain on strictly sequential flows. Measure on one batch first.
