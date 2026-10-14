// Browser test for the Auto-tune screen: master switch, settings, "what it may change", live round, past rounds, errors.
// Run:   npm install playwright-core   &&   node e2e/tuner_ui_test.js
// Env:   TERMINAL_URL (default http://127.0.0.1:8791, a server started with `python3 -m sniper.terminal --no-browser --port 8791`),
//        CHROME_PATH (a Chromium/Chrome binary; optional), TUNER_SHOTS (a directory: if set, screenshots are saved there).
// It CHANGES that instance's bot and tuner settings and leaves the bot stopped and the tuner off, so use a throwaway one.
// Note: the server's Content-Security-Policy forbids eval, so page.waitForFunction cannot be used; we poll with evaluate instead.
const path = require("path");
const { chromium } = require("playwright-core");
const URL = process.env.TERMINAL_URL || "http://127.0.0.1:8791";
const SHOTS = process.env.TUNER_SHOTS || "";
const SW = "[aria-label='Auto-tune switch']", CLONES = "[aria-label='Tuner clones']", ROUND = "[aria-label='Tuner launches per round']",
  CAUTION = "[aria-label='Tuner caution']", STEP = "[aria-label='Tuner step size']", TUNE = (label) => `[aria-label='Tune ${label}']`;
const check = (name, ok, extra = "") => { console.log((ok ? "PASS " : "FAIL ") + name + (extra ? "  " + extra : "")); if (!ok) process.exitCode = 1; };
const sleep = (ms) => new Promise(r => setTimeout(r, ms));
const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);
let page;

// the page's own fetch, so this works against the Python server and (same origin) against the browser build's fake /api
const state = () => page.evaluate(async () => (await fetch("/api/state?range=all&rows=10", { cache: "no-store" })).json());
const tuner = async () => (await state()).tuner;
const send = (p, body) => page.evaluate(async ([p, body]) => { const r = await fetch(p, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }); return { code: r.status, body: await r.json() }; }, [p, body]);
async function until(fn, ms, what) {
  const end = Date.now() + ms; let last;
  while (Date.now() < end) { try { last = await fn(); if (last) return last; } catch (e) { last = e; } await sleep(250); }
  throw new Error("timed out waiting for " + what + (last instanceof Error ? ": " + last.message : ""));
}
const text = (sel) => page.textContent(sel);
const visible = (sel) => page.isVisible(sel);
const shot = async (name) => { if (SHOTS) await page.screenshot({ path: path.join(SHOTS, name), fullPage: true }); };
const typeAndLeave = async (sel, value) => { await page.fill(sel, String(value)); await page.press(sel, "Tab"); };
const toastText = () => page.evaluate(() => { const t = document.getElementById("toast"); return getComputedStyle(t).display === "none" ? "" : t.textContent; });
const hideToast = () => page.evaluate(() => { document.getElementById("toast").style.display = "none"; });

