// End-to-end click test for the terminal's configuration screen.
// Run:   python -m sniper.terminal --no-browser --port 8765 &      (demo feed is fine)
//        npm install playwright-core   &&   node e2e/terminal_click_test.js
// Env:   TERMINAL_URL (default http://127.0.0.1:8765), CHROME_PATH (a Chromium/Chrome binary; optional)
// It saves presets/fields through the real UI and reads the settings back from the API, so it CHANGES the
// running terminal's settings and resets its paper bankroll to 5 SOL. Use a throwaway instance.
const { chromium } = require("playwright-core");
const URL = process.env.TERMINAL_URL || "http://127.0.0.1:8765";
const state = async () => (await fetch(URL + "/api/state")).json();
const check = (name, ok, extra = "") => { console.log((ok ? "PASS " : "FAIL ") + name + (extra ? "  " + extra : "")); if (!ok) process.exitCode = 1; };
(async () => {
  const browser = await chromium.launch({ executablePath: process.env.CHROME_PATH || undefined, args: ["--no-sandbox"] });
  const page = await browser.newPage({ viewport: { width: 1400, height: 1000 } });
  const errors = []; page.on("pageerror", e => errors.push(String(e))); page.on("console", m => { if (m.type() === "error" && !/status of 400/.test(m.text())) errors.push(m.text()); }); // the 400s are the deliberate invalid-input checks
  await page.goto(URL); await page.waitForSelector(".trow:not(.thead)", { timeout: 15000 });

  // 1. open config, load the Safe preset, save
  await page.click("#b-cfg"); await page.waitForSelector("#cfg.open");
  await page.click(".pc:has-text('Safe')");
  await page.click("#cfgsave"); await page.waitForSelector("#cfg.open", { state: "detached" });
  let s = (await state()).settings;
  check("Safe preset reached the engine", s.take_profit === 35 && s.stop_loss === 15 && s.slippage_pct === 5 && JSON.stringify(s.verdicts) === '["PASS"]', JSON.stringify({tp: s.take_profit, sl: s.stop_loss, v: s.verdicts}));

  // 2. edit single fields, toggle, select; check they round-trip
  await page.click("#b-cfg"); await page.waitForSelector("#cfg.open");
  await page.fill("[aria-label='Trailing Stop (%)']", "12");
  await page.fill("[aria-label='Min Liquidity (SOL)']", "2.5");
  await page.selectOption("[aria-label='Position Size Mode']", "random");
  await page.check("[aria-label='Auto-compound']"); await page.uncheck("[aria-label='Use wallet blacklist']");
  await page.click("#cfgtabs button[data-pane=safety]");
  await page.selectOption("[aria-label='Max Hold Time']", "1800"); await page.selectOption("[aria-label='Trading Speed']", "2000"); await page.uncheck("[aria-label='Rug Pull Protection']");
  await page.click("#cfgtabs button[data-pane=advanced]"); await page.selectOption("[aria-label='Risk Tolerance']", "high");
  await page.click("#cfgsave"); await page.waitForSelector("#cfg.open", { state: "detached" });
  s = (await state()).settings;
  check("edited fields saved", s.trailing_stop_pct === 12 && s.min_liquidity_sol === 2.5 && s.size_mode === "random" && s.auto_compound === true && s.use_blacklist === false, JSON.stringify({t: s.trailing_stop_pct, m: s.min_liquidity_sol, mode: s.size_mode}));
  check("safety tab saved", s.max_hold_s === 1800 && s.latency_ms === 2000 && s.exit_on_creator_sell === false && s.verdicts.length === 3, JSON.stringify({h: s.max_hold_s, l: s.latency_ms, r: s.exit_on_creator_sell, v: s.verdicts}));

  // 3. reopening shows the saved values (not defaults)
  await page.click("#b-cfg"); await page.waitForSelector("#cfg.open");
  check("reopen shows saved trailing stop", (await page.inputValue("[aria-label='Trailing Stop (%)']")) === "12");
  check("reopen shows saved risk tolerance", (await page.inputValue("[aria-label='Risk Tolerance']")) === "high");

  // 4. invalid input: server rejects, modal stays open, nothing changes
  await page.click("#cfgtabs button[data-pane=trading]");
  await page.fill("[aria-label='Stop Loss (%)']", "500");
  await page.click("#cfgsave"); await page.waitForSelector("#toast:has-text('Stop loss')");  // selector wait: the page CSP forbids eval, which waitForFunction needs
  check("invalid value keeps modal open + shows error", (await page.isVisible("#cfg.open")) && /Stop loss/.test(await page.textContent("#toast")));
  check("invalid value changed nothing", (await state()).settings.stop_loss === 15);

  // 5. blacklist: bad address rejected, good one saved
  await page.fill("[aria-label='Stop Loss (%)']", "15");
  await page.click("#cfgtabs button[data-pane=trading]");
  const listBefore = JSON.stringify((await state()).settings.blacklist);
  await page.fill("[aria-label='Blacklisted creator wallets']", "not-a-wallet");
  await page.click("#cfgsave"); await page.waitForTimeout(400);
  check("bad wallet rejected", (await page.isVisible("#cfg.open")) && JSON.stringify((await state()).settings.blacklist) === listBefore);
  const good = "7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU";
  await page.fill("[aria-label='Blacklisted creator wallets']", good + "\n" + good);
  await page.click("#cfgsave"); await page.waitForSelector("#cfg.open", { state: "detached" });
  check("good wallet saved once (deduped)", JSON.stringify((await state()).settings.blacklist) === JSON.stringify([good]));

  // 6. cancel / escape discard edits
  await page.click("#b-cfg"); await page.waitForSelector("#cfg.open");
  await page.fill("[aria-label='Take Profit (%)']", "99"); await page.keyboard.press("Escape");
  check("Escape discards edits", (await state()).settings.take_profit === 35);

  // 7. left-panel quick fields apply live
  await page.fill("#take_profit", "42"); await page.press("#take_profit", "Tab"); await page.waitForTimeout(900);
  check("quick field applied live", (await state()).settings.take_profit === 42);

  // 8. start/stop + bankroll reset
  await page.click("#start"); await page.waitForTimeout(500);
  check("start button runs the bot", (await state()).running === true);
  await page.click("#stop"); await page.waitForTimeout(500);
  check("stop button stops it", (await state()).running === false);
  await page.click("#nav button[data-view=profile]"); await page.fill("#bankroll", "5");
  page.once("dialog", d => d.accept()); await page.click("#reset"); await page.waitForTimeout(800);
  const st = await state();
  check("bankroll reset to 5 SOL", st.paper.start === 5 && st.paper.equity === 5, JSON.stringify(st.paper));
  check("no page errors", errors.length === 0, errors.join(" | ").slice(0, 300));
  await browser.close();
})().catch(e => { console.error("CRASH", e); process.exit(2); });
