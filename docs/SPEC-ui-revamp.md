# SPEC: Console UI revamp (16 Nov 2026)

Status: direction C (Workbench) chosen by the owner on 16 Nov, with an intelligence layer (section 6a).
Scope: `master/controller/ui/` (React 18, react-router 6, TanStack Query 5, Vite 5) and small additive routes in
`master/controller/`. The full page and route inventory behind this spec is in the session notes
(`ui-inventory.md`); the summary below is what the redesign needs.

## 1. What exists today

14 routes under `/app`, a sidebar shell, one SSE stream (`/api/events`) that invalidates every query, 30s polling
fallback, permission-aware controls (`/api/me`), IBM Plex Sans/Mono, sage/teal palette with light and dark.

| Page | Shows | Does |
|---|---|---|
| Overview `/` | needs-attention, working now, usage, call stats | approve/reject, open task |
| Live `/live` | machines, accounts, queue, chains, alerts, token feed | posture check, account reset |
| Budget `/budget` | spend vs caps, usage by model/kind/day, savings, rules + history, decisions | edit/restore rules |
| Tasks `/tasks` | work items or all attempts, tabs by state | new task, open TaskSheet |
| TaskSheet (dialog) | log, diff, handoff | message, approve/reject, cancel, complete, resolve, cleanup, handoff |
| Pipelines | list + detail | start, retry reviews, cancel |
| Models | catalog x nodes | add, install/remove/retry |
| Roles | assignments | assign, unassign, edit role |
| Nodes, `/nodes/:id` | node list, detail | rescan, enroll, approve, drain, ceiling |
| Policies, Skills | layered config + history | edit, bind |
| Evals | runs, leaderboard, recommendations | run eval, assign suggested |
| Activity | event list (1000, unpaged) | filter |
| Settings | theme, notify rules, export/import | toggle, test, import |

## 2. Problems

1. **Navigation is flat.** 13 peer links, no grouping by job. Overview and Live overlap.
2. **Work is task-shaped, not work-item-shaped.** Chains, stages, reviews, gate rounds and merges are scattered
   across Tasks, Live and Pipelines. The owner can't see "feature X: built by Sonnet, reviewed by Fable, gate 2/3,
   waiting merge" in one place.
3. **TaskSheet is a cramped dialog** carrying 10 actions, log, diff and handoff. No token/cost per task
   (`/api/tokens/{id}` unused), no outcome, no `resolved_by` picker, `window.prompt` for resolve.
4. **Plans are invisible.** `/api/plan`, `/api/leader`, chains (`PUT /api/work/{root}/chain`) have no UI.
5. **Budget hold** (`/api/budget/rules/hold`) and catalog delete (`DELETE /api/models/{id}`) have no UI.
6. **Global refetch on every SSE event** re-pulls everything; fine at today's scale, wasteful on Live.
7. **No shared primitives** (button, table, dialog, tabs, menu): every page hand-rolls them, so focus, keyboard
   and empty/error states differ page to page.
8. **No UI step in the gate.** `scripts/gate.sh` runs no typecheck/test/build for the UI; `ui/dist` is committed.

## 3. Information architecture (new)

Four groups in a collapsible left rail, plus a command palette (`⌘K` / `Ctrl K`) that reaches every page, task,
node and action.

| Group | Page | Purpose |
|---|---|---|
| **Operate** | Now (`/`) | Replaces Overview + Live. Attention inbox, running work, machines, accounts, alerts. |
| | Work (`/work`, `/work/:root`) | Work items as chains. Detail page = timeline of attempts, current diff, gate, reviews, actions. Replaces Tasks + Pipelines list. |
| | Plan (`/plan`) | The orchestrator plan and leader state; edit plan items. |
| **Spend** | Budget (`/budget`) | Today/7d/30d spend per model and account, limits, reset times, rules, hold, decisions, savings. |
| **Fleet** | Nodes (`/nodes`, `/nodes/:id`) | Posture, health, capacity, models per node, onboarding wizard. |
| | Models (`/models`) | Catalog x node matrix, install/remove/delete. |
| | Roles (`/roles`) | Role -> model routing, evals leaderboard and suggestions inline. |
| **Configure** | Policies, Skills, Evals runs, Activity, Settings | Kept, restyled on shared primitives. |

