import { test, expect } from "bun:test";
import { readFileSync } from "node:fs";
import { runInNewContext } from "node:vm";

// Lightweight DOM check; does not replace a real browser layout inspection.
test("context visual renders and switches all seven scenarios", () => {
  const html = readFileSync(new URL("../context-compression-visual.html", import.meta.url), "utf8");
  const elements = new Map<string, any>();
  const create = () => ({
    children: [] as any[],
    style: {},
    attributes: {} as Record<string, string>,
    textContent: "",
    append(child: any) { this.children.push(child); },
    replaceChildren() { this.children = []; },
    setAttribute(key: string, value: string) { this.attributes[key] = value; },
    addEventListener(_event: string, listener: () => void) { this.click = listener; },
    click() {},
  });
  for (const match of html.matchAll(/id="([^"]+)"/g)) elements.set(match[1], create());
  const document = {
    getElementById: (id: string) => elements.get(id),
    querySelector: (selector: string) => elements.get(selector.slice(1)),
    querySelectorAll: () => elements.get("tabs").children,
    createElement: create,
  };
  runInNewContext(html.match(/<script>([\s\S]*?)<\/script>/)![1], { document, Date });
  const buttons = elements.get("tabs").children;
  expect(buttons).toHaveLength(7);
  const outcomes = ["PASS", "COMPACT", "CACHE HIT", "WARN + FORWARD", "DROP BRANCH", "HTTP 502", "NORMALIZE"];
  buttons.forEach((button: any, index: number) => {
    button.click();
    expect(buttons.filter((b: any) => b.attributes["aria-pressed"] === "true")).toEqual([button]);
    expect(elements.get("result").textContent.startsWith(outcomes[index])).toBe(true);
    expect(elements.get("before").children.length).toBeGreaterThan(0);
    expect(elements.get("after").children.length === 0).toBe(index === 5);
  });
  const color = (name: string) => html.match(new RegExp(`--${name}:(#[0-9a-f]+)`))![1];
  expect(color("text")).not.toBe(color("bg"));
  expect(color("text")).not.toBe(color("panel"));
});
