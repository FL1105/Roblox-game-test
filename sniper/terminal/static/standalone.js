/* Browser-only build of the paper-trading terminal: the Python engine, settings validation and demo market
 * rewritten in JavaScript, with a fake /api/* so the same page works with no server. tests/test_js_parity.py
 * runs this file and the Python code on identical inputs and compares the results.
 * Simulated demo data only. There is no network access, wallet or key anywhere in this file. */
(function (root) {
"use strict";

/* ---------------- curve ---------------- */
const TOTAL_SUPPLY = 1e9, VIRTUAL_START_SOL = 30, GRADUATION_V_TOKENS = 280e6, DAY_MS = 86400000;
const mcapOf = (s) => (s.v_sol / s.v_tokens) * TOTAL_SUPPLY;
const isGraduated = (s) => s.v_tokens <= GRADUATION_V_TOKENS;
function quoteBuy(s, solIn, feeBps) {
  const sol = solIn * (1 - feeBps / 10000), k = s.v_sol * s.v_tokens;
  return s.v_tokens - k / (s.v_sol + sol);
}
function quoteSell(s, tokensIn, feeBps) {
  const k = s.v_sol * s.v_tokens, gross = s.v_sol - k / (s.v_tokens + tokensIn);
  return gross * (1 - feeBps / 10000);
}

/* ---------------- seeded random numbers (the demo market only; Python's generator is not reproduced) ---------------- */
function mulberry32(a) {
  return function () {
    a |= 0; a = (a + 0x6D2B79F5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}
function makeRng(seed) {
  const r = mulberry32(seed);
  return {
    random: r,
    uniform: (a, b) => a + (b - a) * r(),
    randint: (a, b) => a + Math.floor(r() * (b - a + 1)),
    choice: (arr) => arr[Math.floor(r() * arr.length)],
    expovariate: (lambda) => -Math.log(1 - r()) / lambda,
    bits32: () => Math.floor(r() * 4294967296),
  };
}

/* ---------------- decision features, smart rules, learned model (ports of sniper/policy/) ---------------- */
const FEATURES = ["age_s", "liq_sol", "log_ret_launch", "mom_5", "mom_15", "mom_45", "flow_15", "flow_60", "trades_15", "trades_60",
  "idle_s", "up_frac_30", "dd_peak", "since_peak_s", "progress", "vol_30", "dev_pct"];
const GRADUATION_SOL = 85;

class TokenTape {
  constructor(launch_t, dev_pct, launch_price) {
    this.launch_t = launch_t; this.dev_pct = dev_pct || 0; this.launch_price = launch_price || VIRTUAL_START_SOL / 1073000000;
    this.ts = []; this.vs = []; this.vt = []; this.peak_price = 0; this.peak_t = 0;
  }
  add(t, v_sol, v_tokens) {
    const n = this.vt.length;
    if (n && this.vt[n - 1] === v_tokens && this.vs[n - 1] === v_sol) return;
    this.ts.push(t); this.vs.push(v_sol); this.vt.push(v_tokens);
    const price = v_sol / v_tokens;
    if (price >= this.peak_price) { this.peak_price = price; this.peak_t = t; }
  }
  price(i) { return this.vs[i] / this.vt[i]; }
  indexAt(t) { let lo = 0, hi = this.ts.length; while (lo < hi) { const mid = (lo + hi) >> 1; if (this.ts[mid] <= t) lo = mid + 1; else hi = mid; } return lo - 1; }
  priceAt(t) { const i = this.indexAt(t); return i < 0 ? this.launch_price : this.price(i); }
  vsolAt(t) { const i = this.indexAt(t); return i < 0 ? VIRTUAL_START_SOL : this.vs[i]; }
}
function tapeFromLaunch(launch) {
  const tape = new TokenTape(launch.t, launch.dev_tokens / TOTAL_SUPPLY * 100, launch.v_sol / launch.v_tokens);
  tape.add(launch.t, launch.v_sol, launch.v_tokens);
  return tape;
}
function pathFeatures(tape, now) {
  const last = tape.ts.length - 1;
  if (now === undefined || now === null) now = tape.ts[last];
  const price = tape.price(last), vsol = tape.vs[last];
  const mom = (w) => Math.log(price / tape.priceAt(now - Math.trunc(w * 1000)));
  const flow = (w) => vsol - tape.vsolAt(now - Math.trunc(w * 1000));
  const trades = (w) => { let n = 0; for (const t of tape.ts) if (t > now - Math.trunc(w * 1000) && t > tape.launch_t) n++; return n; };
  const recent = []; for (let i = Math.max(0, last - 40); i <= last; i++) if (tape.ts[i] > now - 30000 && i > 0) recent.push(i);
  let ups = 0, downs = 0; const rets = [];
  for (const i of recent) { if (tape.price(i) > tape.price(i - 1)) ups++; if (tape.price(i) < tape.price(i - 1)) downs++; rets.push(Math.log(tape.price(i) / tape.price(i - 1))); }
  let vol = 0;
  if (rets.length >= 2) { const mean = rets.reduce((a, b) => a + b, 0) / rets.length; vol = Math.sqrt(rets.reduce((a, r) => a + (r - mean) * (r - mean), 0) / (rets.length - 1)); }
  return {
    age_s: (now - tape.launch_t) / 1000, liq_sol: vsol - VIRTUAL_START_SOL, log_ret_launch: Math.log(price / tape.launch_price),
    mom_5: mom(5), mom_15: mom(15), mom_45: mom(45), flow_15: flow(15), flow_60: flow(60), trades_15: trades(15), trades_60: trades(60),
    idle_s: (now - tape.ts[last]) / 1000, up_frac_30: ups + downs ? ups / (ups + downs) : 0.5, dd_peak: Math.log(price / tape.peak_price),
    since_peak_s: (now - tape.peak_t) / 1000, progress: Math.min(Math.max((vsol - VIRTUAL_START_SOL) / GRADUATION_SOL, 0), 1), vol_30: vol, dev_pct: tape.dev_pct,
  };
}

const SMART_DEFAULTS = { tp_floor: 30, tp_cap: 150, arm_ret: 15, trail_k: 3, trail_min: 0.05, trail_max: 0.25, stall_idle_s: 30, flow_exit: 0.04, use_stall: true, use_flow: true, use_trail: true, use_momentum_tp: true };
function smartDecide(p, ret, peakRet, f) {
  if (ret >= p.tp_cap) return "take_profit";
  if (p.use_momentum_tp && ret >= p.tp_floor && f.mom_5 <= 0) return "profit_stall";
  if (p.use_trail && peakRet >= p.arm_ret) {
    const width = Math.min(Math.max(p.trail_k * f.vol_30, p.trail_min), p.trail_max);
    if (1 + ret / 100 <= (1 + peakRet / 100) * (1 - width)) return "smart_trail";
  }
  if (p.use_stall && ret > 0 && f.idle_s >= p.stall_idle_s) return "stalled";
  if (p.use_flow && ret > 0 && f.flow_15 < -p.flow_exit * Math.max(f.liq_sol + 30.0, 1.0)) return "sell_pressure";
  return null;
}

const LOG1P = new Set(["age_s", "liq_sol", "trades_15", "trades_60", "idle_s", "since_peak_s", "dev_pct"]), SIGNED_LOG1P = new Set(["flow_15", "flow_60"]);
function transformFeatures(f) {
  return FEATURES.map((name) => {
    let x = f[name];
    if (LOG1P.has(name)) x = Math.log1p(Math.max(x, 0));
    else if (SIGNED_LOG1P.has(name)) x = Math.log1p(Math.abs(x)) * (x < 0 ? -1 : 1);
    return x;
  });
}
function logisticProba(m, x) {
  let z = m.intercept;
  for (let i = 0; i < x.length; i++) z += m.coef[i] * (x[i] - m.mean[i]) / m.std[i];
  return 1 / (1 + Math.exp(-Math.max(Math.min(z, 35), -35)));
}
class ModelExit {
  constructor(modelJson, params) {
    if (JSON.stringify(modelJson.features) !== JSON.stringify(FEATURES)) throw new Error("model was trained on a different feature list; retrain it");
    this.up = modelJson.up; this.down = modelJson.down; this.params = Object.assign({ sell_margin: 0.10, min_age_s: 5.0 }, params || {});
  }
  scores(f) { const x = transformFeatures(f); return [logisticProba(this.up, x), logisticProba(this.down, x)]; }
  decide(ret, peakRet, f) {
    if (f.age_s < this.params.min_age_s) return null;
    const x = transformFeatures(f);
    return logisticProba(this.down, x) - logisticProba(this.up, x) >= this.params.sell_margin ? "model_sell" : null;
  }
}

/* ---------------- paper engine (port of sniper/paper/engine.py) ---------------- */
function defaultStrategyFields() {
  return {
    take_profit_pct: 30, stop_loss_pct: 20, max_hold_s: 300, size_sol: 0.1, size_frac: null, size_caution_sol: null,
    max_open: 3, latency_ms: 800, fee_bps: 123.8, tx_cost_sol: 0.003, exit_on_creator_sell: true, require_scan: true,
    allowed_verdicts: ["PASS", "CAUTION"], max_dev_buy_pct: 15, max_entry_mcap_sol: 60, slippage_pct: 10,
    trailing_stop_pct: 0, min_liquidity_sol: 0, size_mode: "verdict", auto_compound: false, use_blacklist: true, blacklist: [],
    exit_mode: "targets", smart: null, entry_mode: "snipe", confirm_gain_pct: 10, confirm_min_flow_sol: 0, confirm_window_s: 60,
  };
}
const cmpStr = (a, b) => (a < b ? -1 : a > b ? 1 : 0);

class PaperEngine {
  constructor(strategy, startSol, onNote, modelPolicy) {
    this.s = strategy;
    this.model_policy = modelPolicy || null;
    this.tapes = new Map(); this.watch = new Map();
    this.enabled = true;
    this.onNote = onNote || null;
    this.start_sol = startSol === undefined ? 1 : startSol;
    this.cash = this.start_sol;
    this.curve = new Map(); this.launch = new Map(); this.scans = new Map();
    this.pending = new Map(); this.pending_state = new Map();
    this.rng = makeRng(0);
    this.creator_sold = new Map(); this.known_ruggers = new Set();
    this.positions = new Map(); this.closed = [];
    this.equity_curve = [this.start_sol];
    this.skipped = {};
    this.last_t = 0;
  }
  _note(t, kind, mint, text) { if (this.onNote) this.onNote(t, kind, mint, text); }
  _skip(t, mint, reason) { this.skipped[reason] = (this.skipped[reason] || 0) + 1; this._note(t, "skip", mint, `skipped (${reason})`); }

  onEvent(ev) {
    this.last_t = Math.max(this.last_t, ev.t);
    this._requestTimeouts(ev.t);
    this._expireWatches(ev.t);
    this._reviewPositions(ev.t);
    this._fillDue(ev.t);
    if (ev.type === "new_token") {
      this.launch.set(ev.mint, ev);
      this.tapes.set(ev.mint, tapeFromLaunch(ev));
      this.curve.set(ev.mint, { v_sol: ev.v_sol, v_tokens: ev.v_tokens });
      if (!this.s.require_scan) this._considerEntry(ev.mint, ev.t);
    } else if (ev.type === "scan") {
      this.scans.set(ev.mint, ev);
      if (this.s.require_scan && this.launch.has(ev.mint)) {
        if (this.s.allowed_verdicts.includes(ev.verdict)) this._considerEntry(ev.mint, ev.t);
        else this._skip(ev.t, ev.mint, `verdict:${ev.verdict}`);
      }
    } else if (ev.type === "trade") this._onTrade(ev);
  }

  finish() {
    for (const pos of [...this.positions.values()]) this._close(pos, this.last_t, pos.exit_reason || "end_of_data");
    if (this.s.require_scan) { let n = 0; for (const m of this.launch.keys()) if (!this.scans.has(m)) n++; this.skipped.no_scan = (this.skipped.no_scan || 0) + n; }
    this.skipped.entry_never_filled = (this.skipped.entry_never_filled || 0) + this.pending.size;
    this.skipped.no_confirmation = (this.skipped.no_confirmation || 0) + this.watch.size;
    return { closed: this.closed, skipped: this.skipped, final_equity: this.cash, equity_curve: this.equity_curve };
  }

  _considerEntry(mint, decisionT) {
    if (this.pending.has(mint) || this.positions.has(mint) || this.watch.has(mint)) return;
    if (this.s.entry_mode === "confirm" && this.enabled) {
      this.watch.set(mint, decisionT + Math.trunc(this.s.confirm_window_s * 1000));
      this._checkWatch(mint, decisionT);
      return;
    }
    this._tryEntry(mint, decisionT);
  }

  _checkWatch(mint, now) {
    const deadline = this.watch.get(mint);
    if (deadline === undefined) return;
    if (now > deadline) { this.watch.delete(mint); this._skip(now, mint, "no_confirmation"); return; }
    const tape = this.tapes.get(mint);
    if (!tape) return;
    const f = pathFeatures(tape, now);
    if ((Math.exp(f.log_ret_launch) - 1) * 100 >= this.s.confirm_gain_pct && f.flow_15 >= this.s.confirm_min_flow_sol) { this.watch.delete(mint); this._tryEntry(mint, now); }
  }

  _expireWatches(now) {
    for (const [mint, d] of [...this.watch]) if (now > d) this._checkWatch(mint, now);
  }

  _tryEntry(mint, decisionT) {
    const launch = this.launch.get(mint), state = this.curve.get(mint), s = this.s;
    if (!this.enabled) this._skip(decisionT, mint, "bot_stopped");
    else if (s.use_blacklist && (s.blacklist.includes(launch.creator) || this.known_ruggers.has(launch.creator))) this._skip(decisionT, mint, "blacklisted");
    else if (launch.dev_tokens / TOTAL_SUPPLY * 100 > s.max_dev_buy_pct) this._skip(decisionT, mint, "dev_buy");
    else if (state.v_sol - VIRTUAL_START_SOL < s.min_liquidity_sol) this._skip(decisionT, mint, "low_liquidity");
    else if (mcapOf(state) > s.max_entry_mcap_sol) this._skip(decisionT, mint, "mcap_too_high");
    else if (this.positions.size + this.pending.size >= s.max_open) this._skip(decisionT, mint, "max_open");
    else { this.pending.set(mint, decisionT + s.latency_ms); this.pending_state.set(mint, state); }
  }

  _size(mint, equity) {
    const s = this.s;
    if (s.size_frac !== null) return equity * s.size_frac;
    const lo = s.size_caution_sol !== null ? s.size_caution_sol : s.size_sol;
    let size;
    if (s.size_mode === "random") size = this.rng.uniform(lo, s.size_sol);
    else if (s.size_mode === "fixed") size = s.size_sol;
    else { const scan = this.scans.get(mint); size = scan && scan.verdict === "CAUTION" ? lo : s.size_sol; }
    return s.auto_compound ? size * equity / this.start_sol : size;
  }

  _open(mint, t) {
    const state = this.curve.get(mint), decided = this.pending_state.get(mint);
    this.pending_state.delete(mint);
    if (mcapOf(state) > this.s.max_entry_mcap_sol) { this._skip(t, mint, "mcap_too_high_at_fill"); return; }
    let invested = 0; for (const p of this.positions.values()) invested += p.sol_in;
    const equity = this.cash + invested, size = this._size(mint, equity);
    if (this.cash < size + this.s.tx_cost_sol || size <= 0) { this._skip(t, mint, "no_cash"); return; }
    if (decided !== undefined) {
      const expected = quoteBuy(decided, size, this.s.fee_bps);
      const slip = (1 - quoteBuy(state, size, this.s.fee_bps) / expected) * 100;
      if (slip > this.s.slippage_pct) { this._skip(t, mint, "slippage"); return; }
    }
    const tokens = quoteBuy(state, size, this.s.fee_bps);
    this.cash -= size + this.s.tx_cost_sol;
    this.positions.set(mint, { mint, creator: this.launch.get(mint).creator, entry_t: t, sol_in: size, tokens, entry_mcap: mcapOf(state), exit_reason: null, exit_ready_ms: null, peak_net: 0 });
    this._note(t, "entry", mint, `bought ${size.toFixed(3)} SOL at ${mcapOf(state).toFixed(1)} SOL mcap`);
  }

  _onTrade(tr) {
    const state = { v_sol: tr.v_sol, v_tokens: tr.v_tokens }, s = this.s;
    this.curve.set(tr.mint, state);
    const tape = this.tapes.get(tr.mint);
    if (tape) tape.add(tr.t, tr.v_sol, tr.v_tokens);
    if (this.watch.has(tr.mint)) this._checkWatch(tr.mint, tr.t);
    const launch = this.launch.get(tr.mint);
    if (launch && tr.trader === launch.creator && tr.side === "sell" && launch.dev_tokens > 0) {
      const sold = (this.creator_sold.get(tr.mint) || 0) + tr.tokens;
      this.creator_sold.set(tr.mint, sold);
      if (sold >= 0.5 * launch.dev_tokens) this.known_ruggers.add(launch.creator);
    }
    const pos = this.positions.get(tr.mint);
    if (!pos || pos.exit_ready_ms !== null) return;
    let reason = null;
    if (s.exit_on_creator_sell && tr.trader === pos.creator && tr.side === "sell") reason = "creator_sold";
    else if (isGraduated(state)) reason = "graduated";
    else reason = this._exitReason(pos, state, tr.t);
    if (reason) { pos.exit_reason = reason; pos.exit_ready_ms = tr.t + s.latency_ms; }
  }

  _exitReason(pos, state, now) {
    const s = this.s;
    const net = quoteSell(state, pos.tokens, s.fee_bps) - 2 * s.tx_cost_sol;
    pos.peak_net = Math.max(pos.peak_net, net);
    const ret = (net / pos.sol_in - 1) * 100;
    if (s.exit_mode === "targets") {
      if (ret >= s.take_profit_pct) return "take_profit";
      if (s.trailing_stop_pct > 0 && pos.peak_net > pos.sol_in && net <= pos.peak_net * (1 - s.trailing_stop_pct / 100)) return "trailing_stop";
      if (ret <= -s.stop_loss_pct) return "stop_loss";
      return null;
    }
    if (ret <= -s.stop_loss_pct) return "stop_loss";
    const tape = this.tapes.get(pos.mint);
    if (!tape) return null;
    const feats = pathFeatures(tape, now), peakRet = (pos.peak_net / pos.sol_in - 1) * 100;
    if (s.exit_mode === "model" && this.model_policy) return this.model_policy.decide(ret, peakRet, feats);
    const params = s.smart || Object.assign({}, SMART_DEFAULTS, { tp_floor: s.take_profit_pct, tp_cap: Math.max(150.0, 4 * s.take_profit_pct) });
    return smartDecide(params, ret, peakRet, feats);
  }

  _reviewPositions(now) {
    if (this.s.exit_mode === "targets") return;
    for (const pos of this.positions.values()) {
      if (pos.exit_ready_ms === null) {
        const reason = this._exitReason(pos, this.curve.get(pos.mint), now);
        if (reason) { pos.exit_reason = reason; pos.exit_ready_ms = now + this.s.latency_ms; }
      }
    }
  }

  _requestTimeouts(now) {
    const holdMs = Math.trunc(this.s.max_hold_s * 1000);
    for (const pos of this.positions.values()) {
      if (pos.exit_ready_ms === null && now - pos.entry_t >= holdMs) { pos.exit_reason = "timeout"; pos.exit_ready_ms = pos.entry_t + holdMs + this.s.latency_ms; }
    }
  }

  _fillDue(now) {
    const due = [];
    for (const [m, r] of this.pending) if (r <= now) due.push([r, 0, m]);
    for (const [m, p] of this.positions) if (p.exit_ready_ms !== null && p.exit_ready_ms <= now) due.push([p.exit_ready_ms, 1, m]);
    due.sort((a, b) => a[0] - b[0] || a[1] - b[1] || cmpStr(a[2], b[2]));
    for (const [ready, kind, mint] of due) {
      if (kind === 0) { this.pending.delete(mint); this._open(mint, ready); }
      else { const pos = this.positions.get(mint); this._close(pos, ready, pos.exit_reason); }
    }
  }

  _close(pos, t, reason) {
    const state = this.curve.get(pos.mint), tc = this.s.tx_cost_sol;
    const proceeds = quoteSell(state, pos.tokens, this.s.fee_bps);
    this.cash += proceeds - tc;
    const pnl = proceeds - pos.sol_in - 2 * tc;
    this.positions.delete(pos.mint);
    this.closed.push({ mint: pos.mint, entry_t: pos.entry_t, exit_t: t, sol_in: pos.sol_in, proceeds, pnl_sol: pnl, ret_pct: pnl / pos.sol_in * 100, reason, entry_mcap: pos.entry_mcap, exit_mcap: mcapOf(state) });
    let invested = 0; for (const p of this.positions.values()) invested += p.sol_in;
    this.equity_curve.push(this.cash + invested);
    const r = pnl / pos.sol_in * 100;
    this._note(t, "exit", pos.mint, `sold (${reason}) ${(r >= 0 ? "+" : "") + r.toFixed(1)}% / ${(pnl >= 0 ? "+" : "") + pnl.toFixed(4)} SOL`);
  }
}

/* ---------------- settings (port of sniper/terminal/state.py) ---------------- */
const ALLOWED_VERDICTS = ["PASS", "CAUTION", "AVOID"];
const SIZE_MODES = ["verdict", "random", "fixed"];
const WALLET_RE = /^[1-9A-HJ-NP-Za-km-z]{32,44}$/;
const MAX_BLACKLIST = 200, MAX_ROWS = 80;

function defaultStrategy() { return Object.assign(defaultStrategyFields(), { size_sol: 0.1, size_caution_sol: 0.05, auto_compound: true }); }

function settingsDict(s) {
  return {
    amount_min: s.size_caution_sol !== null ? s.size_caution_sol : s.size_sol, amount_max: s.size_sol,
    take_profit: s.take_profit_pct, stop_loss: s.stop_loss_pct, trailing_stop_pct: s.trailing_stop_pct, slippage_pct: s.slippage_pct,
    max_open: s.max_open, max_dev_buy_pct: s.max_dev_buy_pct, min_liquidity_sol: s.min_liquidity_sol, max_entry_mcap_sol: s.max_entry_mcap_sol,
    size_mode: s.size_mode, auto_compound: s.auto_compound, use_blacklist: s.use_blacklist, blacklist: [...s.blacklist].sort(),
    honeypot_detection: s.require_scan, exit_on_creator_sell: s.exit_on_creator_sell, latency_ms: s.latency_ms, max_hold_s: s.max_hold_s,
    verdicts: [...s.allowed_verdicts].sort(), fee_bps: s.fee_bps, tx_cost_sol: s.tx_cost_sol,
    exit_mode: s.exit_mode, entry_mode: s.entry_mode, confirm_gain_pct: s.confirm_gain_pct, confirm_min_flow_sol: s.confirm_min_flow_sol, confirm_window_s: s.confirm_window_s,
  };
}

const has = (o, k) => Object.prototype.hasOwnProperty.call(o, k);
function num(data, key, label, lo, hi, current) {
  if (!has(data, key)) return current;
  const v = data[key];
  if (typeof v !== "number" || !Number.isFinite(v) || !(lo <= v && v <= hi)) throw new Error(`${label} must be a number between ${lo} and ${hi}`);
  return v;
}

function parseSettings(data, current, hasModel) {
  const cur = settingsDict(current);
  const unknown = Object.keys(data).filter((k) => !has(cur, k)).sort();
  if (unknown.length) throw new Error(`unknown setting: ${unknown[0]}`);
  const lo = num(data, "amount_min", "Minimum amount", 0.001, 100, cur.amount_min);
  const hi = num(data, "amount_max", "Maximum amount", 0.001, 100, cur.amount_max);
  if (lo > hi) throw new Error("Minimum amount can't be larger than the maximum");
  const verdicts = has(data, "verdicts") ? data.verdicts : cur.verdicts;
  if (!Array.isArray(verdicts) || !verdicts.length || verdicts.some((v) => !ALLOWED_VERDICTS.includes(v)))
    throw new Error("verdicts must be a non-empty list drawn from ['PASS', 'CAUTION', 'AVOID']");
  const flags = {};
  for (const [key, label] of [["exit_on_creator_sell", "Rug-pull protection"], ["auto_compound", "Auto-compound"], ["use_blacklist", "Wallet blacklist"], ["honeypot_detection", "Honeypot detection"]]) {
    flags[key] = has(data, key) ? data[key] : cur[key];
    if (typeof flags[key] !== "boolean") throw new Error(`${label} must be true or false`);
  }
  const mode = has(data, "size_mode") ? data.size_mode : cur.size_mode;
  if (!SIZE_MODES.includes(mode)) throw new Error("size_mode must be one of ['verdict', 'random', 'fixed']");
  const exitMode = has(data, "exit_mode") ? data.exit_mode : cur.exit_mode;
  if (!["targets", "smart", "model"].includes(exitMode)) throw new Error("exit_mode must be one of ['targets', 'smart', 'model']");
  if (exitMode === "model" && !hasModel) throw new Error("No learned model is loaded, so the learned exit can't be used");
  const entryMode = has(data, "entry_mode") ? data.entry_mode : cur.entry_mode;
  if (!["snipe", "confirm"].includes(entryMode)) throw new Error("entry_mode must be one of ['snipe', 'confirm']");
  const wallets = has(data, "blacklist") ? data.blacklist : cur.blacklist;
  if (!Array.isArray(wallets) || wallets.length > MAX_BLACKLIST || wallets.some((w) => typeof w !== "string" || !WALLET_RE.test(w)))
    throw new Error(`Blacklist must be a list of up to ${MAX_BLACKLIST} valid wallet addresses`);

  const out = Object.assign({}, current);
  out.size_sol = hi; out.size_caution_sol = lo;
  out.take_profit_pct = num(data, "take_profit", "Take profit", 1, 1000, cur.take_profit);
  out.stop_loss_pct = num(data, "stop_loss", "Stop loss", 1, 99, cur.stop_loss);
  out.trailing_stop_pct = num(data, "trailing_stop_pct", "Trailing stop", 0, 99, cur.trailing_stop_pct);
  out.slippage_pct = num(data, "slippage_pct", "Slippage tolerance", 0.1, 50, cur.slippage_pct);
  out.max_hold_s = num(data, "max_hold_s", "Max hold", 10, 3600, cur.max_hold_s);
  out.max_open = Math.trunc(num(data, "max_open", "Max open positions", 1, 20, cur.max_open));
  out.latency_ms = Math.trunc(num(data, "latency_ms", "Latency", 0, 5000, cur.latency_ms));
  out.fee_bps = num(data, "fee_bps", "Fee", 0, 1000, cur.fee_bps);
  out.tx_cost_sol = num(data, "tx_cost_sol", "Transaction cost", 0, 0.1, cur.tx_cost_sol);
  out.max_dev_buy_pct = num(data, "max_dev_buy_pct", "Max dev hold", 0, 100, cur.max_dev_buy_pct);
  out.min_liquidity_sol = num(data, "min_liquidity_sol", "Min liquidity", 0, 1000, cur.min_liquidity_sol);
  out.max_entry_mcap_sol = num(data, "max_entry_mcap_sol", "Max entry market cap", 1, 1000, cur.max_entry_mcap_sol);
  out.allowed_verdicts = [...new Set(verdicts)];
  out.exit_on_creator_sell = flags.exit_on_creator_sell; out.auto_compound = flags.auto_compound; out.use_blacklist = flags.use_blacklist;
  out.require_scan = flags.honeypot_detection; out.size_mode = mode; out.blacklist = [...new Set(wallets)];
  out.exit_mode = exitMode; out.entry_mode = entryMode;
  out.confirm_gain_pct = num(data, "confirm_gain_pct", "Confirmation gain", 0, 500, cur.confirm_gain_pct);
  out.confirm_min_flow_sol = num(data, "confirm_min_flow_sol", "Confirmation buying", 0, 100, cur.confirm_min_flow_sol);
  out.confirm_window_s = num(data, "confirm_window_s", "Confirmation window", 5, 600, cur.confirm_window_s);
  return out;
}

const PRESETS = {
  steady: { label: "Steady", blurb: "Your 30% cash-out goal: small size, strict filters, tight stop", tag: "RECOMMENDED",
    settings: { amount_min: 0.05, amount_max: 0.1, take_profit: 30, stop_loss: 15, trailing_stop_pct: 0, slippage_pct: 8, max_open: 3, max_dev_buy_pct: 10, verdicts: ["PASS"], max_hold_s: 300, size_mode: "verdict" } },
  safe: { label: "Safe", blurb: "Small size, strict filters, low slippage", tag: "",
    settings: { amount_min: 0.05, amount_max: 0.3, take_profit: 35, stop_loss: 15, trailing_stop_pct: 0, slippage_pct: 5, max_open: 3, max_dev_buy_pct: 10, verdicts: ["PASS"], max_hold_s: 600, size_mode: "verdict" } },
  balanced: { label: "Balanced", blurb: "Good risk/reward for most runs", tag: "",
    settings: { amount_min: 0.1, amount_max: 1.0, take_profit: 50, stop_loss: 25, trailing_stop_pct: 12, slippage_pct: 10, max_open: 5, max_dev_buy_pct: 30, verdicts: ["PASS", "CAUTION"], max_hold_s: 1800, size_mode: "random" } },
  aggressive: { label: "Aggressive", blurb: "Bigger size, higher target, more shots", tag: "",
    settings: { amount_min: 0.3, amount_max: 2.0, take_profit: 120, stop_loss: 40, trailing_stop_pct: 20, slippage_pct: 15, max_open: 5, max_dev_buy_pct: 30, verdicts: ["PASS", "CAUTION"], max_hold_s: 1800, size_mode: "random" } },
  degen: { label: "Degen", blurb: "Moonshots only, relaxed filters, big drawdowns", tag: "",
    settings: { amount_min: 0.5, amount_max: 3.0, take_profit: 300, stop_loss: 60, trailing_stop_pct: 30, slippage_pct: 25, max_open: 8, max_dev_buy_pct: 50, verdicts: ["PASS", "CAUTION", "AVOID"], max_hold_s: 3600, size_mode: "random" } },
};

/* ---------------- terminal state (port of TerminalState) ---------------- */
class TerminalState {
  constructor(strategy, opts) {
    opts = opts || {};
    this.source = opts.source || "demo"; this.simulated = opts.simulated !== false;
    this.start_sol = opts.start_sol === undefined ? 1 : opts.start_sol;
    this.strategy = strategy || defaultStrategy();
    this.model_policy = opts.model ? new ModelExit(opts.model) : null;
    this.sources = opts.sources || []; this.source_id = opts.source_id || null; this.note = opts.note || null;
    this.running = false; this.feed_status = "starting"; this._logSize = opts.log_size || 500;
    this.nowMs = opts.now || (() => Date.now());
    this._resetLocked();
  }
  _resetLocked() {
    this.logs = []; this.log_total = 0; this.tokens = new Map(); this.notes = new Map(); this.closed_by_mint = new Map();
    this.events_seen = 0; this.clock = 0;
    this.engine = new PaperEngine(this.strategy, this.start_sol, (t, k, m, x) => this._onNote(t, k, m, x), this.model_policy);
    this.engine.enabled = this.running;
  }
  reset(startSol) {
    if (startSol !== undefined && startSol !== null) {
      if (typeof startSol !== "number" || !Number.isFinite(startSol) || !(startSol >= 0.1 && startSol <= 1000)) throw new Error("Paper bankroll must be a number between 0.1 and 1000");
      this.start_sol = startSol;
    }
    this._resetLocked();
    this._log(this.nowMs(), "info", "", "paper account reset");
  }
  setRunning(running) {
    this.running = running; this.engine.enabled = running;
    this._log(this.clock || this.nowMs(), "info", "", running ? "bot started" : "bot stopped: no new positions will open");
  }
  updateSettings(data) {
    this.strategy = parseSettings(data, this.strategy, !!this.model_policy); this.engine.s = this.strategy;
    return settingsDict(this.strategy);
  }
  setFeedStatus(status) {
    this.feed_status = status;
    if (status.startsWith("error") || status === "finished") this._log(this.clock || this.nowMs(), "info", "", `feed ${status}`);
  }
  emit(ev) {
    this.events_seen += 1; this.clock = Math.max(this.clock, ev.t);
    if (this.feed_status === "starting") this.feed_status = "live";
    if (ev.type === "new_token") {
      this.tokens.set(ev.mint, { symbol: ev.symbol, name: ev.name, t: ev.t, creator: ev.creator });
      while (this.tokens.size > 400) { const old = this.tokens.keys().next().value; this.tokens.delete(old); this.notes.delete(old); this.closed_by_mint.delete(old); }
      this._log(ev.t, "launch", ev.mint, `${ev.symbol} launched, dev bought ${(ev.dev_tokens / TOTAL_SUPPLY * 100).toFixed(1)}% of supply`);
    } else if (ev.type === "scan" && this.tokens.has(ev.mint)) {
      this._log(ev.t, "scan", ev.mint, `${this.tokens.get(ev.mint).symbol} scanned: ${ev.verdict}` + (ev.summary ? ` (${ev.summary})` : ""));
    }
    this.engine.onEvent(ev);
  }
  _onNote(t, kind, mint, text) {
    const tok = this.tokens.get(mint), sym = tok ? tok.symbol : mint.slice(0, 6);
    if (kind === "skip") this.notes.set(mint, text);
    else if (kind === "exit") this.closed_by_mint.set(mint, this.engine.closed[this.engine.closed.length - 1]);
    this._log(t, kind, mint, `${sym}: ${text}`);
  }
  _log(t, kind, mint, text) {
    this.log_total += 1; this.logs.unshift({ t, kind, mint, text });
    if (this.logs.length > this._logSize) this.logs.length = this._logSize;
  }
  snapshot(win) {
    const e = this.engine;
    const closed = win === "all" ? e.closed : e.closed.filter((c) => c.exit_t >= e.last_t - DAY_MS);
    const wins = closed.filter((c) => c.ret_pct > 0);
    let invested = 0; for (const p of e.positions.values()) invested += p.sol_in;
    const entries = [...this.tokens.entries()].slice(-MAX_ROWS).reverse();
    return {
      running: this.running, source: this.source, simulated: this.simulated, feed_status: this.feed_status, clock: this.clock, events_seen: this.events_seen,
      paper: { start: this.start_sol, equity: e.cash + invested, cash: e.cash, open: e.positions.size },
      settings: settingsDict(this.strategy), presets: PRESETS, has_model: !!this.model_policy, sources: this.sources, source_id: this.source_id,
      stats: { window: win, trades: closed.length, wins: wins.length, win_rate: closed.length ? wins.length / closed.length * 100 : 0, pnl_sol: closed.reduce((a, c) => a + c.pnl_sol, 0) },
      counters: { launches: e.launch.size, scanned: e.scans.size, skipped: Object.assign({}, e.skipped) },
      rows: entries.map(([m, t]) => this._row(m, t)), logs: this.logs.slice(0, 200), log_total: this.log_total,
      equity_curve: e.equity_curve.slice(-300),
    };
  }
  _row(mint, tok) {
    const e = this.engine, curve = e.curve.get(mint);
    const row = { mint, symbol: tok.symbol, name: tok.name, t: tok.t, liquidity_sol: curve ? Math.max(curve.v_sol - VIRTUAL_START_SOL, 0) : null, amount: null, pnl_sol: null, pnl_pct: null };
    const pos = e.positions.get(mint), closed = this.closed_by_mint.get(mint), scan = e.scans.get(mint);
    if (pos) {
      const net = quoteSell(curve, pos.tokens, e.s.fee_bps) - 2 * e.s.tx_cost_sol - pos.sol_in;
      Object.assign(row, { status: pos.exit_ready_ms !== null ? "exiting" : "holding", detail: pos.exit_reason || "", amount: pos.sol_in, pnl_sol: net, pnl_pct: net / pos.sol_in * 100 });
    } else if (closed) Object.assign(row, { status: "closed", detail: closed.reason, amount: closed.sol_in, pnl_sol: closed.pnl_sol, pnl_pct: closed.ret_pct });
    else if (e.pending.has(mint)) Object.assign(row, { status: "entering", detail: "" });
    else if (e.watch.has(mint)) Object.assign(row, { status: "confirming", detail: "" });
    else if (this.notes.has(mint)) { let d = this.notes.get(mint); if (d.startsWith("skipped (")) d = d.slice(9); if (d.endsWith(")")) d = d.slice(0, -1); Object.assign(row, { status: "skipped", detail: d }); }
    else if (scan) Object.assign(row, { status: "watching", detail: scan.verdict });
    else Object.assign(row, { status: "scanning", detail: "" });
    return row;
  }
}

/* ---------------- synthetic demo market (same archetypes as sniper/pump/synth.py; its own random numbers) ---------------- */
const FEE_BPS = 123.8;
class Sim {
  constructor(mint, creator, t) { this.mint = mint; this.creator = creator; this.t = t; this.state = { v_sol: 30, v_tokens: 1073000000 }; }
  _buy(sol) { const tokens = quoteBuy(this.state, sol, FEE_BPS); this.state = { v_sol: this.state.v_sol + sol * (1 - FEE_BPS / 10000), v_tokens: this.state.v_tokens - tokens }; return tokens; }
  create(devSol, name, symbol) {
    const tokens = devSol ? this._buy(devSol) : 0;
    return { type: "new_token", t: this.t, mint: this.mint, creator: this.creator, name, symbol, dev_tokens: tokens, dev_sol: devSol || 0, v_sol: this.state.v_sol, v_tokens: this.state.v_tokens };
  }
  scan(t, verdict) { return { type: "scan", t, mint: this.mint, verdict, score: 0, summary: "" }; }
  buy(t, sol, trader) { const tokens = this._buy(sol); return { type: "trade", t, mint: this.mint, trader, side: "buy", sol, tokens, v_sol: this.state.v_sol, v_tokens: this.state.v_tokens }; }
  sell(t, tokens, trader) {
    const s = this.state, gross = s.v_sol - s.v_sol * s.v_tokens / (s.v_tokens + tokens);
    this.state = { v_sol: s.v_sol - gross, v_tokens: s.v_tokens + tokens };
    return { type: "trade", t, mint: this.mint, trader, side: "sell", sol: gross * (1 - FEE_BPS / 10000), tokens, v_sol: this.state.v_sol, v_tokens: this.state.v_tokens };
  }
}
const ARCHETYPES = [["dead", 0.62], ["rug", 0.18], ["pump", 0.13], ["runner", 0.05], ["grad", 0.02]];
const VERDICTS_RUG = [["REJECT", 0.25], ["AVOID", 0.30], ["CAUTION", 0.25], ["PASS", 0.20]];
const VERDICTS_OTHER = [["PASS", 0.55], ["CAUTION", 0.30], ["AVOID", 0.10], ["REJECT", 0.05]];
const NAMES = ["Moon", "Frog", "Doge", "Pepe", "Cat", "Rocket", "Bonk", "Wif", "Gigachad", "Pump", "Based", "Ape"];
function pick(rng, table) { const r = rng.random(); let acc = 0; for (const [name, p] of table) { acc += p; if (r < acc) return name; } return table[table.length - 1][0]; }

function demoToken(rng, idx, t0) {
  const kind = pick(rng, ARCHETYPES);
  const hex = (n) => rng.bits32().toString(16).padStart(8, "0").slice(0, n);
  const mint = `DEMO${String(idx).padStart(5, "0")}${hex(8)}${hex(2)}`;
  const creator = `DEMOCREATOR${String(Math.floor(rng.random() * 60)).padStart(2, "0")}`;
  const sim = new Sim(mint, creator, t0);
  const devSol = kind === "rug" ? rng.uniform(1, 4) : rng.choice([0, 0, 0.5, 1, 2, 3]);
  const name = `${rng.choice(NAMES)} ${idx}`, symbol = `${rng.choice(NAMES).slice(0, 4).toUpperCase()}${idx % 100}`;
  const launch = sim.create(devSol, name, symbol);
  const verdict = pick(rng, kind === "rug" ? VERDICTS_RUG : VERDICTS_OTHER);
  const events = [launch, sim.scan(t0 + rng.randint(600, 2500), verdict)];
  const others = [0];
  const buy = (atS, sol) => { const tr = sim.buy(t0 + Math.trunc(atS * 1000), sol, "B" + rng.bits32().toString(16).padStart(8, "0")); others[0] += tr.tokens; events.push(tr); };
  const sellOthers = (atS, frac) => { const tokens = others[0] * frac; if (tokens > 0) { others[0] -= tokens; events.push(sim.sell(t0 + Math.trunc(atS * 1000), tokens, "S" + rng.bits32().toString(16).padStart(8, "0"))); } };
  if (kind === "dead") {
    for (let i = rng.randint(4, 12); i > 0; i--) { const at = rng.uniform(3, 60); if (rng.random() < 0.65) buy(at, rng.uniform(0.05, 0.8)); else sellOthers(at, rng.uniform(0.2, 0.5)); }
  } else if (kind === "pump" || kind === "runner") {
    const pump = kind === "pump", lo = pump ? 0.6 : 1.5, hi = pump ? 4 : 6, n = pump ? rng.randint(8, 16) : rng.randint(14, 24), span = pump ? 120 : 200;
    for (let i = 0; i < n; i++) buy(rng.uniform(10, span), rng.uniform(lo, hi));
    for (let k = 0; k < 3; k++) sellOthers(span + 30 + k * 20 + rng.uniform(0, 10), rng.uniform(0.1, 0.25));
  } else if (kind === "rug") {
    for (let i = rng.randint(5, 10); i > 0; i--) buy(rng.uniform(5, 25), rng.uniform(0.5, 3.5));
    if (launch.dev_tokens > 0) events.push(sim.sell(t0 + Math.trunc(rng.uniform(30, 90) * 1000), launch.dev_tokens, creator));
    for (let k = 0; k < 3; k++) sellOthers(95 + k * 4, k === 0 ? 0.8 : 0.9);
  } else {
    let at = 20;
    for (let i = 0; i < 200; i++) { if (isGraduated(sim.state)) break; at += rng.uniform(3, 8); buy(at, rng.uniform(4, 8)); }
  }
  return events;
}
function generateDemo(seed, nTokens, startMs, firstId, meanGapMs) {
  const rng = makeRng(seed), events = []; let t = startMs;
  for (let i = 0; i < nTokens; i++) { t += Math.trunc(rng.expovariate(1 / (meanGapMs || 4000))); events.push(...demoToken(rng, (firstId || 0) + i, t)); }
  return events.map((e, i) => [e, i]).sort((a, b) => a[0].t - b[0].t || a[1] - b[1]).map((x) => x[0]);
}

/* ---------------- endless demo feed, paced by a timer ---------------- */
class DemoFeed {
  constructor(state, opts) {
    opts = opts || {};
    this.state = state; this.speed = opts.speed || 5; this.batch = opts.batch || 25; this.seed = opts.seed || 7; this.maxGapMs = 1000;
    this.now = opts.now || (() => Date.now()); this.queue = []; this.qi = 0; this.target = 0; this.batchNo = 0; this.firstId = 0; this.nextStart = this.now();
  }
  _refill() {
    this.queue = generateDemo(this.seed + this.batchNo, this.batch, this.nextStart, this.firstId);
    this.qi = 0; this.nextStart = this.queue[this.queue.length - 1].t + 1000; this.firstId += this.batch; this.batchNo += 1;
    if (!this.target) this.target = this.queue[0].t;
  }
  tick(realMs) {
    if (this.qi >= this.queue.length) this._refill();
    this.target += realMs * this.speed;
    const next = this.queue[this.qi];
    if (next && next.t - this.target > this.maxGapMs * this.speed) this.target = next.t - this.maxGapMs * this.speed;  // cap idle gaps at 1 s of real time
    while (this.qi < this.queue.length && this.queue[this.qi].t <= this.target) this.state.emit(this.queue[this.qi++]);
  }
}

/* ---------------- replay of a recorded (real) event stream, paced by a timer ---------------- */
class ReplayFeed {
  constructor(state, events, opts) {
    opts = opts || {};
    this.state = state; this.events = events.slice().sort((a, b) => a.t - b.t); this.speed = opts.speed || 20; this.maxGapMs = 1000;
    this.i = 0; this.target = this.events.length ? this.events[0].t : 0; this.done = false;
  }
  tick(realMs) {
    if (this.done) return;
    this.target += realMs * this.speed;
    const next = this.events[this.i];
    if (next && next.t - this.target > this.maxGapMs * this.speed) this.target = next.t - this.maxGapMs * this.speed;  // cap idle gaps at 1 s of real time
    while (this.i < this.events.length && this.events[this.i].t <= this.target) this.state.emit(this.events[this.i++]);
    if (this.i >= this.events.length) { this.done = true; this.state.setFeedStatus("finished"); }
  }
}

/* ---------------- fake /api so the same page runs with no server ---------------- */
function makeApi(app) {
  const json = (code, body) => new Response(JSON.stringify(body), { status: code, headers: { "Content-Type": "application/json" } });
  return async function (url, opts) {
    const u = new URL(url, "http://local"), path = u.pathname, method = ((opts && opts.method) || "GET").toUpperCase();
    const state = app.state;
    if (method === "GET" && path === "/api/state") return json(200, state.snapshot(u.searchParams.get("range") === "all" ? "all" : "24h"));
    if (method !== "POST") return json(404, { error: "not found" });
    let data; try { data = JSON.parse((opts && opts.body) || "{}"); if (data === null || typeof data !== "object" || Array.isArray(data)) throw new Error(); } catch (e) { return json(400, { error: "body must be a JSON object" }); }
    try {
      if (path === "/api/bot") { if (typeof data.running !== "boolean") throw new Error("running must be true or false"); state.setRunning(data.running); return json(200, { running: state.running }); }
      if (path === "/api/settings") return json(200, { settings: state.updateSettings(data) });
      if (path === "/api/source") { app.switchSource(data.id); return json(200, { source_id: app.state.source_id }); }
      if (path === "/api/reset") { state.reset(has(data, "start_sol") ? data.start_sol : undefined); return json(200, { ok: true, start_sol: state.start_sol }); }
    } catch (e) { return json(400, { error: e.message }); }
    return json(404, { error: "not found" });
  };
}

const api = { ReplayFeed, PaperEngine, TerminalState, ModelExit, pathFeatures, tapeFromLaunch, TokenTape, smartDecide, parseSettings, settingsDict, defaultStrategy, defaultStrategyFields, PRESETS, generateDemo, DemoFeed, makeApi, quoteBuy, quoteSell, Sim };
if (typeof module !== "undefined" && module.exports) module.exports = api;
if (typeof window !== "undefined" && root === window) {
  let real = null;
  try { const node = document.getElementById("sniper-real"); if (node) real = JSON.parse(node.textContent); } catch (e) { real = null; }
  const sources = real ? [{ id: "real", label: real.label }, { id: "demo", label: "Simulated market (made up)" }] : [];
  const app = {
    state: null, feed: null,
    switchSource(id) {
      if (id !== "real" && id !== "demo") throw new Error("unknown data source");
      if (id === "real" && !real) throw new Error("no recorded data is embedded in this page");
      const prev = this.state;
      const st = new TerminalState(prev ? prev.strategy : defaultStrategy(), {
        source: id === "real" ? "replay " + real.label : "demo", simulated: id !== "real", start_sol: prev ? prev.start_sol : 1,
        model: real ? real.model : null, sources, source_id: id,
      });
      if (prev && prev.running) st.setRunning(true);
      this.state = st;
      this.feed = id === "real" ? new ReplayFeed(st, real.events) : new DemoFeed(st);
    },
  };
  app.switchSource(real ? "real" : "demo");
  const localApi = makeApi(app), realFetch = window.fetch.bind(window);
  window.fetch = (url, opts) => (typeof url === "string" && url.startsWith("/api/")) ? localApi(url, opts) : realFetch(url, opts);
  setInterval(() => app.feed.tick(100), 100);
  window.__sniperLocal = app;
}
})(typeof window !== "undefined" ? window : globalThis);
