// Retakes docs/screenshots/*.png from a running demo server (python3 scripts/demo/demo_server.py).
// Usage: NODE_PATH=<dir>/node_modules node scripts/demo/take_screenshots.js [http://127.0.0.1:7790]
// (see scripts/demo/README.md: playwright@1.49.1 is installed into a temp folder, never into the repo)
const path = require("path");
const { chromium } = require("playwright");

const BASE = process.argv[2] || "http://127.0.0.1:7790";
const OUT = path.join(__dirname, "..", "..", "docs", "screenshots") + path.sep;

(async () => {
  const browser = await chromium.launch();
  const ctx = await browser.newContext({ viewport: { width: 1600, height: 1000 }, colorScheme: "dark", reducedMotion: "reduce" });
  const page = await ctx.newPage();
  const go = async (hash, wait = 2500) => { await page.goto(BASE + "/" + hash); await page.reload(); await page.waitForTimeout(wait); };
  const shot = (name, opts = {}) => page.screenshot({ path: OUT + name, ...opts });

  await go("#/", 3000);                 await shot("01-command-center.png");
  await go("#/agents");                 await shot("02-agents.png");
  await go("#/workflows/demo");         await shot("03-workflows.png");
  await go("#/board");                  await shot("04-board.png");
  await go("#/maps", 3000);             await shot("05-project-maps.png");
  await go("#/review");                 await shot("06-review-inbox.png");
  await go("#/analytics");              await shot("07-analytics.png");
  await go("#/settings");               await shot("08-settings.png");

  // the command bar: the /orchestrate preview, then the /help answer
  await go("#/");
  const box = page.locator("textarea").first();
  await box.click();
  await box.fill("/orchestrate Add discount codes to the shop, update the checkout tests and write the migration notes");
  await page.waitForTimeout(1500);
  await shot("09-command-bar-orchestrate.png", { clip: { x: 204, y: 0, width: 1396, height: 520 } });
  await box.fill("/help");
  await page.getByRole("button", { name: /Launch/ }).click();   // /help only lists commands, nothing runs
  await page.waitForTimeout(800);
  await box.click();
  await page.waitForTimeout(500);
  await shot("10-slash-help.png");

  await go("#/knowledge");              await shot("11-knowledge.png");
  await page.setViewportSize({ width: 1280, height: 800 });
  await go("#/", 3000);                 await shot("12-command-center-1280x800.png");
  await go("#/review");                 await shot("13-review-1280x800.png");
  await browser.close();
})();
