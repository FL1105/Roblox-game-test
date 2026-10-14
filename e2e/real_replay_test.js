// Browser test for the real-data replay build. Usage: node e2e/real_replay_test.js <dir containing evil.html>  (see tests for how evil.html is built)
// Needs: npm i playwright-core, and dist/launch-sniper.html built with --events.
const { chromium } = require("playwright-core");
(async () => {
  const b = await chromium.launch({ executablePath: "/opt/pw-browsers/chromium-1194/chrome-linux/chrome", args: ["--no-sandbox"] });
  const check = (n, ok, x = "") => { console.log((ok ? "PASS " : "FAIL ") + n + (x ? "  " + x : "")); if (!ok) process.exitCode = 1; };
  // 1. hostile names
  const p1 = await b.newPage(); const errs = []; p1.on("pageerror", e => errs.push(String(e)));
  await p1.goto("file://" + process.argv[2] + "/evil.html"); await p1.waitForSelector(".trow:not(.thead)", { timeout: 10000 });
  check("hostile name shown as plain text", (await p1.textContent(".trow:not(.thead) .tok b")) === "</script><img src=x onerror=window.__xss=1><b>bold</b>");
  check("no injected elements", (await p1.$$("img[src=x], .trow b b")).length === 0);
  check("script never ran", (await p1.evaluate(() => window.__xss)) === undefined);
  check("hostile label shown as text in the source picker", (await p1.textContent("#sourcesel option")).includes("</script>"));
  check("no page errors (hostile page)", errs.length === 0, errs.join("|"));
  // 2. real replay page
  const p2 = await b.newPage({ viewport: { width: 1400, height: 1000 } }); const errs2 = []; p2.on("pageerror", e => errs2.push(String(e)));
  await p2.goto("file:///home/user/Roblox-game-test/dist/launch-sniper.html"); await p2.waitForSelector(".trow:not(.thead)", { timeout: 10000 });
  check("starts on real replay", (await p2.textContent("#modetext")).includes("REPLAY"));
  check("real-data note visible, simulated banner hidden", (await p2.isVisible("#realnote")) && !(await p2.isVisible("#simnote")));
  check("source picker offers real + simulated", (await p2.$$eval("#sourcesel option", o => o.map(x => x.value))).join() === "real,demo");
  await p2.click("#start"); await p2.waitForTimeout(25000);
  const s = await p2.evaluate(() => window.__sniperLocal.state.snapshot("all"));
  console.log("after 25s of real replay at 20x:", "launches", s.counters.launches, "| trades", s.stats.trades, "| open", s.paper.open, "| feed", s.feed_status, "| clock", new Date(s.clock).toISOString());
  check("real launches flowed through", s.counters.launches > 10);
  await p2.screenshot({ path: process.argv[2] + "/real_replay.png" });
  await p2.selectOption("#sourcesel", "demo"); await p2.waitForTimeout(1500);
  check("switching to the simulated market works and shows its banner", (await p2.isVisible("#simnote")) && !(await p2.isVisible("#realnote")));
  check("bot stays running across the switch", (await p2.evaluate(() => window.__sniperLocal.state.running)) === true);
  await p2.selectOption("#sourcesel", "real"); await p2.waitForTimeout(1200);
  check("switching back restarts the real replay", (await p2.evaluate(() => window.__sniperLocal.state.snapshot("all").counters.launches)) < s.counters.launches);
  check("no page errors (real page)", errs2.length === 0, errs2.join("|"));
  await b.close();
})();
