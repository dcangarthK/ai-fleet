import { describe, expect, it } from "vitest";
import {
  LIVE_POLL_MS, accountTone, alertText, fill, liveQueryOptions, machineWarnings, presenceText, queueText, rowsClass,
  stageChip, stageTitle, workerText, type Account, type Machine, type QueueRow, type Session, type Worker,
} from "./live";

const NOW = new Date(2026, 10, 11, 21, 0).getTime() / 1000;
const at = (h: number, m: number) => new Date(2026, 10, 11, h, m).getTime() / 1000;

const running = (over: Partial<Worker> = {}): Worker => ({
  task_id: "t-1", agent: "claude", node: "master", profile: "fleet", account_label: "owner-max", title: "Greeting build",
  model: "claude-opus-5-5", kind: "build", effort: "medium", stage: "build-1", branch: "fleet/t-1", started_at: NOW - 120,
  elapsed_s: 120, independence: null, ...over,
});
const idle = (over: Partial<Worker> = {}): Worker => ({
  task_id: null, agent: "codex", node: "master", node_name: "master", profile: "fleet", account_label: "owner-plus",
  idle_since: NOW - 300, limited_until: null, signed_in: true, offline: null, ...over,
});

describe("worker rows", () => {
  it("a running row: title, account and model, kind · effort · stage", () => {
    expect(workerText(running(), NOW)).toEqual({
      primary: "Greeting build", secondary: "owner-max · opus 5.5", meta: "build · medium · build-1", tone: "info",
      flag: undefined,
    });
    expect(workerText(running({ independence: "waived" }), NOW).flag).toBe("independence waived");
    expect(workerText(running({ agent: "ollama", model: "qwen3:8b", account_label: null, profile: null }), NOW).secondary)
      .toBe("local · ollama qwen3:8b");
  });
  it("idle rows per state", () => {
    expect(workerText(idle(), NOW)).toMatchObject({ primary: "codex (owner-plus)", meta: "idle since 5m ago", tone: "ok" });
    expect(workerText(idle({ limited_until: at(23, 30) }), NOW)).toMatchObject({ meta: "limited until 11:30 PM", tone: "bad" });
    expect(workerText(idle({ signed_in: false }), NOW)).toMatchObject({ meta: "not signed in", tone: "bad" });
    expect(workerText(idle({ offline: "runner offline" }), NOW)).toMatchObject({ meta: "offline", tone: "quiet" });
    expect(workerText(idle({ idle_since: null }), NOW).meta).toBe("idle");
  });
});

describe("presence", () => {
  const s: Session = { session: "orch-1", model: "claude-opus-5-5", account_label: "spare", last_seen: NOW - 120,
    stale: false, waiting_on: ["t-c0bbd59e"], last_wait: null };
  it("says who, where and what it waits on", () => {
    expect(presenceText(s, NOW)).toBe("Orchestrator (opus 5.5, configured; spare), last seen 2m ago, waiting on t-c0bbd59e");
    expect(presenceText({ ...s, waiting_on: null, last_wait: { ids: ["t-1"], ago_s: 90 } }, NOW))
      .toBe("Orchestrator (opus 5.5, configured; spare), last seen 2m ago, last waited on t-1 2m ago");
  });
});

describe("queue", () => {
  const q = (over: Partial<QueueRow>): QueueRow => ({ task_id: "t-2", title: "x", agent: "claude", model: null,
    account_label: null, kind: "build", stage: null, node: "master", wait: "slot", reason: "waiting for a Claude Opus slot",
    until: null, queued_s: 10, ...over });
  it("names each reason", () => {
    expect(queueText(q({}))).toBe("waiting for a Claude Opus slot");  // the server's reason, said once
    expect(queueText(q({ wait: "limit", until: at(23, 30) }))).toBe("limit until 11:30 PM");
    expect(queueText(q({ wait: "approval", reason: null }))).toBe("needs your approval");
    expect(queueText(q({ wait: "node", reason: "quiet hours" }))).toBe("node can't take it yet: quiet hours");
    expect(queueText(q({ reason: null }))).toBe("waiting for a slot");
  });
});

