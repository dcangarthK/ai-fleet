import { describe, expect, it } from "vitest";
import { applySgr, parseAnsi, parseAnsiLine, stripAnsi, toneRgb } from "./ansi";

const E = String.fromCharCode(27);

describe("ansi", () => {
  it("strips escape sequences", () => {
    expect(stripAnsi(`${E}[31mred${E}[0m ok`)).toBe("red ok");
    expect(stripAnsi(`${E}[2K${E}[1Gdone`)).toBe("done");
    expect(stripAnsi(`${E}]0;title\x07text`)).toBe("text");
  });

  it("maps basic colours to status tones", () => {
    const { spans } = parseAnsiLine(`${E}[31mfail${E}[0m ${E}[32mpass${E}[33m warn`);
    expect(spans.map((s) => [s.text, s.fg])).toEqual([["fail", "bad"], [" ", undefined], ["pass", "ok"], [" warn", "warn"]]);
  });

  it("handles bold, bright, reset and combined params", () => {
    const { spans } = parseAnsiLine(`${E}[1;91mX${E}[22mY${E}[0mZ`);
    expect(spans[0]).toMatchObject({ text: "X", bold: true, fg: "bad" });
    expect(spans[1]).toMatchObject({ text: "Y", bold: false, fg: "bad" });
    expect(spans[2]).toEqual({ text: "Z" });
  });

  it("treats an empty parameter list as reset", () => {
    expect(applySgr({ fg: "ok", bold: true }, "")).toEqual({});
  });

  it("maps 256 and truecolor to tones", () => {
    expect(applySgr({}, "38;5;1").fg).toBe("bad");
    expect(applySgr({}, "38;5;10").fg).toBe("ok");
    expect(applySgr({}, "38;2;255;0;0").fg).toBe("bad");
    expect(applySgr({}, "48;2;0;200;0").bg).toBe("ok");
    expect(toneRgb(10, 10, 10)).toBe("ink-3");
    expect(toneRgb(0, 120, 255)).toBe("accent");
  });

  it("carries style across lines and drops the trailing empty line", () => {
    const lines = parseAnsi(`${E}[31mone\ntwo${E}[0m\nthree\n`);
    expect(lines).toHaveLength(3);
    expect(lines[1].spans[0].fg).toBe("bad");
    expect(lines[2].spans[0].fg).toBeUndefined();
    expect(lines.map((l) => l.plain)).toEqual(["one", "two", "three"]);
  });

  it("keeps only what follows a carriage return", () => {
    expect(parseAnsi("10%\r50%\r100%")[0].plain).toBe("100%");
    expect(parseAnsi("line\r")[0].plain).toBe("line");
  });

  it("ignores unknown codes and malformed input without throwing", () => {
    expect(parseAnsi(`${E}[999mhi${E}[`)[0].plain).toBe("hi");
  });
});
