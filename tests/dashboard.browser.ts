import assert from "node:assert/strict";
import { chromium } from "playwright-core";

// Run after `bun run build`; CHROME_BIN points to an installed Chrome/Chromium.
if (!process.env.CHROME_BIN)
  throw new Error("Set CHROME_BIN to a Chrome/Chromium executable");
const upstream = Bun.serve({
  hostname: "127.0.0.1",
  port: 0,
  fetch: () =>
    Response.json({
      choices: [
        { message: { content: "Useful summary SUMMARY_END_SENTINEL" } },
      ],
    }),
});
Object.assign(process.env, {
  PI_CONTEXT_ROUTER_DB: ":memory:",
  PI_CONTEXT_ROUTER_API_KEY: "test",
  PI_CONTEXT_ROUTER_CONDENSER_URL: upstream.url.toString(),
  PI_CONTEXT_ROUTER_TOOL_OUTPUT_TOKENS: "100",
});
const { app, db } = await import("../main.ts");
app.listen({ hostname: "127.0.0.1", port: 0 });
const base = `http://127.0.0.1:${app.server!.port}`;
const insert = db.query(
  "INSERT INTO history(created_at,route,model,status,changed,before_tokens,after_tokens,duration_ms,error,original,compressed,changes) VALUES(?,?,?,200,0,20,20,10,NULL,?,?,'[]')",
);
const payload = JSON.stringify({
  messages: [{ role: "user", content: "hello" }],
});
for (let i = 0; i < 55; i++)
  insert.run(
    Date.now(),
    "POST /compress",
    `browser-history-${i}`,
    payload,
    payload,
  );
const original = {
  model: "browser-inspection",
  messages: [
    { role: "user", content: "read" },
    {
      role: "tool",
      tool_call_id: "call-exact",
      content: "Full original content ".repeat(100) + "ORIGINAL_END_SENTINEL",
    },
  ],
};
const compression = await fetch(`${base}/compress`, {
  method: "POST",
  body: JSON.stringify(original),
});
assert.equal(compression.status, 200);
const browser = await chromium.launch({
  executablePath: process.env.CHROME_BIN,
  headless: true,
  args: ["--no-sandbox"],
});
try {
  const page = await browser.newPage({
    viewport: { width: 1440, height: 1000 },
  });
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto(`${base}/dashboard/`);
  await page
    .getByRole("heading", { name: "Lifetime history (persisted)" })
    .waitFor();
  assert.equal(await page.locator(".cards").count(), 2);
  const contrast = await page.locator(".app-title").evaluate((el) => ({
    foreground: getComputedStyle(el).color,
    background: getComputedStyle(document.body).backgroundColor,
  }));
  assert.notEqual(contrast.foreground, contrast.background);
  await page.getByRole("tab", { name: "Overview", exact: true }).focus();
  await page.keyboard.press("ArrowRight");
  assert.equal(
    await page
      .getByRole("tab", { name: "History", exact: true })
      .getAttribute("aria-selected"),
    "true",
  );
  await page.getByRole("tab", { name: "History", exact: true }).click();
  await page
    .getByText("Showing 50 of 56 matching records.", { exact: false })
    .waitFor();
  await page.getByRole("button", { name: "Load older" }).click();
  await page
    .getByText("Showing 56 of 56 matching records.", { exact: false })
    .waitFor();
  await page
    .getByRole("row", { name: "Inspect history entry 56", exact: true })
    .click();
  const dialog = page.getByRole("dialog");
  await dialog
    .getByRole("heading", { name: "Unified diff (complete)" })
    .waitFor();
  const diff = dialog.getByRole("table", { name: "Unified diff" });
  assert((await diff.textContent())!.includes("ORIGINAL_END_SENTINEL"));
  assert((await diff.textContent())!.includes("SUMMARY_END_SENTINEL"));
  assert((await diff.textContent())!.includes("call-exact"));
  assert((await diff.locator(".diff-add").count()) > 0);
  assert((await diff.locator(".diff-del").count()) > 0);
  assert.equal(await dialog.locator(".change-card").count(), 1);
  await dialog.getByLabel("Wrap long lines").check();
  const downloadEvent = page.waitForEvent("download");
  await dialog.getByRole("button", { name: "Download full JSON" }).click();
  assert.equal((await downloadEvent).suggestedFilename(), "history-56.json");
  await page.keyboard.press("Escape");
  assert.equal(await page.getByRole("dialog").count(), 0);
  for (let i = 0; i < 60; i++)
    insert.run(Date.now(), "POST /compress", `burst-${i}`, payload, payload);
  await page
    .getByText("Showing 116 of 116 matching records.", { exact: false })
    .waitFor();
  assert.equal(await page.locator("#panel-history tbody tr").count(), 116);
  await page.getByRole("tab", { name: "Settings", exact: true }).click();
  const threshold = page.getByLabel("Tool output token budget", {
    exact: false,
  });
  await threshold.fill("101");
  await page.getByRole("button", { name: "Save settings" }).click();
  await page.getByText("Settings saved to SQLite.", { exact: true }).waitFor();
  const settings = await (await fetch(`${base}/api/settings`)).json();
  assert.equal(settings.settings.toolTokens, 101);
  assert.equal(
    JSON.parse(
      (
        db.query("SELECT value FROM dashboard_settings").get() as {
          value: string;
        }
      ).value,
    ).toolTokens,
    101,
  );
  await page.getByRole("button", { name: "Reset to .env defaults" }).click();
  assert.equal(await threshold.inputValue(), "100");
  await page.setViewportSize({ width: 390, height: 844 });
  assert(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  );
  assert.deepEqual(errors, []);
  await page.getByRole("tab", { name: "History", exact: true }).click();
  assert(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  );
  if (process.env.SCREENSHOT_PATH)
    await page.screenshot({
      path: process.env.SCREENSHOT_PATH,
      fullPage: false,
    });
  console.log(
    "Dashboard browser check: metrics, full history, full diff, download, settings, mobile layout passed",
  );
} finally {
  await browser.close();
  await app.stop(true);
  await upstream.stop(true);
  db.close();
}