Old routes (`/live`, `/tasks`, `/pipelines`, `/overview`) redirect to their new homes.

## 4. Functionality to add

- **Work detail page** (`/work/:root`): chain strip (spec -> build -> gate -> review -> fix -> final -> merge, with
  model and account per stage), attempts list, live log with follow toggle, diff viewer with file tree, gate
  summary, findings (HIGH/MED/LOW), tokens and cost per attempt, and an action bar: message, approve/reject,
  cancel, complete, resolve (dialog with `resolved_by` picker), outcome, cleanup, handoff, spawn follow-up
  (review/fix/merge prefilled with `parent_id` and `link`).
- **Spawn** form upgraded: agent, model, profile, kind, stage, parent, gate, mode, effort; shows the budget rule
  that will apply before submit (dry check against `/api/budget/status`).
- **Chains:** edit planned stages for a work item (`PUT /api/work/{root}/chain`).
- **Plan:** view and edit plan items (`GET/PUT /api/plan`, `PUT /api/plan/{item}`), leader state.
- **Budget:** hold on/off (`/api/budget/rules/hold`), per-account rate-limit bars with reset countdowns.
- **Nodes:** onboarding wizard (enroll -> command -> waiting for node -> approve), posture check results, drain.
- **Models:** delete catalog entry.
- **Keyboard:** `⌘K` palette, `g n` / `g w` / `g b` jumps, `j/k` row nav, `Enter` open, `Esc` close, `?` help.

Never in the UI for the orchestrator's scope: `budget_override` and approvals stay admin-only and are shown only
when `/api/me` says admin.

## 5. New controller routes (additive, owned here)

All in a new module `master/controller/ui_api.py`, mounted with one `include_router` line, so the parallel
`controller-intel` work in scheduling/allocation does not conflict.

| Route | Purpose |
|---|---|
| `GET /api/work/{root}` | One work item with attempts, chain, gate summaries, findings, token totals (joins existing data). |
| `GET /api/search?q=` | Palette search over tasks, nodes, models, roles (limit 20). |

Everything else uses existing routes.

## 6. Design directions (owner picks one)

Mocks in `docs/mocks/` (Now, Work detail, Nodes; light and dark). Shared rules for all three: system or self-hosted
fonts only, a real type scale, tabular numerals for numbers, 4px spacing grid, WCAG AA contrast, visible focus
rings, no gradients, no emoji, no glass, no decorative shadows.

- **A. Ledger.** Dense, print-like operations sheet. Warm off-white / near-black, one ink accent, hairline rules,
  serif-less grotesk + mono numbers. Tables first. Feels like a well-set financial terminal.
- **B. Control room.** Dark-first, high information density, status colour is the only colour, monospace
  everywhere data lives, compact panes with keyboard focus. Feels like a tmux/Grafana hybrid done properly.
- **C. Workbench.** Calm, roomier, light-first. Work item as the hero: big chain/timeline, side panel for
  context. Feels like Linear/GitHub's PR view, but with this fleet's data.


## 6a. Chosen: Workbench + intelligence layer

The owner chose C and asked that the console feel like the UI of an intelligent system, in function as much as
looks. The console should not only display state; it should explain it, predict it and propose the next move,
with every proposal one click (and one confirmation) away. Intelligence is grounded in controller data only; no
model is called from page render.