(async () => {
  const browser = await chromium.launch({ executablePath: process.env.CHROME_PATH || undefined, args: ["--no-sandbox"] });
  const ctx = await browser.newContext({ viewport: { width: 1300, height: 1000 } });
  page = await ctx.newPage();
  const errors = [];
  page.on("pageerror", e => errors.push(String(e)));
  page.on("console", m => { if (m.type() === "error" && !/status of 400/.test(m.text())) errors.push(m.text()); }); // the 400s are the deliberate bad-input checks
  await page.goto(URL); await page.waitForSelector(".trow:not(.thead), #empty", { timeout: 15000 });

  // 0. a known starting point: bot stopped, tuner off with default settings
  await send("/api/bot", { running: false });
  const keys = (await tuner()).params.map(p => p.key);
  await send("/api/tuner", { enabled: false, clones: 6, round_launches: 100, caution: "balanced", blend: 0.5, params: keys });
  await page.reload(); await page.waitForSelector("#nav-tune:not([hidden])", { timeout: 15000 });

  // 1. the nav item is there; the view is built; the tuner starts out OFF
  check("Auto-tune nav item visible", await visible("#nav-tune"));
  await page.click("#start"); await until(async () => (await text("#state")) === "RUNNING", 10000, "RUNNING");
  await page.click("#nav-tune");
  check("view opens and nav item is highlighted", await visible("#view-tune") && !(await visible("#view-dashboard")) && await page.$eval("#nav-tune", n => n.classList.contains("on")));
  check("switch starts OFF", await page.getAttribute(SW, "aria-checked") === "false" && (await text("#tuneonlbl")) === "OFF");
  check("header explains how it works and its limits", /clones/i.test(await text("#view-tune .lead")) && /cannot create an edge/i.test(await text("#view-tune .limits")) && /made-up/i.test(await text("#view-tune .limits")) && /memoris/i.test(await text("#view-tune .limits")));
  check("no 'start the bot' notice while the bot runs", !(await visible("#tune-stopped")));
  check("settings show the defaults", [await page.inputValue(CLONES), await page.inputValue(ROUND), await page.inputValue(CAUTION), await page.inputValue(STEP)].join() === "6,100,balanced,0.5");
  check("caution options come from the server", same(await page.$$eval(CAUTION + " option", o => o.map(x => [x.value, x.textContent])), [["eager", "Eager 80%"], ["balanced", "Balanced 90%"], ["careful", "Careful 95%"], ["strict", "Strict 99%"]]));
  check("one checkbox per setting, all ticked", (await page.$$eval("#tuneparams input[type=checkbox]", n => n.map(x => x.checked))).every(Boolean) && (await page.$$("#tuneparams input[type=checkbox]")).length === keys.length, `${keys.length} settings`);
  const had = (await tuner()).history.length;  // a fresh server has none; a reused one may (the empty state is also checked on a stubbed page below)
  if (had === 0) check("empty state for past rounds", /No rounds finished yet/.test(await text("#tunehist")));
  else check("earlier rounds are listed (newest first, at most 30)", (await page.$$("#tunehist .round")).length === Math.min(had, 30), `${had} in state`);
  check("round card says it is off", /off/i.test(await text("#roundmsg")) && !(await visible("#roundlive")));
  check("sidebar status line hidden while off", !(await visible("#tunestat")));
  await shot("tuner_off.png");

  // 2. master switch
  await page.click(SW);
  await until(async () => (await tuner()).enabled, 5000, "server to turn the tuner on");
  check("switch shows ON", await page.getAttribute(SW, "aria-checked") === "true" && (await text("#tuneonlbl")) === "ON");
  check("keyboard users can reach the switch (it is a button)", await page.$eval(SW, n => n.tagName === "BUTTON"));

  // 3. enabled but bot stopped: the page says so and the bot can be started from the notice
  await page.click("#stop"); await until(async () => (await text("#state")) === "STOPPED", 10000, "STOPPED");
  await until(() => visible("#tune-stopped"), 5000, "stopped notice");
  check("notice says to start the bot", /Start the bot/.test(await text("#tune-stopped")), await text("#tune-stopped"));
  check("round card waits for the bot", /Waiting for the bot/i.test(await text("#roundmsg")) && !(await visible("#roundlive")));
  check("sidebar line says it is waiting", /Auto-tune: ON.*waiting/i.test(await text("#tunestat")), await text("#tunestat"));
  await page.click("#tunestart"); await until(async () => (await text("#state")) === "RUNNING", 10000, "RUNNING again");
  await until(async () => !(await visible("#tune-stopped")), 5000, "notice to go");
  check("notice goes away once the bot runs", true);

  // 4. settings: send on change (not per keystroke), error handling, focus is respected
  // record the page's own POSTs to /api/tuner (works with a server and with the no-install build, whose "server" is a fake fetch)
  await page.evaluate(() => { const f = window.fetch; window.__tunerPosts = []; window.fetch = (u, o) => { if (String(u).endsWith("/api/tuner") && o && o.method === "POST") window.__tunerPosts.push(o.body); return f(u, o); }; });
  const getPosts = () => page.evaluate(() => window.__tunerPosts.slice());
  await page.focus(ROUND); await page.keyboard.press("Control+A"); await page.keyboard.type("2"); await page.keyboard.type("0");
  await sleep(700);
  let posts = await getPosts();
  check("typing sends nothing until the box is left", posts.length === 0, posts.join("|"));
  await page.press(ROUND, "Tab");
  await until(async () => (await tuner()).config.round_launches === 20, 5000, "launches per round = 20");
  posts = await getPosts();
  check("launches per round saved", posts.length === 1 && same(JSON.parse(posts[0]), { round_launches: 20 }), posts.join("|"));
  await typeAndLeave(CLONES, 4);
  await until(async () => (await tuner()).config.clones === 4, 5000, "clones = 4");
  await page.selectOption(CAUTION, "careful");
  await until(async () => (await tuner()).config.caution === "careful", 5000, "caution = careful");
  await typeAndLeave(STEP, "0.3");
  await until(async () => (await tuner()).config.blend === 0.3, 5000, "step size = 0.3");
  let c = (await tuner()).config;
  check("clones / launches / caution / step size round-trip through /api/state", c.clones === 4 && c.round_launches === 20 && c.caution === "careful" && c.blend === 0.3, JSON.stringify(c));
  const n = (await getPosts()).length;
  await typeAndLeave(CLONES, 4);   // same value again
  await sleep(500);
  check("re-entering the same value sends nothing (a post would restart the round)", (await getPosts()).length === n);

  // "what it may change": untick one setting
  const MIN_TRADES = "Min trades seen";
  await page.uncheck(TUNE(MIN_TRADES));
  await until(async () => !(await tuner()).config.params.includes("min_trades"), 5000, "min_trades to leave the list");
  c = (await tuner()).config;
  check("unticking sends the full new list", c.params.length === keys.length - 1 && !c.params.includes("min_trades") && same(c.params, keys.filter(k => k !== "min_trades")), c.params.join());
  await sleep(1500);
  check("the box stays unticked after the next poll", !(await page.isChecked(TUNE(MIN_TRADES))) && (await page.isChecked(TUNE("Take profit (%)"))));
  // the server says `active:false` for a setting that is unticked OR irrelevant to the bot's modes; only ticked ones may be called "not used"
  const t1 = await tuner(), inactive = t1.params.filter(p => !p.active && t1.config.params.includes(p.key)).map(p => p.key);
  const greyed = await page.$$eval("#tuneparams .prow.off", rows => rows.map(r => r.querySelector("input").id.replace("tp-", "")));
  check("ticked settings the bot's current mode ignores are greyed out with a note", same(greyed.sort(), inactive.slice().sort()) && inactive.length > 0 && (await page.$$eval("#tuneparams .prow.off .pn", n => n.every(x => !x.hidden && x.textContent.startsWith("not used")))), inactive.join());
  check("an unticked setting is not mislabelled 'not used'", await page.$eval("#tp-min_trades", n => !n.closest(".prow").classList.contains("off") && n.closest(".prow").querySelector(".pn").hidden));
  check("active settings show no note", await page.$$eval("#tuneparams .prow:not(.off) .pn", n => n.every(x => x.hidden)));
  const row = await page.$eval("#tp-take_profit", n => n.closest(".prow").textContent);
  const tp = (await tuner()).params.find(p => p.key === "take_profit");
  check("each row shows the bot's current value and the allowed range", row.includes("Take profit (%)") && row.includes("now " + tp.value + "%") && row.includes(`${tp.lo}–${tp.hi}`), row);

  // never overwrite what someone is typing
  await page.focus(CLONES); await page.keyboard.press("Control+A"); await page.keyboard.type("7");
  await sleep(2500);
  check("a focused box is not overwritten by the next polls", await page.inputValue(CLONES) === "7");
  await page.fill(CLONES, "4"); await page.press(CLONES, "Tab");

  // invalid input: error toast, server unchanged, box goes back to the real value
  const before = (await tuner()).config;
  await hideToast();
  await typeAndLeave(CLONES, 99);
  await until(async () => (await toastText()) !== "", 5000, "error toast");
  const msg = await toastText();
  check("clones 99 shows an error toast that explains the limit", /clones/i.test(msg) && /2/.test(msg) && /8/.test(msg) && !(await page.getAttribute("#toast", "class")).includes("ok"), msg);
  check("server config unchanged after the refusal", same((await tuner()).config, before));
  await until(async () => (await page.inputValue(CLONES)) === "4", 5000, "clones box to show the real value again");
  check("box shows the real value again", true);
  await hideToast();
  await page.focus(ROUND); await page.keyboard.press("Control+A"); await page.keyboard.type("5"); await page.keyboard.press("Enter");  // Enter keeps focus: the box must still be corrected
  await until(async () => (await toastText()) !== "", 5000, "toast for launches 5");
  check("launches 5 refused with a message", /launches/i.test(await toastText()), await toastText());
  await until(async () => (await page.inputValue(ROUND)) === "20", 5000, "launches box to be corrected even while focused");
  check("a refused value is corrected even if the box kept focus", true);
  await page.press(ROUND, "Tab");
  await hideToast();
  await typeAndLeave(STEP, 5);
  await until(async () => (await toastText()) !== "", 5000, "toast for step 5");
  check("step size 5 refused with a message", /step/i.test(await toastText()), await toastText());
  check("server config still unchanged", same((await tuner()).config, before));
  await until(async () => (await page.inputValue(STEP)) === "0.3", 5000, "step box to show the real value");

  // 5. the live round
  await until(async () => (await tuner()).clones.length === 4 && (await tuner()).progress >= 2, 60000, "a round with progress");
  await until(() => visible("#roundlive"), 5000, "live round card");
  const rowsN = (await page.$$("#clonerows .ctr")).length;
  check("clones table has 4 rows", rowsN === 4, `${rowsN} rows`);
  const labels = await page.$$eval("#clonerows .ctr:first-child .cm [data-l]", n => n.map(x => x.dataset.l));
  check("each row has trades / avg return / vs bot", same(labels, ["Trades", "Avg return", "Vs bot"]), labels.join());
  const tries = await page.$$eval("#clonerows .tries", n => n.map(x => x.textContent));
  check("every clone says what it tries (or 'same as bot')", tries.length === 4 && tries.every(t => t === "same as bot" || /\(bot: /.test(t)), tries.join(" || "));
  check("a setting that was unticked is never tried", tries.every(t => !t.includes(MIN_TRADES)), tries.join(" || "));
  check("a clone's changes are named like the settings list", tries.every(t => t === "same as bot" || t.split(" · ").every(part => /^(Take profit|Stop loss|Trailing stop|Max hold|Min liquidity|Max dev hold|Max gain already|Confirmation gain) /.test(part))), tries.join(" || "));
  const sign = await page.$$eval("#clonerows .cm > :nth-child(3)", n => n.map(x => [x.textContent, x.className]));
  check("'vs bot' is signed and coloured", sign.every(([t, cls]) => /^[+-]\d+\.\d\d%$/.test(t) && (/\bpos\b/.test(cls) ? !t.startsWith("-") && t !== "+0.00%" : /\bneg\b/.test(cls) ? t.startsWith("-") : /\bmut\b/.test(cls))), JSON.stringify(sign));
  const w1 = await page.$eval("#roundfill", n => parseFloat(n.style.width));
  const line1 = await text("#roundtxt");
  check("progress line reads 'Round N · X of 20 launches'", /^Round \d+ · \d+ of 20 launches$/.test(line1), line1);
  check("progress bar has moved off zero", w1 > 0, w1 + "%");
  const now1 = +(await page.getAttribute("#roundbar", "aria-valuenow")), shown = +line1.match(/· (\d+) of/)[1];
  check("progress bar value matches the text", Math.abs(now1 - Math.round(shown / 20 * 100)) <= 5, `${now1}% vs ${shown}/20`);
  check("bot's average and trades are shown", /^[+-]\d+\.\d\d%$|^—$/.test(await text("#bot-mean")) && /^\d+$/.test(await text("#bot-trades")), `${await text("#bot-mean")} / ${await text("#bot-trades")}`);
  check("sidebar status line follows the round", /^Auto-tune: ON · round \d+ \(\d+\/20\)/.test(await text("#tunestat")), await text("#tunestat"));
  check("round chip says which round", /^ROUND \d+$/.test(await text("#roundchip")), await text("#roundchip"));
  await until(async () => (await page.$eval("#roundfill", n => parseFloat(n.style.width))) > w1, 30000, "progress to advance");
  check("progress advances while the round runs", true);
  await shot("tuner_live.png");

  // phone width: nothing scrolls sideways
  await page.setViewportSize({ width: 390, height: 800 }); await sleep(400);
  const over = await page.evaluate(() => ({ page: document.documentElement.scrollWidth - document.documentElement.clientWidth, table: (s => s.scrollWidth - s.clientWidth)(document.querySelector("#roundlive .scroll")) }));
  check("no horizontal scroll at phone width (page or clones table)", over.page <= 0 && over.table <= 0, JSON.stringify(over));
  check("table header is replaced by per-cell labels at phone width", !(await visible("#roundlive .thead")) && await visible("#clonerows .ctr .cm > [data-l]"));
  await shot("tuner_phone.png");
  await page.setViewportSize({ width: 1300, height: 1000 });

  // 6. wait for a finished round
  await until(async () => (await tuner()).history.length >= 1, 90000, "a finished round");
  await until(async () => (await page.$$("#tunehist .round")).length >= 1, 5000, "the round in Past rounds");
  const first = (await page.$$("#tunehist .round"))[0];
  const roundNo = +(await first.getAttribute("data-round"));
  const B = (sel) => `#tunehist .round[data-round="${roundNo}"] ${sel}`;  // a newer round can land at any moment and rebuild the list, so never hold element handles
  const chip = await page.$eval(B(".chip"), n => n.textContent), why = await page.$eval(B(".why"), n => n.textContent), title = await page.$eval(B(".rh b"), n => n.textContent);
  check("past round is titled 'Round N'", title === "Round " + roundNo, title);
  check("past round has a CHANGED or KEPT chip", chip === "CHANGED" || chip === "KEPT", chip);
  check("past round has a reason", why.length > 10, why);
  const hist = (await tuner()).history.find(h => h.round === roundNo);
  check("past round matches /api/state", !!hist && chip === (hist.decision === "changed" ? "CHANGED" : "KEPT") && why === hist.reason, JSON.stringify(hist && [hist.decision, hist.reason]));
  check("chip is green for CHANGED and neutral for KEPT", await page.$eval(B(".chip"), (n, changed) => n.classList.contains("g") === changed, hist.decision === "changed"));
  check("past round states launches (20) and clone count", !!hist && hist.launches === 20 && hist.clones.length === 4);
  if (hist && hist.decision === "changed") {
    const items = await page.$$eval(B("ul li"), n => n.map(x => x.textContent));
    check("CHANGED lists 'Label: old → new' for every moved setting", items.length === Object.keys(hist.changes).length && items.every(t => /^[^:]+: .+ → .+$/.test(t)), items.join(" | "));
    check("a setting that was unticked was not moved", !Object.keys(hist.changes).includes("min_trades"));
  } else check("KEPT lists no moves", (await page.$$(B("ul li"))).length === 0);
  check("past round shows a time", /\d{1,2}:\d\d:\d\d/.test(await page.$eval(B(".rh .mono"), n => n.textContent)));

  // the log line for the round (checked straight away: the Logs view only shows the newest 200 lines)
  await page.click("#nav button[data-view=logs]");
  const tuneLines = await page.$$eval(".logline.tune", n => n.map(x => x.textContent));
  check("Logs view shows an auto-tune line for the round", tuneLines.length >= 1 && /auto-tune round \d+/.test(tuneLines[0]) && tuneLines[0].includes("TUNE"), tuneLines[0]);
  await page.click("#nav-tune");

  await page.$eval(B("summary"), n => n.click());
  await until(() => page.$eval(B("details"), d => d.open), 3000, "details to open");
  const cr = await page.$$eval(B("details .ctr:not(.thead)"), rows => rows.map(r => [...r.querySelectorAll(".cm [data-l]")].map(x => x.textContent)));
  check("collapsible shows 4 clone rows with trades, avg, lower bound, differed-on and passed", cr.length === 4 && cr.every(r => r.length === 5 && /^(yes|no)$/.test(r[4]) && /^[+-]\d+\.\d\d%$/.test(r[2]) && /launches$/.test(r[3])), JSON.stringify(cr[0]));
  check("passed matches the server", same(cr.map(r => r[4] === "yes"), hist.clones.map(k => k.passed)));
  const hdr = await page.$$eval(B("details .thead span"), n => n.map(x => x.textContent));
  check("collapsible has the full header", same(hdr, ["#", "TRIES", "TRADES", "AVG", "LOWER BOUND", "DIFFERED ON", "PASSED"]), hdr.join());
  await sleep(2500);
  check("an opened round stays open across polls (and across a rebuild when a newer round lands)", await page.$eval(B("details"), d => d.open));
  check("past-rounds counter matches", +(await text("#histcount")) === (await page.$$("#tunehist .round")).length);
  await shot("tuner_history.png");

  // the config survived the round (a finished round must not reset the user's choices) and the bot's settings in the list follow the bot
  c = (await tuner()).config;
  check("config still as set after a finished round", c.clones === 4 && c.round_launches === 20 && c.caution === "careful" && c.blend === 0.3 && !c.params.includes("min_trades"), JSON.stringify(c));
  const st = await state(), live = st.tuner.params.find(p => p.key === "take_profit").value;
  await until(async () => (await page.$eval("#tp-take_profit", n => n.closest(".prow").textContent)).includes("now " + live + "%"), 5000, "list to show the bot's value");
  check("'now' values follow the bot's settings", true);

  // 7. switching off
  await page.click(SW);
  await until(async () => !(await tuner()).enabled, 5000, "tuner off");
  check("switch shows OFF and the live round is hidden", await page.getAttribute(SW, "aria-checked") === "false" && (await text("#tuneonlbl")) === "OFF" && !(await visible("#roundlive")) && /off/i.test(await text("#roundmsg")));
  check("settings and past rounds are kept while off", (await page.$$("#tunehist .round")).length >= 1 && await page.inputValue(CLONES) === "4");
  check("sidebar line disappears", !(await visible("#tunestat")));

  check("no page errors", errors.length === 0, errors.join("|"));

  // 9. a server without a tuner (older build): hide the nav item, never throw
  if (URL.startsWith("http")) {
    const ctx2 = await browser.newContext({ viewport: { width: 1300, height: 900 } });
    const p2 = await ctx2.newPage(); const errs2 = [];
    p2.on("pageerror", e => errs2.push(String(e))); p2.on("console", m => { if (m.type() === "error") errs2.push(m.text()); });
    await p2.route("**/api/state*", async (route) => { const r = await route.fetch(); const j = await r.json(); delete j.tuner; await route.fulfill({ response: r, json: j }); });
    await p2.goto(URL + "/#tune"); await p2.waitForSelector("#modetext b", { timeout: 15000 }); await sleep(2500);
    check("[no tuner] nav item hidden", !(await p2.isVisible("#nav-tune")));
    check("[no tuner] a #tune link falls back to the dashboard", await p2.isVisible("#view-dashboard") && !(await p2.isVisible("#view-tune")));
    check("[no tuner] sidebar line hidden", !(await p2.isVisible("#tunestat")));
    await p2.click("#nav button[data-view=logs]"); await p2.click("#nav button[data-view=profile]"); await p2.click("#nav button[data-view=dashboard]");
    check("[no tuner] other views still work", await p2.isVisible("#view-dashboard"));
    check("[no tuner] no page errors", errs2.length === 0, errs2.join("|"));
    // a stubbed state: the tuner is there, then paused (the reason must be prominent), then gone while its view is open
    const ctx3 = await browser.newContext({ viewport: { width: 1300, height: 900 } });
    const p3 = await ctx3.newPage(); const errs3 = []; let mode = "empty";
    p3.on("pageerror", e => errs3.push(String(e))); p3.on("console", m => { if (m.type() === "error") errs3.push(m.text()); });
    const REASON = "repeat lap: the bot has already seen this data";
    await p3.route("**/api/state*", async (route) => {
      const r = await route.fetch(); const j = await r.json();
      if (mode === "empty") j.tuner.history = [];
      else if (mode === "strip") delete j.tuner; else if (mode === "paused") { j.tuner.enabled = true; j.tuner.paused = REASON; j.running = true; }
      await route.fulfill({ response: r, json: j });
    });
    await p3.goto(URL + "/#tune"); await p3.waitForSelector("#nav-tune:not([hidden])", { timeout: 15000 });
    check("[tuner appears] nav item shown and view open", await p3.isVisible("#nav-tune") && await p3.isVisible("#view-tune"));
    check("[tuner appears] no pause banner", !(await p3.isVisible("#tune-paused")));
    check("[no history] the empty state reads 'No rounds finished yet'", (await p3.textContent("#tunehist")).trim() === "No rounds finished yet" && await p3.textContent("#histcount") === "0");
    mode = "paused"; await p3.click("#b-refresh"); await sleep(1500);
    const banner = await p3.isVisible("#tune-paused") ? await p3.textContent("#tune-paused") : "";
    check("[paused] the reason is shown prominently", banner.includes(REASON) && /^Paused/.test(banner), banner);
    check("[paused] round card and sidebar say paused, no clones table", await p3.textContent("#roundchip") === "PAUSED" && /paused/i.test(await p3.textContent("#tunestat")) && !(await p3.isVisible("#roundlive")));
    await p3.screenshot({ path: SHOTS ? path.join(SHOTS, "tuner_paused.png") : undefined, fullPage: true }).catch(() => {});
    mode = "strip"; await p3.click("#b-refresh"); await sleep(1500);
    check("[tuner vanishes] view closes and nav item hides", !(await p3.isVisible("#view-tune")) && !(await p3.isVisible("#nav-tune")) && await p3.isVisible("#view-dashboard"));
    check("[tuner vanishes] no page errors", errs3.length === 0, errs3.join("|"));
    await ctx2.close(); await ctx3.close();
  } else console.log("SKIP missing-tuner checks (they need page.route on an http server)");

  // leave the instance as we found it
  await send("/api/bot", { running: false });
  await send("/api/tuner", { enabled: false, clones: 6, round_launches: 100, caution: "balanced", blend: 0.5, params: keys });
  await browser.close();
})().catch(e => { console.log("FAIL test crashed: " + (e && e.stack || e)); process.exitCode = 1; });
