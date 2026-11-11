// Thin client for the controller API. Every call sends X-Fleet (the controller's CSRF check);
// auth itself rides on the session cookie set by /login?token=…, exactly like the old dashboard.

export type Mode = "read-only" | "supervised" | "auto-edit" | "full";

export type TaskState =
  | "queued" | "awaiting_approval" | "starting" | "working" | "blocked"
  | "review" | "failed" | "interrupted" | "done" | "cancelled";

export interface RunnerHealth {
  ok: boolean;
  version: string;
  max_mode: Mode;
  running: number;
  agents: Record<string, string | null>;
  gate_profiles?: string[];
  allow_raw_gates?: boolean;
  allowed_roots?: string[];
  max_jobs?: number;
}

export interface InventoryServer {
  engine: string;
  port: number;
  reachable_from_master: boolean;
  hint: string;
  models: { name: string; size_gb?: number; params?: string; quant?: string }[];
}

export interface Inventory {
  scanned_at?: number;
  hardware?: { os?: string; arch?: string; cpu?: string; gpu?: string; ram_gb?: number; hostname?: string };
  servers?: InventoryServer[];
  agents?: { cli: string; version?: string }[];
}

export interface FleetNode {
  id: string;
  name: string;
  ip: string;
  os: string;
  engine: string | null;
  port: number | null;
  status: "pending" | "approved" | "drained";
  models: string[];
  healthy: boolean;
  runner_ok: boolean;
  runner: RunnerHealth | null;
  inventory: Inventory | null;
  ceiling: Mode | null;
  effective_ceiling: Mode | null;
  last_seen: number | null;
  fails: number;
}

export interface Assignment {
  role: string;
  model: string;
  node_id: string;
  node: string | null;
}

export interface Fleet {
  nodes: FleetNode[];
  assignments: Assignment[];
  max_fails: number;
  modes: Mode[];
}

export interface GateResult {
  cmd?: string;
  profile?: string;
  state: "pending" | "running" | "passed" | "failed" | "skipped";
  exit_code: number | null;
  output_tail: string;
}

export interface Task {
  id: string;
  parent_id: string | null;
  title: string;
  agent: "claude" | "codex" | "ollama";
  model: string | null;
  prompt: string;
  mode: Mode;
  repo: string | null;
  gate: string | null;
  node_id: string;
  state: TaskState;
  created_at: number;
  updated_at: number;
  started_at: number | null;
  ended_at: number | null;
  session_id: string | null;
  result: string | null;
  denials: { tool_name?: string; tool?: string; input?: unknown; tool_input?: unknown }[];
  gate_result: GateResult | null;
  exit_code: number | null;
  branch: string | null;
  cwd: string | null;
  runs: number;
  error: string | null;
  agent_seconds: number;
}

export interface UsageRow {
  node_id: string;
  agent: string;
  tasks: number;
  running: number;
  failed: number;
  seconds: number;
}

export interface FleetEvent {
  id: number;
  ts: number;
  task_id: string | null;
  type: string;
  data: Record<string, unknown> | null;
}

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

export function errorText(detail: unknown, status: number): string {
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    // FastAPI validation errors: [{loc, msg}]
    return detail.map((d) => (d && typeof d === "object" && "msg" in d ? String(d.msg) : JSON.stringify(d))).join("; ");
  }
  if (detail) return JSON.stringify(detail);
  return status === 401 ? "Sign in again: open /login?token=… with a fleet token." : `Request failed (HTTP ${status})`;
}

export async function api<T>(path: string, method = "GET", body?: unknown): Promise<T> {
  const r = await fetch(path, {
    method,
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-Fleet": "1" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new ApiError(r.status, errorText((data as { detail?: unknown }).detail, r.status));
  return data as T;
}

export const get = <T>(path: string) => api<T>(path);
export const post = <T>(path: string, body?: unknown) => api<T>(path, "POST", body);
export const del = <T>(path: string, body?: unknown) => api<T>(path, "DELETE", body);
