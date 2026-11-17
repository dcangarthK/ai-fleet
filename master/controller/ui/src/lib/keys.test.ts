import { describe, expect, it } from "vitest";
import { createSequencer, isHelpKey, isPaletteHotkey, isTypingTarget } from "./keys";

describe("keys", () => {
  it("detects the palette hotkey", () => {
    expect(isPaletteHotkey({ key: "k", metaKey: true })).toBe(true);
    expect(isPaletteHotkey({ key: "K", ctrlKey: true })).toBe(true);
    expect(isPaletteHotkey({ key: "k" })).toBe(false);
    expect(isPaletteHotkey({ key: "k", ctrlKey: true, altKey: true })).toBe(false);
  });

  it("detects help", () => {
    expect(isHelpKey({ key: "?", shiftKey: true })).toBe(true);
    expect(isHelpKey({ key: "?", ctrlKey: true })).toBe(false);
  });

  it("recognises typing targets", () => {
    expect(isTypingTarget({ tagName: "INPUT" })).toBe(true);
    expect(isTypingTarget({ tagName: "textarea" })).toBe(true);
    expect(isTypingTarget({ tagName: "DIV", isContentEditable: true })).toBe(true);
    expect(isTypingTarget({ tagName: "BUTTON" })).toBe(false);
    expect(isTypingTarget(null)).toBe(false);
  });

  it("completes g sequences", () => {
    const s = createSequencer({ n: "/", w: "/work", b: "/budget" });
    expect(s.feed({ key: "g" })).toBeNull();
    expect(s.feed({ key: "w" })).toBe("/work");
    expect(s.feed({ key: "w" })).toBeNull(); // not armed any more
    s.feed({ key: "g" });
    expect(s.feed({ key: "b" })).toBe("/budget");
  });

  it("drops unknown second keys and expired prefixes", () => {
    let t = 0;
    const s = createSequencer({ n: "/" }, 1000, () => t);
    s.feed({ key: "g" });
    expect(s.feed({ key: "z" })).toBeNull();
    expect(s.feed({ key: "n" })).toBeNull();
    s.feed({ key: "g" });
    t = 5000;
    expect(s.feed({ key: "n" })).toBeNull();
  });

  it("ignores modified keys", () => {
    const s = createSequencer({ n: "/" });
    s.feed({ key: "g" });
    expect(s.feed({ key: "n", metaKey: true })).toBeNull();
    expect(s.feed({ key: "n" })).toBeNull();
  });
});
