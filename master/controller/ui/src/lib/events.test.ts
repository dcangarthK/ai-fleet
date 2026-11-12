import { describe, expect, it } from "vitest";
import type { FleetEvent } from "./api";
import { EVENTS_URL, newestFirst } from "./data";

const ev = (id: number): FleetEvent => ({ id, ts: id, task_id: null, type: "t", data: null });

describe("events feed", () => {
  it("asks for the newest events, without after", () => {
    expect(EVENTS_URL).toContain("/api/events/list");
    expect(EVENTS_URL).not.toContain("after");
    expect(EVENTS_URL).toContain("limit=1000");
  });
  it("orders newest first whatever order the server used", () => {
    expect(newestFirst([ev(3), ev(2), ev(1)]).map((e) => e.id)).toEqual([3, 2, 1]);
    expect(newestFirst([ev(1), ev(2), ev(3)]).map((e) => e.id)).toEqual([3, 2, 1]);
    expect(newestFirst(undefined)).toEqual([]);
  });
  it("does not mutate its input", () => {
    const input = [ev(1), ev(2)];
    newestFirst(input);
    expect(input.map((e) => e.id)).toEqual([1, 2]);
  });
});
