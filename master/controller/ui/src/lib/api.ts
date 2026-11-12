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
  power?: { on_battery: boolean | null; battery_pct: number | null; min_battery_pct: number; ok: boolean };
  digest?: string;
  models?: { remote_management: boolean; disk_reserve_gb?: number; ram_reserve_gb?: number; max_loaded?: number };
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
  update_available?: boolean;
}

export type AssignmentState = "pending_install" | "active" | "blocked" | "failed";

export interface Assignment {
  role: string;
  model: string;
  node_id: string;
  node: string | null;
  state?: AssignmentState;
  catalog_id?: string | null;
  routed?: boolean | null;
  route_reason?: string | null;
}

export type InstallState =
  | "pending" | "checking" | "pulling" | "configuring" | "verifying" | "ready"
  | "draining" | "removing" | "removed" | "failed" | "blocked" | "cancelled";

export type Fit = "fits" | "tight" | "wont_fit" | "unknown";

export interface ModelParams {
  num_ctx?: number;
  temperature?: number;
  top_p?: number;
  seed?: number;
  think?: boolean;
  keep_alive?: string;
}

export interface NodeModelState {
  installed: boolean;
  desired?: "present" | "absent" | null;
  state?: InstallState | null;
  reason?: string | null;
  progress?: { completed?: number; total?: number; status?: string } | null;
  fit: Fit;
  ownership?: "fleet-managed" | "pre-existing" | null;
  params?: ModelParams | null;
}

export interface CatalogModel {
  id: string;
  reference: string;
  engine: string;
  display_name: string | null;
  download_gb: number | null;
  digest: string | null;
  defaults: ModelParams | null;
  suggested_roles: string[] | null;
  evals: Record<string, number> | null;
  source: string;
  trusted_reviewer?: boolean;
  derived_model?: string;
  derived_trusted_reviewer?: boolean;
  nodes: Record<string, NodeModelState>;
}

export interface Fleet {
  nodes: FleetNode[];
  assignments: Assignment[];
  max_fails: number;
  modes: Mode[];
  roles?: RoleInfo[];
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
  role?: string | null;
  instructions?: string | null;
  policy?: PolicyBody | null;
  policy_hash?: string | null;
  // Lineage (absent on older controllers).
  link?: "retry" | "resume" | "review" | "fix" | "merge" | "check" | null;
  failure_reason?: "limit" | "gate_failed" | "denied" | "error" | "interrupted" | "cancelled" | null;
  resets_at?: number | null;
  resolution?: { kind: "resolved_by" | "dismissed" | "done_elsewhere" | "handed_off" | "handing_off"; task_id?: string; title?: string; commit?: string; by?: string; note?: string; model?: string | null; label?: string | null; at: number } | null;
  outcome?: { kind: "merged" | "done" | "abandoned"; commit?: string; at: number } | null;
  hidden?: boolean;
  display?: { tone: "running" | "ok" | "paused" | "attention" | "resolved" | "muted"; label: string };
  root_id?: string;
  // Account pools and limit handoff (docs/SPEC-limit-handoff.md); absent on older controllers.
  kind?: string | null;
  profile?: string | null;
  account_label?: string | null;
  account?: string | null;
  planned?: { action: "handoff" | "wait" | "left"; label: string; at?: number } | null;
}

export interface UsageRow {
  node_id: string;
  agent: string;
  tasks: number;
  running: number;
  failed: number;
  seconds: number;
}

