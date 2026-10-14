// Browser test for the "Show N tokens" control on the dashboard.
// Run:   npm install playwright-core   &&   node e2e/row_limit_test.js
// Env:   TERMINAL_URL (default: file://<repo>/dist/launch-sniper.html), CHROME_PATH (a Chromium/Chrome binary; optional)
const path = require("path");
const { chromium } = require("playwright-core");
const URL = process.env.TERMINAL_URL || "file://" + path.resolve(__dirname, "..", "dist", "launch-sniper.html");
const check = (name, ok, extra = "") => { console.log((ok ? "PASS " : "FAIL ") + name + (extra ? "  " + extra : "")); if (!ok) process.exitCode = 1; };
(async () => {
  const browser = await chromium.launch({ executablePath: process.env.CHROME_PATH || undefined, args: ["--no-sandbox"] });
  const ctx = await browser.newContext({ viewport: { width: 1400, height: 1000 } });
  const page = await ctx.newPage();
  const errors = []; page.on("pageerror", e => errors.push(String(e)));
  await page.goto(URL); await page.waitForSelector(".trow:not(.thead)", { timeout: 15000 });
  const count = () => page.$$eval("#rows .trow", n => n.length);
  const label = () => page.textContent("#rowcount");
  check("selector offers 10/25/50/100", (await page.$$eval("#rowlimit option", o => o.map(x => x.value))).join() === "10,25,50,100");
  check("default is 25", await page.inputValue("#rowlimit") === "25");
  await page.click("#start"); // the demo/replay feed needs to be running to produce launches fast
  await page.waitForFunction(() => document.querySelector("#rowcount").textContent.includes("of"), null, { timeout: 60000 });
  let n = await count(); check("never more rows than the limit (25)", n <= 25 + 3 && n >= 20, `${n} rows; "${await label()}"`);
  await page.selectOption("#rowlimit", "10"); await page.waitForTimeout(1500);
  n = await count(); const m = (await label()).match(/\+ (\d+) older still open/); const extra = m ? +m[1] : 0;
  check("limit 10 applied (plus any older open positions, which the label states)", n === 10 + extra, `${n} rows; "${await label()}"`);
  await page.selectOption("#rowlimit", "100"); await page.waitForTimeout(1500);
  n = await count(); check("limit 100 shows more", n > 25 && n <= 103, `${n} rows`);
  await page.selectOption("#rowlimit", "10");
  await page.reload(); await page.waitForSelector(".trow:not(.thead)", { timeout: 15000 });
  check("choice remembered after reload", await page.inputValue("#rowlimit") === "10");
  check("no page errors", errors.length === 0, errors.join("|"));
  await browser.close();
})();
