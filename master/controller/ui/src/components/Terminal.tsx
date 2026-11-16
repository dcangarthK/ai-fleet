import { Fragment, memo, useEffect, useId, useMemo, useRef, useState, type KeyboardEvent, type ReactNode } from "react";
import { parseAnsi, stripAnsi, type AnsiSpan, type ParsedLine } from "../lib/ansi";

/** Split `text` around case-insensitive matches of `q` (empty q = one plain piece). */
function pieces(text: string, q: string): { t: string; hit: boolean }[] {
  if (!q) return [{ t: text, hit: false }];
  const out: { t: string; hit: boolean }[] = [];
  const lower = text.toLowerCase();
  let at = 0;
  for (let i = lower.indexOf(q); i >= 0; i = lower.indexOf(q, at)) {
    if (i > at) out.push({ t: text.slice(at, i), hit: false });
    out.push({ t: text.slice(i, i + q.length), hit: true });
    at = i + q.length;
  }
  if (at < text.length) out.push({ t: text.slice(at), hit: false });
  return out;
}

function spanClass(s: AnsiSpan, extra?: string): string | undefined {
  const c = [
    s.fg && `a-${s.fg}`, s.bg && `b-${s.bg}`, s.bold && "s-bold", s.dim && "s-dim", s.italic && "s-italic", s.underline && "s-ul", extra,
  ].filter(Boolean).join(" ");
  return c || undefined;
}

/** Drop the leading "$ " from the spans so it can be drawn as a prompt. */
function withoutPrompt(spans: AnsiSpan[]): AnsiSpan[] {
  let drop = 2;
  const out: AnsiSpan[] = [];
  for (const s of spans) {
    if (drop > 0) {
      const n = Math.min(drop, s.text.length);
      drop -= n;
      if (n < s.text.length) out.push({ ...s, text: s.text.slice(n) });
    } else out.push(s);
  }
  return out;
}

const Line = memo(function Line({ n, line, q, anchorId, selected, current, cursor }: {
  n: number; line: ParsedLine; q: string; anchorId: string; selected: boolean; current: boolean; cursor: boolean;
}) {
  const isCmd = line.plain.startsWith("$ ");
  const spans = isCmd ? withoutPrompt(line.spans) : line.spans;
  const body: ReactNode = spans.map((s, i) => (
    <span key={i} className={spanClass(s, isCmd ? "cmd" : undefined)}>
      {pieces(s.text, q).map((p, j) => (p.hit ? <mark key={j} className={current ? "cur" : undefined}>{p.t}</mark> : <Fragment key={j}>{p.t}</Fragment>))}
    </span>
  ));
  return (
    <div className={selected || current ? "term-line hit-line" : "term-line"} id={anchorId}>
      <a className="term-ln" href={`#${anchorId}`} data-line={n} aria-label={`Line ${n}`}>{n}</a>
      <span className="term-text">
        {isCmd && <><span className="prompt">$</span>{" "}</>}
        {body}
        {cursor && <span className="term-cursor" aria-hidden="true" />}
        {"\n"}
      </span>
    </div>
  );
});

/**
 * Shared terminal frame for logs and shell output (spec 6b). Dark in both themes, ANSI colours mapped to status
 * tokens, `#L120` line anchors, `$` prompts, "/" search, follow + jump-to-latest, copy, wrap, full screen.
 */