/** GET /api/usage: the window it covers, and per node and agent counts; `failed` is unsettled failures only. */
export interface UsageResponse {
  window_days: number;
  since: number;
  until: number;
  rows: UsageRow[];
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
export const put = <T>(path: string, body?: unknown) => api<T>(path, "PUT", body);

export type Scope = "fleet" | "node" | "role" | "agent";

export interface PolicyBody {
  filesystem?: { allowed_roots?: string[]; denied?: string[] };
  tools?: { claude_allow?: string[]; claude_deny?: string[] };
  network?: { agents?: "allow" | "deny" };
  mode_ceiling?: Mode;
  limits?: { task_minutes?: number; max_concurrent?: number; agent_minutes_per_day?: number; quiet_hours?: string[] };
  auto_approve?: { agents?: ("claude" | "codex")[]; full_mode?: boolean; denied_tools?: boolean };
  escalation?: { agents?: ("claude" | "codex")[] };
}

export interface Versioned {
  scope: Scope;
  scope_id: string;
  version: number;
  author: string;
  created_at: number;
}

export interface InstructionSet extends Versioned { text: string }
export interface PolicyDoc extends Versioned { body: PolicyBody }

export interface Effective {
  instructions: string;
  policy: PolicyBody;
  sources: Record<string, string | string[]>;
  hash: string;
}

export interface Skill {
  id: string;
  version: number;
  description: string;
  agents: ("claude" | "codex" | "ollama")[];
  risk: "prompt-only" | "tool-aware" | "executable";
  body: string;
  digest: string;
  author: string;
  created_at: number;
  bindings: { scope: Scope | "task"; scope_id: string }[];
}

export interface EvalRun {
  id: string;
  model: string;
  node: string;
  roles: string[];
  state: "queued" | "running" | "done" | "failed";
  started_at: number | null;
  ended_at: number | null;
  scores: Record<string, number> | null;
  errors: string[] | null;
  log_tail?: string | null;
}

export interface ReviewMetrics {
  precision: number | null; recall: number | null; fp_rate: number | null;
  bug_cases?: number; found?: number; clean_cases?: number; alarmed?: number; serious?: number; matched?: number;
  unsupported?: number; identical_fix?: number;
}
export interface SignalDecision {
  decision: "kept" | "dropped"; threshold: number | null; coverage: number | null;
  error_rate: number | null; baseline_error_rate: number | null; train_rows: number; held_out_rows: number; reason: string | null;
}
export interface RoleResult {
  score: number;
  hallucination_rate?: number; correct_abstain_rate?: number; quote_validity?: number;
  signal_decisions?: Record<string, SignalDecision>;
  metrics?: Record<string, ReviewMetrics>;
}

export interface LeaderRow {
  model: string;
  roles: Record<string, RoleResult>;
  size_gb: number | null;
  fits_16gb: boolean | null;
  median_s: number | null;
  tokens_per_s: number | null;
  errors: string[] | null;
}

export interface Recommendation { node: string; role: string; model: string; score: number; installed: boolean }

export type PipelineState = "building" | "reviewing" | "resolving" | "review_waiting" | "ready" | "failed" | "escalated" | "cancelled";

export interface ReviewIssue { severity: "high" | "medium" | "low"; file?: string; problem: string; evidence?: string; fix?: string }

export interface Pipeline {
  id: string;
  title: string;
  state: PipelineState;
  round: number;
  max_rounds: number;
  spec: string;
  repo: string | null;
  gate: string | null;
  mode: Mode;
  builder: { agent: string; node?: string | null };
  reviewers: ({ kind: "role"; role: string } | { kind: "agent"; agent: string; model?: string })[];
  build_task: string | null;
  reviews: Record<string, { status: "valid" | "abstained" | "invalid" | "pending"; verdict?: string; issues?: ReviewIssue[]; unsupported?: number; identical_fix?: number; trusted?: boolean; trusted_by?: string; fallback?: boolean; task_id?: string; reason?: string }>;
  history: { round: number; state: PipelineState; at: number; issues?: number; unsupported?: number; identical_fix?: number; note?: string }[];
  created_at: number;
  updated_at: number;
}

export interface Me { scope: "view" | "operate" | "admin"; label: string; local: boolean }

export interface RoleInfo { name: string; fallback: string | null; description: string | null }

export interface Enrollment { code: string; expires_at: number; command: string }

export interface NotifyRule { id: string; event_glob: string; plugin: string; enabled: boolean }

export type CallOutcome = "answered" | "abstained" | "escalated" | "check_failed";
export interface CallStat {
  role: string; model: string; count: number; error_rate: number; p50_ms: number; p95_ms: number; fallbacks: number;
  grounded: number; outcomes: Record<CallOutcome, number>;
  abstain_rate: number | null; escalation_rate: number | null; check_failed_rate: number | null; logprob_mean: number | null;
}
