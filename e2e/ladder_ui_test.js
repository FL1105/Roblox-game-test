// Browser test for the take-profit ladder editor on the Trading tab.
// Run:   npm install playwright-core   &&   node e2e/ladder_ui_test.js
// Env:   TERMINAL_URL (default: file://<repo>/dist/launch-sniper.html), CHROME_PATH (a Chromium/Chrome binary; optional)
// For a server instance (http://...) it CHANGES that instance's settings, so use a throwaway one.
const path = require("path");
const { chromium } = require("playwright-core");
const URL = process.env.TERMINAL_URL || "file://" + path.resolve(__dirname, "..", "dist", "launch-sniper.html");
let page;
const state = async () => URL.startsWith("file:") ? page.evaluate(() => window.__sniperLocal.state.snapshot("24h")) : (await fetch(URL + "/api/state")).json();
const check = (name, ok, extra = "") => { console.log((ok ? "PASS " : "FAIL ") + name + (extra ? "  " + extra : "")); if (!ok) process.exitCode = 1; };
const rung = (n, what) => `[aria-label='Ladder rung ${n} ${what}']`;
const openTrading = async () => { await page.click("#b-cfg"); await page.waitForSelector("#cfg.open"); await page.click("#cfgtabs button[data-pane=trading]"); };
const save = async () => { await page.click("#cfgsave"); };

(async () => {
  const browser = await chromium.launch({ executablePath: process.env.CHROME_PATH || undefined, args: ["--no-sandbox"] });
  page = await browser.newPage({ viewport: { width: 1400, height: 1100 } });
  const errors = [];
  page.on("pageerror", e => errors.push(String(e)));
  page.on("console", m => { if (m.type() === "error" && !/status of 400/.test(m.text())) errors.push(m.text()); }); // the 400 is the deliberate bad-input check
  await page.goto(URL); await page.waitForSelector(".trow:not(.thead)", { timeout: 15000 });

  // 1. fill two rungs, save, read back
  await openTrading();
  await page.fill(rung(1, "gain (%)"), "10"); await page.fill(rung(1, "share to sell (%)"), "50");
  await page.fill(rung(2, "gain (%)"), "25"); await page.fill(rung(2, "share to sell (%)"), "30");
  await save(); await page.waitForSelector("#cfg.open", { state: "detached" });
  let s = (await state()).settings;
  check("two rungs saved", JSON.stringify(s.ladder) === JSON.stringify([{ gain_pct: 10, sell_pct: 50 }, { gain_pct: 25, sell_pct: 30 }]), JSON.stringify(s.ladder));

  // 2. reopen: boxes show what was saved, empty rung 3 stays empty
  await openTrading();
  const shown = await Promise.all([rung(1, "gain (%)"), rung(1, "share to sell (%)"), rung(2, "gain (%)"), rung(2, "share to sell (%)"), rung(3, "gain (%)")].map(sel => page.inputValue(sel)));
  check("reopened boxes show saved ladder", shown.join() === "10,50,25,30,", shown.join("|"));

  // 3. gains that don't increase are refused with an error toast, and the old ladder stays
  await page.fill(rung(3, "gain (%)"), "20"); await page.fill(rung(3, "share to sell (%)"), "10");
  await page.waitForSelector("#toast", { state: "hidden" }); // the "saved" message from step 1 must be gone, or we'd read that one
  await save(); await page.waitForSelector("#toast", { state: "visible" });
  const toast = await page.textContent("#toast");
  check("decreasing gains refused with a message", /ladder|rung|increas/i.test(toast), toast);
  check("config stays open after refusal", await page.$("#cfg.open") !== null);
  s = (await state()).settings;
  check("old ladder untouched after refusal", s.ladder.length === 2 && s.ladder[1].gain_pct === 25, JSON.stringify(s.ladder));

  // 4. clearing every box turns the ladder off
  for (const sel of [rung(1, "gain (%)"), rung(1, "share to sell (%)"), rung(2, "gain (%)"), rung(2, "share to sell (%)"), rung(3, "gain (%)"), rung(3, "share to sell (%)")]) await page.fill(sel, "");
  await save(); await page.waitForSelector("#cfg.open", { state: "detached" });
  s = (await state()).settings;
  check("empty boxes clear the ladder", Array.isArray(s.ladder) && s.ladder.length === 0, JSON.stringify(s.ladder));

  // 5. the Ladder preset loads rungs into the boxes
  await page.click("#b-cfg"); await page.waitForSelector("#cfg.open");
  await page.click(".pc:has-text('Ladder')");
  await page.click("#cfgtabs button[data-pane=trading]");
  const g1 = await page.inputValue(rung(1, "gain (%)"));
  check("Ladder preset fills rung 1", g1 !== "", g1);
  await save(); await page.waitForSelector("#cfg.open", { state: "detached" });
  s = (await state()).settings;
  check("Ladder preset saved", s.ladder.length >= 2, JSON.stringify(s.ladder));

  check("no page errors", errors.length === 0, errors.join("|"));
  await browser.close();
})();
