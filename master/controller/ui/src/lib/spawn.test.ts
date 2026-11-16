import { describe, expect, it } from "vitest";
import { followUpBody, followUpError } from "./spawn";

describe("spawn", () => {
  it("builds a body with link and parent", () => {
    const b = followUpBody({ rootId: "r", parentId: "t-1", kind: "fix", agent: "claude", prompt: " do it ", repo: "/x" });
    expect(b).toMatchObject({ agent: "claude", prompt: "do it", parent_id: "t-1", kind: "fix", link: "fix", repo: "/x" });
  });
  it("re_review links as review", () => {
    expect(followUpBody({ rootId: "r", parentId: "t", kind: "re_review", agent: "codex", prompt: "p" }).link).toBe("review");
  });
  it("validates", () => {
    expect(followUpError({ prompt: " ", agent: "a", parentId: "t" })).toMatch(/follow-up/);
    expect(followUpError({ prompt: "x", agent: "a", parentId: "t" })).toBeNull();
  });
});
