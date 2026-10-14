// The browser build's PaperEngine.fork(): a copy must carry on exactly like the original and share nothing with it.
// Usage: node tests/js/fork.js cases.json
const fs = require("fs"), path = require("path");
const js = require(path.join(__dirname, "../../sniper/terminal/static/standalone.js"));
const cases = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
const bad = [];
const note = (m) => bad.push(m);
const seen = {};

const summary = (e) => JSON.stringify({
  cash: e.cash, last_t: e.last_t, closed: e.closed, positions: [...e.positions.entries()], pending: [...e.pending], watch: [...e.watch], pending_state: [...e.pending_state],
  skipped: e.skipped, rugs: [...e.known_ruggers].sort(), sold: [...e.creator_sold], tapes: [...e.tapes.entries()].map(([m, t]) => [m, t.ts, t.vs, t.vt, t.peak_price, t.peak_t]),
  creator_launches: [...e.creator_launches], curve: [...e.curve], launch: [...e.launch].length, scans: [...e.scans].length, eq: e.equity_curve,
});
const finish = (e) => JSON.stringify(e.finish());
const feed = (e, evs) => { for (const ev of evs) e.onEvent(ev); };

for (const c of cases.cases) {
  const strat = () => js.parseSettings(c.payload, js.defaultStrategy(), false);
  const events = c.events;
  // split points: fixed fractions, plus moments where something is waiting / open
  const splits = [0, 0.2, 0.5, 0.8].map((f) => ["fraction", Math.floor(events.length * f)]);
  const probe = new js.PaperEngine(strat(), 1.0), last = {};
  events.forEach((ev, i) => {
    probe.onEvent(ev);
    const exiting = [...probe.positions.values()].some((p) => p.exit_ready_ms !== null);
    for (const [key, there] of [["pending", probe.pending.size], ["watch", probe.watch.size], ["positions", probe.positions.size], ["creator_sold", probe.creator_sold.size], ["exiting", exiting]]) {
      if (there && (last[key] === undefined || i - last[key] > 25) && (splits.filter((s) => s[0] === key).length < 3)) { splits.push([key, i + 1]); last[key] = i; }
    }
  });
  // the whole run must agree with Python's
  const whole = new js.PaperEngine(strat(), 1.0); feed(whole, events);
  const fin = whole.finish();
  if (JSON.stringify(fin.closed.map((t) => t.mint + t.reason)) !== JSON.stringify(c.expected.map((t) => t.mint + t.reason))) note(`${c.name}: closed trades differ from Python`);
  for (const [kind, k] of splits) {
    const where = `${c.name} ${kind}@${k}`;
    const original = new js.PaperEngine(strat(), 1.0); feed(original, events.slice(0, k));
    const twin = original.fork();
    if (kind !== "fraction") seen[kind] = (seen[kind] || 0) + 1;
    feed(original, events.slice(k)); feed(twin, events.slice(k));
    if (finish(twin) !== finish(original)) note(`${where}: copy finished differently from the original`);
    // independence, both directions
    const a = new js.PaperEngine(strat(), 1.0); feed(a, events.slice(0, k));
    const before = summary(a); const b = a.fork(); feed(b, events.slice(k)); b.finish();
    if (summary(a) !== before) note(`${where}: feeding the copy changed the original`);
    const a2 = new js.PaperEngine(strat(), 1.0); feed(a2, events.slice(0, k));
    const b2 = a2.fork(); const before2 = summary(b2); feed(a2, events.slice(k));
    if (summary(b2) !== before2) note(`${where}: feeding the original changed the copy`);
    // nothing mutable shared
    const x = new js.PaperEngine(strat(), 1.0); feed(x, events.slice(0, k)); const y = x.fork();
    for (const name of ["tapes", "curve", "launch", "scans", "pending", "watch", "pending_state", "creator_sold", "known_ruggers", "creator_launches", "positions", "closed", "equity_curve", "skipped"])
      if (x[name] === y[name]) note(`${where}: ${name} is shared`);
    for (const [m, t] of x.tapes) { if (y.tapes.get(m) === t || y.tapes.get(m).ts === t.ts || y.tapes.get(m).vs === t.vs || y.tapes.get(m).vt === t.vt) note(`${where}: tape ${m} is shared`); }
    for (const [m, p] of x.positions) if (y.positions.get(m) === p) note(`${where}: position ${m} is shared`);
    for (const [cr, l] of x.creator_launches) if (y.creator_launches.get(cr) === l) note(`${where}: creator list is shared`);
  }
}
// hand-built cases for state that random markets rarely exercise
for (const h of cases.hand) {
  const strat = () => js.parseSettings(h.payload, js.defaultStrategy(), false);
  const original = new js.PaperEngine(strat(), 1.0); feed(original, h.events.slice(0, h.split));
  const twin = original.fork();
  feed(original, h.events.slice(h.split)); feed(twin, h.events.slice(h.split));
  for (const [who, e] of [["original", original], ["copy", twin]]) {
    if (h.expect.slippage !== undefined && (e.skipped.slippage || 0) !== h.expect.slippage) note(`${h.name}: ${who} slippage skips ${e.skipped.slippage}`);
    if (h.expect.slippage !== undefined && e.positions.size !== 0) note(`${h.name}: ${who} bought anyway`);
    if (h.expect.ruggers && JSON.stringify([...e.known_ruggers].sort()) !== JSON.stringify(h.expect.ruggers)) note(`${h.name}: ${who} did not catch the rugger`);
  }
}
for (const key of ["pending", "watch", "positions", "creator_sold", "exiting"]) if (!seen[key]) note(`no fork was ever made while '${key}' was non-empty`);
// a copy made with new settings uses them, and the original keeps its own
{
  const c = cases.cases[0], base = js.parseSettings(Object.assign({}, c.payload, { take_profit: 100, stop_loss: 60 }), js.defaultStrategy(), false);
  const o = new js.PaperEngine(base, 1.0); feed(o, c.events.slice(0, 40));
  const quick = o.fork(js.parseSettings({ take_profit: 6, stop_loss: 6 }, base, false));
  if (o.s !== base || quick.s.take_profit_pct !== 6) note("fork(strategy) did not use the strategy it was given");
  feed(o, c.events.slice(40)); feed(quick, c.events.slice(40));
  if (JSON.stringify(o.finish().closed) === JSON.stringify(quick.finish().closed)) note("quick-exit copy behaved exactly like the slow original");
  if (quick.onNote !== null) note("a copy must not make notes");
}
console.log(bad.length ? bad.slice(0, 25).join("\n") + `\n${bad.length} problem(s)` : `ok: ${cases.cases.length} strategies, forks at ${Object.entries(seen).map(([k, v]) => k + ":" + v).join(" ")}`);
process.exit(bad.length ? 1 : 0);
