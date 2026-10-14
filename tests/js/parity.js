// Runs the browser port (sniper/terminal/static/standalone.js) on the cases Python produced and compares results.
// Usage: node tests/js/parity.js cases.json   (exit 1 and a list of mismatches on any difference)
const fs = require("fs"), path = require("path");
const js = require(path.join(__dirname, "../../sniper/terminal/static/standalone.js"));
const cases = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
const bad = [];

function same(a, b, where) {
  if (typeof a === "number" && typeof b === "number") {
    if (Math.abs(a - b) > 1e-9 * Math.max(1, Math.abs(a), Math.abs(b))) bad.push(`${where}: python ${a} vs js ${b}`);
  } else if (Array.isArray(a) || Array.isArray(b)) {
    if (!Array.isArray(a) || !Array.isArray(b) || a.length !== b.length) return bad.push(`${where}: array length python ${a && a.length} vs js ${b && b.length}`);
    a.forEach((x, i) => same(x, b[i], `${where}[${i}]`));
  } else if (a && b && typeof a === "object" && typeof b === "object") {
    const keys = new Set([...Object.keys(a), ...Object.keys(b)]);
    for (const k of keys) { if (!(k in a)) bad.push(`${where}.${k}: only in js`); else if (!(k in b)) bad.push(`${where}.${k}: only in python`); else same(a[k], b[k], `${where}.${k}`); }
  } else if (a !== b) bad.push(`${where}: python ${JSON.stringify(a)} vs js ${JSON.stringify(b)}`);
}

// 1. settings validation: same accept/reject, same message, same resulting settings
for (const [i, c] of cases.settings.entries()) {
  let got;
  try { got = { ok: js.settingsDict(js.parseSettings(c.payload, js.defaultStrategy(), c.has_model)) }; } catch (e) { got = { error: e.message }; }
  same(c.expected, got, `settings[${i}] ${JSON.stringify(c.payload).slice(0, 60)}`);
}

