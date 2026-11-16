import { describe, expect, it } from "vitest";
import { ApiError } from "../../lib/api";
import { healthSentence, healthTone, isConflict, leaderLag, leaderState, planHealth, textToSteps, validateItem, type LeaderRecord, type PlanItem } from "./model";

const NOW = 100_000;
const item = (o: Partial<PlanItem>): PlanItem => ({ work_item: "w", next_steps: ["x"], status: "open", updated_at: NOW, revision: 1, ...o });
const leader = (o: Partial<LeaderRecord>): LeaderRecord => ({ account: "claude/fleet", model: "claude-sonnet-5-5", since: 1, session_task_id: "t-1", not_before: null, plan_revision: 3, halted: false, ...o });

describe("planHealth", () => {
  it("counts blocked, step-less, stale and waiting items", () => {
    const h = planHealth([
      item({ work_item: "a", status: "blocked" }),
      item({ work_item: "b", next_steps: [] }),
      item({ work_item: "c", updated_at: NOW - 3 * 3600 }),
      item({ work_item: "d", status: "pending_review" }),
      item({ work_item: "e", status: "done", updated_at: 0 }),
    ], NOW);
    expect(h).toEqual({ blocked: ["a"], noSteps: ["b"], stale: ["c"], pendingReview: ["d"] });
    expect(healthTone(h)).toBe("bad");
    expect(healthSentence(h)).toBe("1 blocked item, 1 open item with no next step, 1 item unchanged for over 2h, 1 item waiting for review.");
  });
  it("is quiet when all is well", () => {
    const h = planHealth([item({})], NOW);
    expect(healthTone(h)).toBe("ok");
    expect(healthSentence(h)).toBe("Nothing blocked, stale or waiting.");
  });
});

describe("leaderState", () => {
  it("covers no leader, halted, waiting and leading", () => {
    expect(leaderState(null, NOW).label).toBe("No leader yet");
    expect(leaderState(leader({ halted: true }), NOW).tone).toBe("bad");
    expect(leaderState(leader({ not_before: NOW + 60 }), NOW).label).toMatch(/limit/);
    expect(leaderState(leader({}), NOW)).toEqual({ tone: "ok", label: "Leading" });
  });
  it("reports how many plan revisions the leader has not seen", () => {
    expect(leaderLag(leader({ plan_revision: 3 }), { revision: 5, items: [] })).toBe(2);
    expect(leaderLag(leader({ plan_revision: 9 }), { revision: 5, items: [] })).toBe(0);
  });
});

describe("editing", () => {
  it("parses steps and validates like the controller", () => {
    expect(textToSteps(" a \n\n b ")).toEqual(["a", "b"]);
    expect(validateItem("", [], "open")).toMatch(/Work item/);
    expect(validateItem("w", [], "nope")).toMatch(/status/);
    expect(validateItem("w", Array(51).fill("s"), "open")).toMatch(/50/);
    expect(validateItem("w", ["s"], "done")).toBeNull();
  });
  it("recognises a revision conflict", () => {
    expect(isConflict(new ApiError(409, "plan item changed"))).toBe(true);
    expect(isConflict(new ApiError(400, "x"))).toBe(false);
    expect(isConflict(new Error("x"))).toBe(false);
  });
});
