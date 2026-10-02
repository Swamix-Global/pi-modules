// Test-only instrumentation of the actual Chart.js canvas, installed before the SPA.
// Fixed server time and a non-UTC browser timezone catch clock/offset mistakes.
const chartDates = {
  now: Date.parse("2026-10-01T18:25:00Z"), // October 1, 23:55 in Asia/Kolkata
  texts: [],
  reads: 0,
};

const fillTextOriginal = CanvasRenderingContext2D.prototype.fillText;
CanvasRenderingContext2D.prototype.fillText = function (text, ...args) {
  chartDates.texts.push(String(text));
  if (chartDates.texts.length > 3000) chartDates.texts.shift();
  return fillTextOriginal.call(this, text, ...args);
};

const fetchOriginal = window.fetch;
window.fetch = async (...args) => {
  const response = await fetchOriginal(...args);
  if (args[0] !== "/stats") return response;
  const stats = await response.json();
  stats.now = chartDates.now;
  stats.timeline = [{
    time: chartDates.now,
    requests: 3,
    before_tokens: 8000,
    after_tokens: 2000,
  }];
  chartDates.reads++;
  return new Response(JSON.stringify(stats), {
    status: response.status,
    headers: { "Content-Type": "application/json" },
  });
};

chartDates.check = async () => {
  const assert = (ok, message) => {
    if (!ok) throw Error(message);
  };
  const wait = async (test) => {
    const deadline = performance.now() + 10000;
    while (!test()) {
      if (performance.now() > deadline) throw Error("Chart condition timed out");
      await new Promise((resolve) => setTimeout(resolve, 25));
    }
  };
  const frame = () => new Promise((resolve) =>
    requestAnimationFrame(() => requestAnimationFrame(resolve)));
  const formatTime = (ms) => new Date(ms).toLocaleString(undefined, {
    year: "numeric", month: "numeric", day: "numeric",
    hour: "numeric", minute: "2-digit", second: "2-digit",
  });
  const checkTooltip = async (expected) => {
    const canvas = document.querySelector("canvas");
    const rect = canvas.getBoundingClientRect();
    // Clear the active tooltip so the check always observes a fresh draw.
    canvas.dispatchEvent(new MouseEvent("mouseout", { bubbles: true }));
    await frame();
    chartDates.texts = [];
    // The last bucket lies on the plot boundary; use the preceding bucket.
    for (let offset = 0; offset < 100; offset++) {
      canvas.dispatchEvent(new MouseEvent("mousemove", {
        bubbles: true,
        clientX: rect.right - offset,
        clientY: rect.top + 80,
      }));
      await frame();
      if (chartDates.texts.includes(expected)) return;
    }
    const titles = [...new Set(chartDates.texts.filter((text) => text.includes("/2026")))];
    throw Error(`Tooltip timestamp missing: ${expected}; got: ${titles.join(" | ")}`);
  };

  await wait(() => chartDates.texts.includes("Oct 1"));
  assert(!chartDates.texts.includes("Oct 2"), "Chart used browser time instead of server time");
  await checkTooltip(formatTime(chartDates.now - 300000));
  const reads = chartDates.reads;
  chartDates.now += 3 * 60 * 60 * 1000;
  await wait(() => chartDates.reads > reads && chartDates.texts.includes("Oct 2"));
  await checkTooltip(formatTime(chartDates.now - 300000));
  return "localized dates and refreshed tooltips across midnight";
};
