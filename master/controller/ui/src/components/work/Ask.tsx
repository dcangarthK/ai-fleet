import { useState } from "react";
import { post } from "../../lib/api";
import { ASK_SUGGESTIONS, splitCitations, type AskResponse } from "../../lib/work";
import { Button, Input } from "../ui";
import { ErrorBanner } from "../bits";

/** Jump the terminal to a line: it listens to hashchange for #L123 anchors. */
export function jumpToLine(line: number) {
  location.hash = `L${line}`;
  window.dispatchEvent(new HashChangeEvent("hashchange"));
  document.getElementById(`L${line}`)?.scrollIntoView({ block: "center" });
}

/** Ask about this work item. Answers come from the local `fast` role only; [L123] cites are buttons. */
export function Ask({ root }: { root: string }) {
  const [q, setQ] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [res, setRes] = useState<AskResponse | null>(null);

  async function ask(question: string) {
    if (!question.trim()) return;
    setBusy(true); setError(null);
    try { setRes(await post<AskResponse>("/api/ask", { root_id: root, question })); }
    catch (e) { setError(e instanceof Error ? e.message : String(e)); }
    finally { setBusy(false); }
  }

  return (
    <div className="stack" style={{ gap: 8 }}>
      <ErrorBanner error={error} onDismiss={() => setError(null)} />
      <form className="row" style={{ gap: 8 }} onSubmit={(e) => { e.preventDefault(); void ask(q); }}>
        <Input aria-label="Ask about this work" placeholder="Ask about this work" value={q} onChange={(e) => setQ(e.target.value)} />
        <Button type="submit" variant="primary" disabled={busy || !q.trim()}>{busy ? "Asking…" : "Ask"}</Button>
      </form>
      <div className="row" style={{ gap: 8, flexWrap: "wrap" }}>
        {ASK_SUGGESTIONS.map((s) => <Button key={s} size="sm" disabled={busy} onClick={() => { setQ(s); void ask(s); }}>{s}</Button>)}
      </div>
      {res && (
        <div role="status">
          <p style={{ whiteSpace: "pre-wrap" }}>
            {splitCitations(res.answer).map((p, i) => p.kind === "text" ? <span key={i}>{p.text}</span>
              : <button key={i} type="button" className="mono" aria-label={`Jump to log line ${p.line}`} onClick={() => jumpToLine(p.line)}>L{p.line}</button>)}
          </p>
          <p className="muted">Answered by {res.model}{res.local ? " (local, no paid model)" : ""}. Verify against the log.</p>
        </div>
      )}
    </div>
  );
}