export function Terminal({ title, text, live = false, height = 360, idPrefix = "", empty = "No output yet.", defaultWrap = false, label = "Terminal output" }: {
  /** Usually `node:~/worktree (branch)`. */
  title: string;
  text: string;
  /** The process is still running: blinking cursor, and follow starts on. */
  live?: boolean;
  height?: number | string;
  /** Prefix for line anchors when several terminals share a page (`gate-L3`). */
  idPrefix?: string;
  empty?: string;
  defaultWrap?: boolean;
  label?: string;
}) {
  const bodyRef = useRef<HTMLDivElement>(null);
  const frameRef = useRef<HTMLElement>(null);
  const searchRef = useRef<HTMLInputElement>(null);
  const searchId = useId();
  const [follow, setFollow] = useState(live);
  const [atBottom, setAtBottom] = useState(true);
  const [wrap, setWrap] = useState(defaultWrap);
  const [full, setFull] = useState(false);
  const [copied, setCopied] = useState(false);
  const [searching, setSearching] = useState(false);
  const [query, setQuery] = useState("");
  const [cur, setCur] = useState(0);
  const [hash, setHash] = useState(() => (typeof location === "undefined" ? "" : location.hash));

  const lines = useMemo(() => (text ? parseAnsi(text) : []), [text]);
  const q = query.toLowerCase();
  const matches = useMemo(() => (q ? lines.flatMap((l, i) => (l.plain.toLowerCase().includes(q) ? [i] : [])) : []), [lines, q]);
  const curLine = matches.length ? matches[Math.min(cur, matches.length - 1)] : -1;
  const anchor = (n: number) => `${idPrefix}L${n}`;

  useEffect(() => {
    const on = () => setHash(location.hash);
    window.addEventListener("hashchange", on);
    window.addEventListener("popstate", on);
    return () => { window.removeEventListener("hashchange", on); window.removeEventListener("popstate", on); };
  }, []);

  const hashLine = useMemo(() => {
    const m = new RegExp(`^#${idPrefix}L(\\d+)$`).exec(hash);
    return m ? Number(m[1]) : 0;
  }, [hash, idPrefix]);

  // Follow the tail.
  useEffect(() => {
    const el = bodyRef.current;
    if (el && follow) el.scrollTop = el.scrollHeight;
  }, [lines.length, follow, full]);

  // Scroll an anchored line into view (also when it arrives after the first render).
  const scrolledTo = useRef(0);
  useEffect(() => {
    if (!hashLine || hashLine > lines.length || scrolledTo.current === hashLine) return;
    scrolledTo.current = hashLine;
    setFollow(false);
    bodyRef.current?.querySelector(`[id="${anchor(hashLine)}"]`)?.scrollIntoView({ block: "center" });
  }, [hashLine, lines.length]); // eslint-disable-line react-hooks/exhaustive-deps

  // Scroll the current search match into view.
  useEffect(() => {
    if (curLine < 0) return;
    setFollow(false);
    bodyRef.current?.querySelector(`[id="${anchor(curLine + 1)}"]`)?.scrollIntoView({ block: "nearest" });
  }, [curLine, q]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    const el = bodyRef.current;
    if (el) setAtBottom(el.scrollHeight - el.scrollTop - el.clientHeight < 24);
  }, [lines.length]);

  function onScroll() {
    const el = bodyRef.current;
    if (!el) return;
    const bottom = el.scrollHeight - el.scrollTop - el.clientHeight < 24;
    setAtBottom(bottom);
    if (!bottom && follow) setFollow(false);
  }

  function jump() {
    const el = bodyRef.current;
    if (el) el.scrollTop = el.scrollHeight;
    setFollow(true);
  }

  function openSearch() {
    setSearching(true);
    requestAnimationFrame(() => { searchRef.current?.focus(); searchRef.current?.select(); });
  }

  function closeSearch() {
    setSearching(false);
    setQuery("");
    setCur(0);
    bodyRef.current?.focus();
  }

  function onFrameKey(e: KeyboardEvent) {
    const typing = (e.target as HTMLElement).tagName === "INPUT";
    if (e.key === "/" && !typing && !e.metaKey && !e.ctrlKey && !e.altKey) {
      e.preventDefault();
      e.stopPropagation();
      openSearch();
    } else if (e.key === "Escape") {
      if (searching) { e.stopPropagation(); closeSearch(); }
      else if (full) { e.stopPropagation(); setFull(false); }
    }
  }

  function onSearchKey(e: KeyboardEvent<HTMLInputElement>) {
    if (e.key !== "Enter" || !matches.length) return;
    e.preventDefault();
    setCur((c) => (c + (e.shiftKey ? -1 : 1) + matches.length) % matches.length);
  }

  async function copy() {
    const plain = stripAnsi(text);
    try {
      await navigator.clipboard.writeText(plain);
    } catch {
      const ta = document.createElement("textarea");
      ta.value = plain;
      document.body.appendChild(ta);
      ta.select();
      document.execCommand("copy");
      ta.remove();
    }
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
  }

  function pick(n: number, e: React.MouseEvent) {
    // Keep the page where it is; the anchor is shareable through the URL.
    e.preventDefault();
    history.replaceState(history.state, "", `#${anchor(n)}`);
    scrolledTo.current = n;
    setHash(`#${anchor(n)}`);
  }

  return (
    <section ref={frameRef} className={`term${wrap ? " wrap" : ""}${full ? " full" : ""}`} aria-label={label} onKeyDown={onFrameKey}>
      <div className="term-bar">
        <span className="term-dots" aria-hidden="true"><i /><i /><i /></span>
        <span className="term-title" title={title}>{title}</span>
        <span className="term-tools">
          <button type="button" aria-pressed={follow} onClick={() => (follow ? setFollow(false) : jump())}>Follow</button>
          <button type="button" onClick={copy} aria-live="polite">{copied ? "Copied" : "Copy"}</button>
          <button type="button" aria-pressed={wrap} onClick={() => setWrap((w) => !w)}>Wrap</button>
          <button type="button" aria-pressed={full} onClick={() => setFull((f) => !f)} aria-label={full ? "Exit full screen" : "Full screen"}>{full ? "Exit" : "Expand"}</button>
        </span>
      </div>
      {searching && (
        <div className="term-search" role="search">
          <label htmlFor={searchId} className="sr-only">Search output</label>
          <input
            id={searchId} ref={searchRef} value={query} placeholder="Search…" autoComplete="off" spellCheck={false}
            onChange={(e) => { setQuery(e.target.value); setCur(0); }} onKeyDown={onSearchKey}
          />
          <span aria-live="polite" className="nowrap">{q ? (matches.length ? `${Math.min(cur, matches.length - 1) + 1} of ${matches.length}` : "No matches") : "Enter: next, Shift+Enter: previous, Esc: close"}</span>
        </div>
      )}
      <div className="term-body-wrap">
        <div
          ref={bodyRef} className="term-body" tabIndex={0} role="log" aria-label={label} aria-live="off" onScroll={onScroll}
          onClick={(e) => { const a = (e.target as HTMLElement).closest("a.term-ln"); if (a) pick(Number(a.getAttribute("data-line")), e); }}
          style={{ ["--term-h" as string]: typeof height === "number" ? `${height}px` : height }}
        >
          {lines.length === 0 ? (
            <div className="term-empty">{empty}{live && <span className="term-cursor" aria-hidden="true" />}</div>
          ) : (
            lines.map((l, i) => (
              <Line key={i} n={i + 1} line={l} q={q} anchorId={anchor(i + 1)} selected={hashLine === i + 1} current={curLine === i} cursor={live && i === lines.length - 1} />
            ))
          )}
        </div>
        {!atBottom && lines.length > 0 && <button type="button" className="term-jump" onClick={jump}>Jump to latest</button>}
      </div>
    </section>
  );
}
