// Browser test for the exit-strategy / entry-mode controls, with and without an embedded model.
// Usage: node e2e/policy_ui_test.js <dir with no_model.html and with_model.html>  (build them with tools/build_standalone.py --events ... [--model ...])
const { chromium } = require("playwright-core");
(async () => {
  const b = await chromium.launch({ executablePath: "/opt/pw-browsers/chromium-1194/chrome-linux/chrome", args: ["--no-sandbox"] });
  const check = (n, ok, x = "") => { console.log((ok ? "PASS " : "FAIL ") + n + (x ? "  " + x : "")); if (!ok) process.exitCode = 1; };
  for (const [file, hasModel] of [["no_model.html", false], ["with_model.html", true]]) {
    const page = await b.newPage({ viewport: { width: 1300, height: 1100 } }); const errs = []; page.on("pageerror", e => errs.push(String(e)));
    await page.goto("file://" + process.argv[2] + "/" + file); await page.waitForSelector(".trow:not(.thead)", { timeout: 10000 });
    await page.click("#b-cfg"); await page.waitForSelector("#cfg.open");
    const exitOpts = await page.$$eval("[aria-label='Exit Strategy'] option", o => o.map(x => x.value));
    await page.click("#cfgtabs button[data-pane=safety]");
    const exitOpts2 = await page.$$eval("[aria-label='Exit Strategy'] option", o => o.map(x => x.value));
    check(`[${file}] exit options ${exitOpts2.join("/")}`, hasModel ? exitOpts2.join() === "targets,smart,model" : exitOpts2.join() === "targets,smart");
    await page.selectOption("[aria-label='Exit Strategy']", "smart");
    await page.click("#cfgtabs button[data-pane=trading]");
    await page.selectOption("[aria-label='Entry Mode']", "confirm");
    await page.fill("[aria-label='Confirm: Min Gain (%)']", "12"); await page.fill("[aria-label='Confirm: Net Buying (SOL)']", "0.3"); await page.fill("[aria-label='Confirm: Window (s)']", "45");
    await page.click("#cfgsave"); await page.waitForSelector("#cfg.open", { state: "detached" });
    let st = await page.evaluate(() => window.__sniperLocal.state.snapshot("24h").settings);
    check(`[${file}] smart + confirm saved`, st.exit_mode === "smart" && st.entry_mode === "confirm" && st.confirm_gain_pct === 12 && st.confirm_min_flow_sol === 0.3 && st.confirm_window_s === 45, JSON.stringify([st.exit_mode, st.entry_mode, st.confirm_gain_pct]));
    if (hasModel) {
      await page.click("#b-cfg"); await page.waitForSelector("#cfg.open"); await page.click("#cfgtabs button[data-pane=safety]");
      await page.selectOption("[aria-label='Exit Strategy']", "model"); await page.click("#cfgsave"); await page.waitForSelector("#cfg.open", { state: "detached" });
      st = await page.evaluate(() => window.__sniperLocal.state.snapshot("24h").settings);
      check("[with_model] learned exit selectable and saved", st.exit_mode === "model");
      await page.click("#start"); await page.waitForTimeout(20000);
      const s = await page.evaluate(() => window.__sniperLocal.state.snapshot("all"));
      console.log("   after 20s with learned exit + confirm entry:", "launches", s.counters.launches, "trades", s.stats.trades, "open", s.paper.open, "skips", JSON.stringify(s.counters.skipped));
      const statuses = await page.$$eval(".trow:not(.thead) .chip", n => [...new Set(n.map(x => x.textContent.split(" · ")[0]))]);
      console.log("   statuses on screen:", JSON.stringify(statuses));
      check("confirming status appears for watched tokens", statuses.includes("CONFIRMING") || statuses.includes("HOLDING") || statuses.some(x => x.startsWith("SKIPPED")));
      await page.screenshot({ path: process.argv[2] + "/policy_ui.png" });
    }
    check(`[${file}] no page errors`, errs.length === 0, errs.join("|"));
    await page.close();
  }
  await b.close();
})();
