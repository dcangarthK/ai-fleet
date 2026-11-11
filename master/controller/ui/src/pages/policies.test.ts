import { describe, expect, it } from "vitest";
import { compact } from "./Policies";

describe("compact", () => {
  it("drops empty fields so a blank form means no opinion", () => {
    expect(compact({})).toEqual({});
    expect(compact({ filesystem: { allowed_roots: [], denied: [] }, tools: { claude_allow: [] }, limits: { quiet_hours: [] } })).toEqual({});
    expect(compact({ limits: { task_minutes: undefined, max_concurrent: NaN } })).toEqual({});
  });
  it("keeps what was set", () => {
    expect(compact({ filesystem: { allowed_roots: ["~/code"], denied: [] }, network: { agents: "deny" }, limits: { task_minutes: 30 } }))
      .toEqual({ filesystem: { allowed_roots: ["~/code"] }, network: { agents: "deny" }, limits: { task_minutes: 30 } });
  });
});
