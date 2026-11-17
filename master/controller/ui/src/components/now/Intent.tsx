import { useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { useSearchParams } from "react-router-dom";
import { Button, Input, useConfirm } from "../ui";
import { api, type Fleet, type Task } from "../../lib/api";
import type { BudgetStatus } from "../../lib/budget";
import { parseIntent, type Intent, type IntentCtx } from "../../lib/intent";
import { Kv, RequestPreview } from "./Request";

export const INTENT_PARAM = "intent";

/** Parser context from the query cache: what the console already loaded. */
export function intentCtx(get: <T>(key: unknown[]) => T | undefined): IntentCtx {
  const tasks = get<Task[]>(["tasks"]) ?? [];
  const fleet = get<Fleet>(["fleet"]);
  return {
    tasks, nodes: (fleet?.nodes ?? []).map((n) => ({ id: n.id, name: n.name })),
    budget: get<BudgetStatus>(["budget", "status"]) ?? null,
  };
}

/** Confirm, then send, one parsed intent. Returns an error string or null. Nothing runs without the confirmation. */
export function useRunIntent() {
  const confirm = useConfirm();
  const qc = useQueryClient();
  return async function runIntent(intent: Intent): Promise<string | null> {
    if (!intent.request) return intent.warnings[0] ?? "That can't be run.";
    const ok = await confirm({
      title: intent.summary,
      message: (
        <RequestPreview
          request={intent.request} fields={intent.fields}
          note={intent.warnings.length ? <span className="warns">{intent.warnings.join(" ")}</span> : "This is exactly what will be sent. Nothing runs until you confirm."}
        />
      ),
      confirmLabel: "Send request", danger: intent.kind === "drain" || intent.request.path.endsWith("/cancel") || intent.request.path.endsWith("/reject"),
    });
    if (!ok) return null;
    try {
      await api(intent.request.method, intent.request.path, intent.request.body);
      return null;
    } catch (e) {
      return e instanceof Error ? e.message : String(e);
    } finally {
      qc.invalidateQueries();
    }
  };
}

/** The palette provider navigates here with `?intent=<text>`; this opens the confirmation and clears the param. */
export function IntentHost({ onError }: { onError: (msg: string | null) => void }) {
  const [params, setParams] = useSearchParams();
  const qc = useQueryClient();
  const runIntent = useRunIntent();
  const handled = useRef<string | null>(null);
  const text = params.get(INTENT_PARAM);

  useEffect(() => {
    if (!text || handled.current === text) return;
    handled.current = text;
    const next = new URLSearchParams(params);
    next.delete(INTENT_PARAM);
    setParams(next, { replace: true });
    const intent = parseIntent(text, intentCtx((k) => qc.getQueryData(k)));
    if (!intent) { onError(`“${text}” isn't a command I know. Press ⌘K to search instead.`); handled.current = null; return; }
    runIntent(intent).then((err) => { onError(err); handled.current = null; });
  }, [text, params, setParams, qc, runIntent, onError]);
  return null;
}

/** Inline intent bar: type a command, see the exact request, confirm to send. */
export function IntentBar({ ctx, onError }: { ctx: IntentCtx; onError: (msg: string | null) => void }) {
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const runIntent = useRunIntent();
  const intent = useMemo(() => parseIntent(text, ctx), [text, ctx]);

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (!intent) return;
    setBusy(true);
    const err = await runIntent(intent);
    setBusy(false);
    onError(err);
    if (!err) setText("");
  }

  return (
    <form className="intent" onSubmit={submit} aria-label="Intent bar">
      <div className="intent-row">
        <Input
          aria-label="Type a command" value={text} onChange={(e) => setText(e.target.value)} autoComplete="off" spellCheck={false}
          placeholder="review t-3f9a12c0 with fable · drain laptop · spawn build: …" aria-describedby="intent-help"
        />
        <Button type="submit" variant="primary" disabled={busy || !intent?.request}>Preview and send</Button>
      </div>
      <span id="intent-help" className="quiet-note">Commands: review, final review, security review, merge, message, approve, reject, cancel, drain, spawn. Anything else: press ⌘K to search.</span>
      {text.trim() && !intent && <span className="quiet-note" role="status">Not a command, so nothing to run. Press ⌘K to search.</span>}
      {intent && (
        <div className={`intent-preview${intent.warnings.length || !intent.request ? " warn" : ""}`} role="status">
          <strong>{intent.summary}</strong>
          <Kv fields={intent.fields} />
          {intent.warnings.length > 0 && <ul className="warns">{intent.warnings.map((w) => <li key={w}>{w}</li>)}</ul>}
        </div>
      )}
    </form>
  );
}
