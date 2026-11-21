// The dashboard with the 8 fake agents running (used by `panel_demo.py shots`; see scripts/demo/README.md).
// Usage: NODE_PATH=<dir>/node_modules node scripts/demo/take_panel_dashboard.js [url] [out.png]
// Also prints every console error, so a Content-Security-Policy problem ("Refused to ...") shows up.
const path = require("path");
const { chromium } = require("playwright");

const BASE = process.argv[2] || "http://127.0.0.1:7790";
const OUT = process.argv[3] || path.join(__dirname, "..", "..", "docs", "screenshots", "15-dashboard-8-agents.png");

(async () => {
  const browser = await chromium.launch();
  const ctx = await browser.newContext({ viewport: { width: 1600, height: 1000 }, colorScheme: "dark", reducedMotion: "reduce" });
  const page = await ctx.newPage();
  const problems = [];
  page.on("console", (m) => { if (m.type() === "error") problems.push(m.text()); });
  page.on("pageerror", (e) => problems.push("pageerror: " + e.message));
  await page.goto(BASE + "/#/");
  await page.reload();
  await page.waitForTimeout(3500);
  await page.screenshot({ path: OUT });
  console.log("saved " + OUT);
  console.log(problems.length ? "console errors:\n  " + problems.join("\n  ") : "no console errors (so no CSP violations)");
  await browser.close();
})();
