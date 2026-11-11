import type { ReactNode } from "react";
import type { FleetNode, TaskState } from "../lib/api";
import { nodeSignal, SIGNAL_LABEL, STATE } from "../lib/format";
import { pillText, toneClass, type Display } from "../lib/lineage";

export function StateLabel({ state }: { state: TaskState }) {
  const s = STATE[state] ?? { tone: "quiet", label: state };
  return <span className={`state tone-${s.tone}`}>{s.label}</span>;
}

/** The honest status of a task or work item, as the controller computed it. */
export function StatusPill({ display }: { display: Display }) {
  return <span className={`state ${toneClass(display.tone)}${display.tone === "resolved" ? " resolved" : ""}`}>{pillText(display)}</span>;
}

export function NodeLight({ node }: { node: FleetNode }) {
  const signal = nodeSignal(node);
  return <span className={`light ${signal}`} role="img" aria-label={SIGNAL_LABEL[signal]} title={SIGNAL_LABEL[signal]} />;
}

export function PageHead({ title, intro, actions }: { title: string; intro?: ReactNode; actions?: ReactNode }) {
  return (
    <header className="page-head">
      <div>
        <h1>{title}</h1>
        {intro && <p>{intro}</p>}
      </div>
      {actions && <div className="row">{actions}</div>}
    </header>
  );
}

export function Section({ title, aside, children }: { title: string; aside?: ReactNode; children: ReactNode }) {
  return (
    <section className="section">
      <h2>
        {title}
        {aside && <span className="quiet">{aside}</span>}
      </h2>
      {children}
    </section>
  );
}

export function ErrorBanner({ error, onDismiss }: { error: string | null | undefined; onDismiss?: () => void }) {
  if (!error) return null;
  return (
    <div className="banner error row" role="alert" style={{ justifyContent: "space-between" }}>
      <span>{error}</span>
      {onDismiss && (
        <button type="button" className="ghost" onClick={onDismiss} aria-label="Dismiss error">
          Dismiss
        </button>
      )}
    </div>
  );
}

export function LoadError({ error }: { error: unknown }) {
  if (!error) return null;
  return <ErrorBanner error={error instanceof Error ? error.message : String(error)} />;
}

export function Tags({ items, max = 8 }: { items: string[]; max?: number }) {
  const shown = items.slice(0, max);
  return (
    <>
      {shown.map((m) => (
        <span className="tag" key={m}>
          {m}
        </span>
      ))}
      {items.length > max && <span className="quiet small">and {items.length - max} more</span>}
    </>
  );
}

/** Colour unified-diff lines; never renders HTML from the diff. */
export function Diff({ text }: { text: string }) {
  return (
    <pre className="out diff">
      {text.split("\n").map((line, i) => {
        const cls = line.startsWith("@@") ? "hunk" : line.startsWith("+") && !line.startsWith("+++") ? "add"
          : line.startsWith("-") && !line.startsWith("---") ? "rem" : undefined;
        return (
          <span key={i} className={cls}>
            {line}
            {"\n"}
          </span>
        );
      })}
    </pre>
  );
}
