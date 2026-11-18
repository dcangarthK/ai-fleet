# Web Interface Guidelines audit (17 Nov 2026)

I audited `master/controller/ui/src` against the Vercel Web Interface Guidelines (vercel-labs/web-interface-guidelines). The items are grouped by type. Line numbers are approximate.

Commit 2293535 already fixed five issues: the select chevron, muted text on primary buttons, the terminal on mobile, the close button on the lineage sheet, and the Live badge and disabled state.

## Disabled, hover and active states
1. **Disabled variant buttons look active** (`styles.css`). `button:disabled` loses to later rules for `.primary`, `.danger` and `.ghost`. Move the disabled block after the variants or raise its specificity, so a disabled variant keeps the muted look.
2. **Hovering a selected chip or layer drops its selected look.** `button:hover:not(:disabled)` beats `.chip.on` and `.layer.on`. Add hover rules that keep `--accent-soft` and `--accent`.
3. **Non-clickable rows highlight on hover.** Change `tbody tr:hover td` so only `tr.click:hover td` highlights.
4. **Danger menu items highlight in blue** (`.menu [role=menuitem].danger`). Use `--bad-soft` on hover and focus.

## Touch targets and mobile
5. **Mobile rules shrink controls below the coarse-pointer size.**
   - At under 640px, `.chip` drops to `min-height: 24px`. Drop `.chip` from that rule.
   - At under 640px, `.now a`, `.now button`, `.work a` and `.work button` get `min-height: 24px`, which overrides the coarse 44px. Use `var(--h-sm)` or remove the rule.
6. **The collapsed rail is too narrow.** `.side .nav a` is 40px wide; make it 44px.
7. **Graph actions ignore the coarse size** (`lineage.css` `.graph-actions button`). Change `min-height: 28px` to `var(--h-sm)`.
8. **The graph traps page scroll on phones** (`.graph-svg`). Use `touch-action: pan-y` by default, and switch to `none` only while the user drags the graph.
9. **iOS zooms on focus.** Add `@media (pointer: coarse) { input, select, textarea { font-size: 16px } }`.
10. **The lineage graph is too narrow at tablet widths.** Between 640 and 1023px, `.graph-layout` and the river layout should collapse to one column below 1024px.
11. **The insight strip is cramped at tablet widths.** Between 640 and 1023px it has 5 columns; use `repeat(auto-fit, minmax(120px, 1fr))`.
12. **Long `dt` labels can squeeze the value to nothing** (`now.css` `.kv`). Change the label column to `minmax(80px, 40%)`.

## Class collisions and layout
13. **`.ops` collides.**
    - The Now grid (`.now .ops`) inherits the shell install strip's padding, border, background and negative margin.
    - Rename the shell one to `.install-strip`.
14. **`lin-range` is used twice** (`Lineage.tsx`).
    - A `<label className="lin-range">` reuses the slider class.
    - Rename the label to `.lin-range-select`, and scope the slider rule to `input.lin-range`.
15. **Panels clip popups** (`Card.tsx` `Panel`).
    - Its inline `overflow: hidden` clips focus rings and menus.
    - `.panel` and `.tbl-wrap` with `overflow-x: auto` also clip absolute `.menu` popups.
    - Use `overflow: clip` with `overflow-clip-margin`, or position menus as fixed.

## Focus
16. **Focus changes control shapes.** The global `:focus-visible { border-radius: 3px }` reshapes focused controls. Remove the radius.
17. **Focus rings are clipped** on `.tabs` buttons and `.crumbs` links. Use `outline-offset: -2px`.
18. **Focus can reach content behind the open mobile drawer.** Set `inert` on `.main-col` while the drawer is open (`Shell.tsx`).

## Dialogs
19. **Dialog close buttons don't match.**
    - `Dialog.tsx` uses a text "Close" button.
    - `NewTask` uses a "×" icon button.
    - `TaskSheet` uses a text "Close" button.
    - Use one icon-button pattern (×, aria-label "Close", 36px, or 44px on coarse pointers), center it vertically with the title, and keep it sticky in long forms.
20. **Title-less dialogs have no top padding.** Add `.dlg:not(:has(.dlg-head)) .dlg-body { padding-top: var(--s4) }`, and replace the palette's inline style with a `.dlg.palette .dlg-body` rule.
21. **NewTask inputs are missing attributes.**
    - Add `name` and `autoComplete="off"`.
    - Add `spellCheck={false}` to the repo and gate inputs.
    - End the placeholder with "…".
22. **The TaskSheet follow-up form uses inline styles.** Replace them with `Field` and `.row` with `align-items: flex-end`.

## Accessibility semantics
23. **The search button loses its label on mobile** (`.cmdk` under 640px). Its aria-label must include the visible text "Search pages, tasks, actions".
24. **The live-dot text is hidden from screen readers.** It uses `display: none` on mobile; use `.sr-only` instead.
25. **Badge labels are ignored.** `Badge.tsx` puts `aria-label` on a span with no role; add `role="img"` or sr-only text.
26. **Table selection uses the wrong attribute.** `Table.tsx` puts `aria-selected` on a `tr` outside a grid; use `aria-current="true"`.
27. **The install-strip bar has no progress semantics** (`Shell.tsx`). Reuse `ProgressBar`, which provides the progressbar role.
28. **`.op` links have no hover style** (`Shell.tsx`). Move the inline style to CSS with an underline on hover.
29. **The River panel close button has no type.** Add `type="button"`.
30. **`.stack-bars` is not reachable by keyboard.** As a scroll region it needs `tabindex="0"`, `role="region"` and an `aria-label`.
31. **Failed and attention stage chips rely on color alone.** Add a glyph prefix.

## Tokens and consistency
32. **Spacing is off the grid.** Values of 6, 9, 10, 14 and 18px are used in `.banner`, `.kv`, `th`/`td`, `.live-row`, `.ops`, `.rules-form`, dialog forms and `.sheet-body h3`. Map them to the `--s1` to `--s5` tokens, adding `--s-half: 6px` if needed.
33. **Inline styles bypass the tokens.** About 20 places use inline `gap` and `margin` values: `TaskSheet`, `work/Actions`, `work/Ask` and `ChainStrip`. Replace them with `.stack`, `.row` and the tokens.
34. **Font sizes are ad hoc.** Add `--fs-xs`, `--fs-sm` and `--fs-md` tokens and use them for the 10–13.5px sizes.

## Data
35. **The Lineage detail panel shows the wrong gate.** It says "Gate: Running" for a cancelled attempt. Show the gate state only when the attempt has finished; otherwise show "—".