describe("stage strips", () => {
  it("highlights current, reds failed, counts repeats", () => {
    expect(stageChip({ stage: "build", state: "done" })).toEqual({ className: "stage stage-done", label: "build" });
    expect(stageChip({ stage: "test-1", state: "current", count: 2 })).toEqual({ className: "stage stage-current", label: "test-1 ×2" });
    expect(stageChip({ stage: "review", state: "failed", count: 1 }).className).toBe("stage stage-failed");
    expect(stageChip({ stage: "merge", state: "next", count: 0 }).label).toBe("merge");
    expect(stageChip({ stage: "review", state: "superseded" }).className).toBe("stage stage-superseded");
  });
  it("colours a stage by the controller's tone, so a settled attempt is never red", () => {
    const settled = { stage: "build", state: "failed" as const,
      display: { tone: "resolved" as const, label: "Done elsewhere · ec2ab20" } };
    expect(stageChip(settled).className).toBe("stage stage-tone-resolved");
    expect(stageChip({ ...settled, display: { tone: "muted", label: "Cancelled" } }).className).toBe("stage stage-tone-muted");
    expect(stageChip({ ...settled, display: { tone: "attention", label: "Failed: error" } }).className).toBe("stage stage-tone-attention");
    expect(stageTitle(settled)).toBe("build: Done elsewhere · ec2ab20");
    expect(stageTitle({ stage: "next", state: "next" })).toBe("next: next");
  });
});

describe("machines and accounts", () => {
  const m = (over: Partial<Machine> = {}): Machine => ({ node: "master", name: "master", age_s: 5, unknown: false,
    mem_total_gb: 48, mem_used_gb: 20, swap_used_gb: 0.5, disk_free_gb: 200, load5: 2, cores: 12, ollama_up: true,
    loaded: ["qwen3:8b"], warm: ["qwen3:8b"], warnings: [], offline: null, heartbeat: "ok", ...over });
  it("shows warnings, or unknown for stale data, or offline", () => {
    expect(machineWarnings(m())).toEqual([]);
    expect(machineWarnings(m({ warnings: ["swap", "disk"] })).map((w) => w.label)).toEqual(["Swap high", "Disk low"]);
    expect(machineWarnings(m({ unknown: true, age_s: 90, warnings: ["swap"] }))).toEqual([{ tone: "quiet", label: "Unknown (data 2m old)" }]);
    expect(machineWarnings(m({ offline: "runner offline" }))[0].label).toBe("Offline");
    expect(machineWarnings(m({ heartbeat: "lost, polling" }))).toEqual([{ tone: "warn", label: "Heartbeat lost, polling" }]);
    expect(fill(20, 48)).toBeCloseTo(20 / 48);
    expect(fill(null, 48)).toBeNull();
  });
  it("tones an account by its status", () => {
    const a = (status: string) => ({ status } as Account);
    expect(accountTone(a("limited until 7:30 PM"))).toBe("bad");
    expect(accountTone(a("limit hit less than 5 h ago (reset passed)"))).toBe("warn");
    expect(accountTone(a("reset by owner at 8:10 PM"))).toBe("info");
    expect(accountTone(a("no recent limit"))).toBe("ok");
  });
  it("alert text never needs more than the title or node", () => {
    expect(alertText({ id: 1, ts: NOW, task_id: null, type: "node.swap_high", title: null, node: "master", state: "started" }))
      .toBe("Swap high: master");
    expect(alertText({ id: 2, ts: NOW, task_id: "t-1", type: "budget.limit", title: "Build", node: null, state: null }))
      .toBe("Usage limit: Build");
  });
});

describe("refresh and layout", () => {
  it("polls every 20 s while visible and pauses when hidden", () => {
    expect(liveQueryOptions(true)).toEqual({ enabled: true, refetchInterval: LIVE_POLL_MS });
    expect(LIVE_POLL_MS).toBe(20_000);
    expect(liveQueryOptions(false)).toEqual({ enabled: false, refetchInterval: false });
  });
  it("collapses rows to cards at phone width", () => {
    expect(rowsClass(true)).toBe("live-rows live-cards");
    expect(rowsClass(false)).toBe("live-rows");
  });
});
