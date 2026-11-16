# SPEC: Console UI revamp (16 Nov 2026)

Status: draft, awaiting the owner's choice of design direction (section 6).
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
- `lib/spawn.test.ts`: spawn form -> request body (kind/stage/parent/link), validation.
- `tests/test_ui_api.py`: `GET /api/work/{root}` shape and 404; `GET /api/search` limits and scope.

## 9. Build plan (after direction is chosen)

Small fleet tasks, Sonnet builders, one writer per file set, run in parallel where files don't overlap:
1. Tokens + primitives (button, table, dialog, tabs, menu, states) + shell/nav/palette.
2. `ui_api.py` + tests.
3. Now page. 4. Work list + detail. 5. Budget. 6. Nodes + onboarding. 7. Models/Roles/Evals restyle.
8. Plan page + Policies/Skills/Activity/Settings restyle.
Round review by Fable (guidance only), final whole-branch review by Codex (`gpt-5.6-terra`, high).