| Capability | What the owner sees | Source |
|---|---|---|
| **Briefing** | Top of Now: 3-5 sentence plain-language state of the fleet ("2 items need you; Codex at 83% of weekly, final reviews only until Thu 09:00; laptop offline 3h, its queue moved to master"). | Derived client-side from `/api/live`, `/api/budget/status`, `/api/fleet`; optional `fast` role summary on demand. |
| **Next best actions** | Ranked cards: "Merge t-3f9a12c0 (gate green, Fable approved)", "Re-run gate on t-9c1e07b2 (runner not re-embedded: run nodes/build.py)". Each card: why, expected cost, one action button. | Rules over task/gate/review state (`lib/insights.ts`). |
| **Why?** on every decision | Placement, reroute, budget refusal, queue wait each show the controller's reason ("routed to Sonnet: Codex limited until 16:42; rule v3 'builds->Sonnet'"). | `placement`, `routed_from`, `budget_wait`, `/api/budget/decisions`. |
| **Forecasts** | Per paid model: projected time to soft/hard limit at the current burn rate; queue ETA; per work item, remaining stages x typical cost. | `/api/tokens/summary`, rate_limits, chain. |
| **Anomalies** | Stalls, flapping nodes, repeated gate failures on the same trap, token spikes, "same model built and reviewed". | Events + tasks, `lib/insights.ts`. |
| **Intent bar (⌘K)** | Palette accepts commands and plain intents: "review t-3f9a12c0 with fable", "drain laptop", "spawn build: …". Shows the exact request (agent, model, kind, stage, parent, budget rule that applies) before running. | Deterministic parser first; `fast` role fallback only to fill a form, never to execute. |
| **Ask the fleet** | Side panel on a work item: questions about this item answered from its log/diff/findings by the local `fast` role, with citations to log lines. | New `POST /api/ask` (local roles only, never paid, redacted). |
| **Confidence and provenance** | Every insight shows its evidence (links to events/log lines) and how sure it is; nothing auto-executes. | — |

Additive route for this: `POST /api/ask` in `ui_api.py` (local `fast` role through the existing gateway; refuses
paid models; uses the existing redaction).


## 6b. Terminal frames and responsive rules (owner, 16 Nov)

**Terminal frame.** Every place that shows logs or work done in a shell (task log, gate output, posture check
output, enroll command, Ask-the-fleet citations, Activity raw events) renders in one shared `<Terminal>`
component: a window frame with a title bar (three neutral dots, title = `node:~/worktree (branch)`, right side:
follow toggle, copy, wrap, full-screen), dark surface in both themes, mono font, line numbers that are
anchors (`#L120`, used by insight evidence links), ANSI colour mapped to the theme's status colours, a blinking
block cursor on the last line while the task is live, a prompt prefix (`$`) on command lines, search within
(`/`), and a "jump to latest" pill when scrolled up. No gradients, glows or scanline gimmicks; restrained and
crisp. `prefers-reduced-motion` stops the cursor blink.

**Responsive, no breakage.** Breakpoints 360 / 640 / 1024 / 1440.
- Rail: full at >=1024, icon-only collapsed at 640-1023, off-canvas drawer below 640.
- Two-column layouts (work detail + context panel) stack below 1024; the context panel becomes a tab.
- Tables: become stacked cards below 640 or scroll inside their own container; the page body never scrolls
  horizontally. Long ids, branch names and paths truncate with ellipsis plus a title tooltip, or wrap with
  `overflow-wrap:anywhere` in prose.
- Controls: one size scale (`sm 28px`, `md 36px`, `lg 44px` min touch target on coarse pointers); buttons,
  inputs, selects and chips in one row share height; labels never wrap inside buttons (`white-space:nowrap`),
  action bars wrap as a whole row instead.
- Cards in a grid use `grid-template-columns: repeat(auto-fill, minmax(min(100%, 280px), 1fr))`, equal heights per
  row, no fixed pixel widths on content containers.
- Acceptance: Playwright-free check in vitest is not possible, so each page is screenshotted at 375, 768 and
  1440 wide in the review round and any overflow (`scrollWidth > clientWidth` on `body`) is a HIGH finding.

## 7. Acceptance criteria

1. All pages in section 3 exist; old routes redirect.
2. Every data view has loading, empty and error states built from shared primitives.
3. Every action from section 4 calls the listed endpoint and is hidden without the right scope.
4. Keyboard: palette opens with `⌘K`/`Ctrl K`, all actions reachable without a mouse, focus visible.
5. Light and dark themes both pass AA contrast (checked on tokens).
6. `npm run typecheck`, `npm test` and `npm run build` pass; a UI step is added to `scripts/gate.sh`;
   `tests/test_console.py` still passes; `ui/dist` rebuilt and committed.
7. New routes have Python tests (`tests/test_ui_api.py`).

