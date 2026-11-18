# Motion scope: making the console feel alive (18 Nov 2026)

## Goal

The console should feel like watching an intelligent fleet think and work, not like reading a database. Motion is the means, but the rule that makes it feel like *intelligence* rather than decoration is:

> **Every animation reports a real signal.** If nothing is happening in the fleet, nothing moves.

A pulse means a heartbeat was received. A particle means work moved along an edge. A shimmer means tokens are arriving right now. Fake activity (shimmers on idle cards, looping decorative backgrounds) is the opposite of the "AI feeling": it trains the owner to ignore motion. Practitioners make the same point. Shimmer that runs while an agent is actually deep in a reasoning loop is "a polite fiction", and the motion that works is believable rather than slick ([anagram.ai](https://anagram.ai/blog/transparency-is-the-new-speed-why-we-stopped-hiding-our-ais-homework), [design-for-builders](https://design-for-builders.beehiiv.com/p/ai-native-ui-how-to-build-what-everyone-else-will-copy-in-2026)).

What exists today:
- The Lineage graph already has flow particles, pulses, a scrubber and an entrance animation (`graph-flow`, `graph-pulse`, `lin-beat` and similar).
- The shell has a live dot, `blink`, skeletons and the sheet slide-in.
- Everything else is static.
- `prefers-reduced-motion` is handled in only four places.
- There are no motion tokens.

## Motion language: eight primitives

Each primitive has one meaning. A page uses the primitives; it doesn't invent new effects.

| # | Primitive | Means | Look | Timing |
|---|---|---|---|---|
| 1 | **Pulse** | Something is alive right now (node heartbeat, running task) | A soft ring expands and fades from a status dot | 1.6s loop, once per heartbeat. **At most one pulsing cluster per viewport.** |
| 2 | **Flow** | Work is moving between agents or stages | Particles travel along an edge or chain connector, in the direction of the handoff | Speed scales with throughput. Stops when the edge goes idle. |
| 3 | **Think** | A model is producing tokens now | A token-stream shimmer on the task's line, a blinking caret at the end of a streaming terminal, and a small sparkline of tokens per second | Only while tokens arrive. Fades out within 1s of the last token. |
| 4 | **Arrive** | New data appeared (row, event, finding) | Slides 4px up from a soft accent wash that fades to the normal background | 320ms in, with the wash fading over 1.2s |
| 5 | **Count** | A number changed | Interpolates from the old value to the new one (never from 0), using tabular numerals | 600ms ease-out, up to 800ms for large jumps. Snaps under reduced motion. |
| 6 | **Settle** | A state resolved (passed, failed, merged, approved) | The badge morphs colour, and the check or cross draws in with a stroke animation. A failure gets a single 2px shake. | 320ms, with the shake once only |
| 7 | **Focus** | Moving from overview to detail | A view transition: the clicked row's title morphs into the detail page's heading, and the rest cross-fades | 200–280ms |
| 8 | **Ambient** | The fleet's overall state, at a glance | The "fleet constellation" in the Now header: nodes are dots, roles are orbits, and links light up when a task runs between them | Driven only by `fleet_status` and live tasks. Fully still when idle. |

### Tokens (add to `styles.css`)

```css
:root {
  --dur-1: 120ms;  /* hover, press */
  --dur-2: 200ms;  /* toggles, small reveals */
  --dur-3: 320ms;  /* arrive, settle, panels */
  --dur-count: 600ms;
  --ease-out: cubic-bezier(.2, .8, .2, 1);
  --ease-in-out: cubic-bezier(.65, 0, .35, 1);
  --ease-in: cubic-bezier(.4, 0, 1, 1);    /* exits only */
}
@media (prefers-reduced-motion: reduce) {
  :root { --dur-1: 0ms; --dur-2: 0ms; --dur-3: 80ms; --dur-count: 0ms; }
}
```

The durations follow common design-system conventions: 150–400ms for transitions, and tickers up to about 800ms ([eldoraui guide](https://eldoraui.vercel.app/blog/micro-interactions-guide), [pluma motion](https://pluma.customer.io/foundations/motion)).

### Guardrails (acceptance criteria for every phase)

1. **Real signal only.** Each animated element names its data source in code (a prop or hook argument). The review rejects motion that has no source.
2. **Reduced motion.** Under `prefers-reduced-motion`:
   - Loops stop.
   - Counts snap to their final value.
   - Arrive becomes an opacity change of 80ms or less.
   - View transitions are skipped.
3. **Performance.**
   - Only `transform` and `opacity` are animated. Allowed non-transform exceptions are the Arrive background wash, the Settle stroke draw, the offline desaturate filter, and SVG offset/animateMotion particles.
   - All loops pause when `document.visibilityState` is hidden.
   - Above 150 animated elements, only the focused path animates (the same rule as the Lineage graph).
   - No new dependencies: CSS, Web Animations API and SVG `animateMotion` only.
4. **Restraint.**
   - One pulsing cluster per viewport.
   - Tables of numbers don't tick each cell. Only KPI tiles count.
5. **Screen readers.** Counts and arrivals announce through a polite live region (`aria-live="polite"`), at most one announcement every 5s.
6. **Fallback.** `document.startViewTransition` is feature-detected, with an instant swap when it is missing ([MDN](https://developer.mozilla.org/en-US/docs/Web/API/Document/startViewTransition), [Chrome docs](https://developer.chrome.com/blog/view-transitions-in-2025)).

## Where each primitive goes

| Page | What comes alive | Primitives |
|---|---|---|
| **Shell** | The live dot pulses on each controller event. The "N need you" chip counts and settles when the count changes. Route changes cross-fade at 200ms. The palette opens with a 120ms scale from 0.98. | Pulse, Count, Focus |
| **Now** | The header gets a **fleet constellation**: nodes, role orbits, and links lighting up for live tasks. The briefing streams in sentence by sentence when it is first loaded (the text is already computed, so this is only a reveal). Next-best-action cards arrive; approve or reject settles and collapses. KPI tiles count. | Ambient, Arrive, Settle, Count |
| **Work** | New and updated rows arrive with a wash. A running row shows a thin progress sheen driven by elapsed time against the expected duration, plus a token sparkline. The chain strip shows flow on the active connector. | Arrive, Think, Flow |
| **Work detail** | The terminal streams with a caret, and auto-scroll pauses when the user scrolls up. Gate results settle (tests turn green one by one as they are parsed). Findings arrive one by one, by severity. The title morphs in from the list. | Think, Settle, Arrive, Focus |
| **Lineage** | It already has Flow, Pulse and the scrubber. It adds Settle on node state changes, Arrive for new nodes (they grow in from their parent's edge), and Focus from Work. | Settle, Arrive, Focus |
| **Plan** | Items settle as they complete, and the progress bar fills with an ease-out. The current item breathes faintly; it is the page's one pulse. | Settle, Pulse |
| **Live** | Queue rows arrive and leave: they slide out to the right with an ease-in once started. Account meters fill with Count. A small rate graph shows events per minute. | Arrive, Count, Flow |
| **Budget** | Usage bars fill on load and update with Count. Crossing the soft budget animates the colour toward warning once, never with a loop. Reset countdowns tick each minute without animation. | Count, Settle |
| **Models / Roles** | A model that is loading shows a load shimmer, driven by the node's real load state. Role reassignment animates the model chip moving between rows. | Think, Focus |
| **Nodes / node detail** | A heartbeat pulse arrives on each `seen` update. CPU and GPU sparklines stream. A node going offline desaturates over 600ms and stops pulsing. | Pulse, Think, Settle |
| **Policies / Skills / Settings** | Calm pages. Only save settles (button → check). Diff lines arrive when a version is selected. | Settle, Arrive |
| **Evals** | Scores count when a run lands. A new best score gets one Settle highlight. A running eval shows Think on its row. | Count, Settle, Think |
| **Activity** | The feed prepends with Arrive, and the time grouping headers stay sticky. | Arrive |

## Signature moment: the fleet constellation (Now)

This is the one piece meant to deliver the "wow, it's alive" feeling. It is a hand-written SVG, about 180px tall on desktop and 120px on phones:
- **Nodes:** each node is a dot. Its colour comes from health; its size from the number of models loaded.
- **Roles:** coder, reviewer and fast are faint orbits around their node.
- **Agents:** Codex and the Claude accounts sit on an outer ring as labelled marks.
- **Live tasks:** each live task draws a curved link from its agent to its node, with Flow particles. When a gate passes, the link flashes green (Settle) and fades.
- **Idle:** everything is still. That is the point, so a calm fleet looks calm.
- **Interaction:** clicking a dot or link opens the same detail panel as Lineage.

## Phases

| Phase | Scope | Gate |
|---|---|---|
| **M1 Foundation** | Motion tokens. A `useCountUp(value)` hook. An `<Arrive>` wrapper keyed by id. A `useLiveSignal` helper that returns false after a timeout. A `withViewTransition(navigate)` helper. A global reduced-motion audit, and the visibility pause. | Vitest for the hooks (interpolation from the previous value, snap under reduced motion, timeout); tsc |
| **M2 Now + Shell** | The constellation, the briefing reveal, card arrive and settle, and the shell count and pulse | Visual check at 1440 and 375, in both themes |
| **M3 Work + Work detail** | Arrive rows, the Think sparkline and sheen, terminal streaming, gate settle, and the Focus morph | Same |
| **M4 Remaining pages** | The table above | Same, plus the WIG re-audit of motion rules |

## Mocks

The interactive mocks are in `docs/mocks/motion/index.html`, a single self-contained file built by Codex from this doc. They show the Shell + Now (with the constellation), Work, Work detail and Lineage, with a toggle to simulate an idle or busy fleet and a reduced-motion toggle.

## Sources

- [Transparency is the new speed (anagram.ai)](https://anagram.ai/blog/transparency-is-the-new-speed-why-we-stopped-hiding-our-ais-homework)
- [AI-native UI (design-for-builders)](https://design-for-builders.beehiiv.com/p/ai-native-ui-how-to-build-what-everyone-else-will-copy-in-2026)
- [ui-for-ai component patterns](https://github.com/bardbyte/ui-for-ai)
- [AI agent processing states (21st.dev)](https://21st.dev/@arunjdass/components/ai-agent-processing-states)
- [Micro-interactions guide (Eldora UI)](https://eldoraui.vercel.app/blog/micro-interactions-guide)
- [Pluma motion foundations](https://pluma.customer.io/foundations/motion)
- [Animation in business applications](https://thepropermotioncompany.pages.dev/blog/animation-in-business-applications-when-and-how/)
- [MDN `startViewTransition`](https://developer.mozilla.org/en-US/docs/Web/API/Document/startViewTransition)
- [Chrome: view transitions in 2025](https://developer.chrome.com/blog/view-transitions-in-2025)
