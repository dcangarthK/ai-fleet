import { describe, expect, it } from "vitest";
import { canMarkSignedIn, plannedText, slotName, slotPill, type ResourceSlot } from "./handoff";
import { timelineSteps, type Attempt } from "./lineage";

const slot = (over: Partial<ResourceSlot> = {}): ResourceSlot => ({
  node: "master", node_name: "master", agent: "claude", profile: "fleet", label: "owner-max",
  model: "claude:claude-opus-*", state: "free", running: 0, max: 1, until: null, kinds: ["build"], today_tokens: 0,
  signed_out: false, ...over,
});

describe("resource board", () => {
  it("shows each slot state as a pill", () => {
    expect(slotPill(slot())).toEqual({ tone: "ok", label: "Free" });
    expect(slotPill(slot({ state: "busy", running: 1, max: 1 }))).toEqual({ tone: "warn", label: "Busy 1/1" });
    expect(slotPill(slot({ state: "busy", running: 1, max: null }))).toEqual({ tone: "warn", label: "Busy 1" });
    const until = new Date(2026, 10, 11, 19, 30).getTime() / 1000;
    expect(slotPill(slot({ state: "limited", until }))).toEqual({ tone: "bad", label: "Limited until 7:30 PM" });
    expect(slotPill(slot({ state: "at_budget" }))).toEqual({ tone: "bad", label: "At budget" });
    expect(slotPill(slot({ state: "offline" }))).toEqual({ tone: "quiet", label: "Offline" });
  });
  it("shows a profile whose directory is missing as not signed in", () => {
    expect(slotPill(slot({ state: "not_signed_in" }))).toEqual({ tone: "bad", label: "Not signed in" });
  });
  it("offers the owner a way back only for a slot flagged after a sign-in failure", () => {
    expect(canMarkSignedIn(slot({ state: "not_signed_in", signed_out: true }))).toBe(true);
    expect(canMarkSignedIn(slot({ state: "not_signed_in", signed_out: false }))).toBe(false); // its directory is missing
    expect(canMarkSignedIn(slot())).toBe(false);
  });
  it("names a slot by its label, never a directory", () => {
    expect(slotName(slot())).toBe("Opus (owner-max)");
    expect(slotName(slot({ label: null, profile: "spare", model: "claude:claude-sonnet-5-5" }))).toBe("claude-sonnet-5-5 (spare)");
  });
});

describe("planned action", () => {
  it("says what happens to a limited task", () => {
    expect(plannedText({ action: "handoff", label: "Will hand off to Opus 5.5 (spare)" })).toBe("Will hand off to Opus 5.5 (spare)");
    expect(plannedText({ action: "wait", label: "Waiting for reset at 7:30 PM (resume scheduled)" }))
      .toBe("Waiting for reset at 7:30 PM (resume scheduled)");
    expect(plannedText({ action: "left", label: "Left for you: still limited" })).toBe("Left for you: still limited");
    expect(plannedText(null)).toBeNull();
  });
});

describe("handoff timeline", () => {
  const attempt = (over: Partial<Attempt>): Attempt => ({
    id: "t", title: "t", agent: "claude", model: "Opus 5.5", link: null, state: "review",
    display: { tone: "ok", label: "gate passed" }, failure_reason: null, started_at: null, ended_at: null, ...over,
  });
  it("reads as one story across accounts", () => {
    const steps = timelineSteps([
      attempt({ id: "a", link: null, kind: "build", account_label: "owner-max", state: "failed", failure_reason: "limit",
        display: { tone: "resolved", label: "Handed off → t-2" }, handed_off: true }),
      attempt({ id: "b", link: "retry", kind: "build", account_label: "spare", handoff: true }),
    ]);
    expect(steps.join(" → ")).toBe("Opus 5.5 (owner-max) build · paused: limit → handed off to Opus 5.5 (spare) · gate passed");
  });
});