## 8. Test cases (written before build)

- `lib/nav.test.ts`: legacy route map redirects; group/page list matches section 3.
- `lib/work.test.ts`: chain derivation from attempts (stage order, current stage, gate round count, verdicts).
- `lib/palette.test.ts`: search ranking and keyboard shortcut parsing.
- `lib/insights.test.ts`: next-best-action rules, anomaly rules, forecast maths, briefing sentences.
- `lib/intent.test.ts`: intent parser -> spawn/message/drain request bodies; unknown intents fall back to search.
- `lib/spawn.test.ts`: spawn form -> request body (kind/stage/parent/link), validation.
- `tests/test_ui_api.py`: `GET /api/work/{root}` shape and 404; `GET /api/search` limits and scope.

## 9. Build plan (after direction is chosen)

Small fleet tasks, Sonnet builders, one writer per file set, run in parallel where files don't overlap:
1. Tokens + primitives (button, table, dialog, tabs, menu, states) + shell/nav/palette.
2. `ui_api.py` + tests.
3. Now page. 4. Work list + detail. 5. Budget. 6. Nodes + onboarding. 7. Models/Roles/Evals restyle.
8. Plan page + Policies/Skills/Activity/Settings restyle.
Round review by Fable (guidance only), final whole-branch review by Codex (`gpt-5.6-terra`, high).

## 6c. Lineage (owner addition, 16 Nov)

A new page, **Lineage** (`/lineage`, under Operate, `g l`), shows tasks, agents and the whole fleet as they work, and lets the owner trace any piece of work end to end. It is the "flight recorder" of the fleet.

**Lenses** (tabs, one shared time axis and scrubber):
1. **Fleet river.** Swimlanes, one per node and account (grouped by node). Each attempt is a bar on its lane from start to end, coloured by state, with its kind as an icon. Edges between bars show lineage links (retry, fix, review, merge, handoff) as curved arrows labelled with the link reason. A vertical "now" line moves in real time while follow mode is on. Queue waits show as hatched segments before a bar starts. Limit hits, stalls and gate failures are marked as small diamonds on the bar.
2. **Trace** (`/lineage/:root`). The work item's DAG laid out left to right by stage (spawn, build, gate, review, fix, re-review, merge, done). The critical path (the chain of dependencies that set the end time) is highlighted, and every other node is dimmed. Clicking a node opens a side panel with the attempt's terminal log excerpt (the shared Terminal), gate summary, findings, tokens and duration. A replay control plays the trace back step by step from the event history.
3. **Agents.** One row per agent and model (claude-sonnet, codex, local roles). It shows a heartbeat strip of the agent's activity: when it ran, idle gaps, tokens per minute, and which account it ran on. This answers "who is doing what right now".

**Intelligence on the page** (in line with 6a, it advises and never acts):
- **Bottleneck callout.** The longest queue wait or slowest stage in view, with why (budget wait, node busy, limit until).
- **Anomaly markers.** Retries above two, gate rounds above three, stalls and cost outliers against the per-kind median.
- **Narrative.** A one-paragraph "what happened" for a trace, generated deterministically from the events. No model is needed.

Each insight shows its evidence (task ids and timestamps).

**Data.** A new additive route `GET /api/lineage?since=&until=&root=` in `ui_api.py` (view scope) returns:
- `nodes`: attempts with id, root_id, parent_id, link, kind, stage, agent, model, account, node, state, created/started/ended, tokens.
- `edges`: parent to child, with the link reason.
- `lanes`.
- `events`: state transitions with timestamps, taken from what the task records already hold.

The page live-updates through the existing SSE invalidation.

**Rendering.** Hand-written SVG, no new dependencies. Layout, critical path, lane packing and narrative live in `src/lib/lineageGraph.ts`, which has unit tests. It is responsive:
- Below 1024px, the river scrolls horizontally inside its own container.
- Below 640px, the trace becomes a vertical list of steps.

It respects reduced-motion and works in both themes.

**Tests.** `lineageGraph.test.ts` (lane packing without overlap, critical path, narrative text, stage ordering); `tests/test_ui_api.py` gains `/api/lineage` cases.
