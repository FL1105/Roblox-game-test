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
  try { got = { ok: js.settingsDict(js.parseSettings(c.payload, js.defaultStrategy())) }; } catch (e) { got = { error: e.message }; }
  same(c.expected, got, `settings[${i}] ${JSON.stringify(c.payload).slice(0, 60)}`);
}

// 2. whole-terminal runs: settings applied, bot started, events replayed, snapshot compared
for (const [i, c] of cases.runs.entries()) {
  const st = new js.TerminalState(js.defaultStrategy(), { source: "demo", simulated: true, start_sol: c.start_sol });
  st.updateSettings(c.payload); st.clock = c.clock0; st.setRunning(true);
  for (const ev of c.events) st.emit(ev);
  for (const win of ["24h", "all"]) same(c.snapshots[win], JSON.parse(JSON.stringify(st.snapshot(win))), `run[${i}:${c.name}].snapshot(${win})`);
  same(c.closed, st.engine.closed, `run[${i}:${c.name}].closed`);
}
console.log(bad.length ? bad.slice(0, 25).join("\n") + `\n${bad.length} mismatch(es)` : `ok: ${cases.settings.length} settings cases, ${cases.runs.length} runs`);
process.exit(bad.length ? 1 : 0);