// 2. whole-terminal runs: settings applied, bot started, events replayed, snapshot compared
for (const [i, c] of cases.runs.entries()) {
  const st = new js.TerminalState(js.defaultStrategy(), { source: "demo", simulated: true, start_sol: c.start_sol, model: c.model });
  st.updateSettings(c.payload); st.clock = c.clock0; if (c.tuner) st.configureTuner(c.tuner); st.setRunning(true);
  for (const ev of c.events) st.emit(ev);
  for (const [key, expected] of Object.entries(c.snapshots)) {
    const [win, rows] = key.split("@");  // "all@3" = the all-time window limited to 3 token rows
    same(expected, JSON.parse(JSON.stringify(st.snapshot(win, rows))), `run[${i}:${c.name}].snapshot(${key})`);
  }
  same(c.closed, st.engine.closed, `run[${i}:${c.name}].closed`);
}
// 3. path features and model scores on many random token paths, compared number by number
for (const [i, c] of cases.features.entries()) {
  const tape = js.tapeFromLaunch(c.launch);
  for (const t of c.trades) tape.add(t.t, t.v_sol, t.v_tokens);
  const feats = js.pathFeatures(tape, c.now);
  same(c.features, feats, `features[${i}]`);
  same(c.scores, new js.ModelExit(cases.model).scores(feats), `scores[${i}]`);
}
// 4. repeat laps: the browser build renames and shifts a recording exactly as Python does, and a looping feed really keeps going
for (const [lap, expected] of Object.entries(cases.laps.laps)) same(expected, js.repeatLap(cases.laps.events, +lap), `lap ${lap}`);
{
  const st = new js.TerminalState(js.defaultStrategy(), { source: "replay x", simulated: false, start_sol: 1 });
  const feed = new js.ReplayFeed(st, cases.laps.events, { loop: true, speed: 50 });
  let ticks = 0; while (st.lap < 3 && ticks++ < 100000) feed.tick(100);
  if (st.lap !== 3) bad.push(`looping feed stuck on lap ${st.lap}`);
  if (st.events_seen < 2 * cases.laps.events.length) bad.push(`looping feed emitted only ${st.events_seen} events`);
  if (st.feed_status === "finished") bad.push("looping feed reported finished");
  if (![...st.tokens.keys()].some((m) => m.endsWith("~2"))) bad.push("lap 2 tokens missing");
  const once = new js.TerminalState(js.defaultStrategy(), { source: "replay x", simulated: false, start_sol: 1 });
  const feed1 = new js.ReplayFeed(once, cases.laps.events, { speed: 50 });
  for (let i = 0; i < 100000 && !feed1.done; i++) feed1.tick(100);
  if (once.feed_status !== "finished" || once.lap !== 1 || once.events_seen !== cases.laps.events.length) bad.push(`one-shot feed: ${once.feed_status} lap ${once.lap} events ${once.events_seen}`);
}
// 5. the tuner's pure pieces on awkward inputs
{
  const T = cases.tuner;
  for (const [seed, expected] of Object.entries(T.rng)) { const g = js.mulberry32(+seed); same(expected, expected.map(() => g()), `rng seed ${seed}`); }
  T.snap.forEach(([x, p, expected], i) => { const got = js.snap(x, p); if (got !== expected) bad.push(`snap[${i}] ${x},${p}: python ${expected} vs js ${got} (must be identical, not just close)`); });
  T.gate.forEach((c, i) => same(c.expected, js.pairedGate(c.diffs, c.alpha, js.mulberry32(c.seed)).map((r) => r.slice()), `gate[${i}]`));
  const strat = (fields) => Object.assign(js.defaultStrategy(), fields, fields.ladder ? { ladder: fields.ladder } : {});
  T.blend.forEach((c, i) => {
    const s = strat(c.fields);
    same(c.expected, js.blendChanges(s, new js.Tuner({ enabled: true, clones: 6, round_launches: 100, caution: "balanced", blend: 0.5, params: js.TUNABLES.map((t) => t.key) }).activeTunables(s), c.winners, c.blend), `blend[${i}]`);
  });
  T.plan.forEach((c, i) => { const t = new js.Tuner(c.cfg, 77), s = strat(c.fields); same(c.expected, c.expected.map(() => t._plan(s, t.activeTunables(s))), `plan[${i}]`); });
  T.passes.forEach(([ch, mean, lcb, k, expected], i) => { if (js.clonePasses(ch, mean, lcb, k) !== expected) bad.push(`clonePasses[${i}] ${JSON.stringify([ch, mean, lcb, k])}: python ${expected}`); });
  T.alpha.forEach(([c, n, expected], i) => same(expected, js.alphaFor(c, n), `alphaFor[${i}] ${c},${n}`));
  // scripted round-end decisions
  T.judge.forEach((c, i) => {
    const tuner = new js.Tuner(c.cfg, undefined, { gate: () => c.gate.map((r) => r.slice()), blender: c.empty_blend ? () => ({}) : undefined });
    const engine = new js.PaperEngine(js.parseSettings(c.payload, js.defaultStrategy(), false), 1.0);
    let verdict = null;
    for (const ev of c.events) { engine.onEvent(ev); verdict = tuner.feed(ev, engine); if (verdict) break; }
    same(c.expected, JSON.parse(JSON.stringify(verdict)), `judge[${i}:${c.name}]`);
  });
  // the terminal's handling of the tuner, step by step
  T.script.forEach((c, i) => {
    const st = new js.TerminalState(js.defaultStrategy(), { source: "demo", simulated: true, start_sol: 5.0 });
    st.updateSettings(c.payload); st.clock = c.events[0].t - 1000;
    c.steps.forEach((step, n) => {
      const [kind, arg] = Object.entries(step)[0];
      if (kind === "configure") st.configureTuner(arg);
      else if (kind === "configure_bad") { try { st.configureTuner(arg); } catch (e) { /* refused, as in Python */ } }
      else if (kind === "emit") for (const ev of c.events.slice(arg[0], arg[1])) st.emit(ev);
      else if (kind === "running") st.setRunning(arg);
      else if (kind === "settings") st.updateSettings(arg);
      else if (kind === "lap") st.setLap(arg);
      else if (kind === "reset") st.reset();
      const s = JSON.parse(JSON.stringify(st.snapshot("all")));
      same(c.observed[n], { tuner: s.tuner, settings: s.settings, running: s.running, paper: s.paper, lap: s.lap, stats: s.stats, notes: st.logs.filter((l) => l.kind === "tune" || l.kind === "info").map((l) => l.text) }, `script[${i}:${c.name}] step ${n} ${JSON.stringify(step).slice(0, 60)}`);
    });
  });
  T.parse.forEach((c, i) => {
    let got; try { got = { ok: js.parseTuner(c.data, { enabled: false, clones: 6, round_launches: 100, caution: "balanced", blend: 0.5, params: js.TUNABLES.map((t) => t.key) }) }; } catch (e) { got = { error: e.message }; }
    same(c.expected, got, `parseTuner[${i}] ${JSON.stringify(c.data)}`);
  });
}
console.log(bad.length ? bad.slice(0, 25).join("\n") + `\n${bad.length} mismatch(es)` : `ok: ${cases.settings.length} settings cases, ${cases.runs.length} runs, ${cases.features.length} feature/score cases, ${Object.keys(cases.laps.laps).length} lap cases, ${cases.tuner.gate.length + cases.tuner.plan.length + cases.tuner.parse.length} tuner cases`);
process.exit(bad.length ? 1 : 0);
