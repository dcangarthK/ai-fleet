/** Exact-request text for confirm dialogs: "POST /path  body: {...}". Display only; never executes. */
export function describeRequest(method: string, path: string, body?: unknown): string {
  if (body === undefined) return `${method} ${path}`;
  let s = JSON.stringify(body);
  if (s.length > 300) s = `${s.slice(0, 300)}…`;
  return `${method} ${path}  body: ${s}`;
}
