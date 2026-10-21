/* The xrpl_arb package in plain JavaScript, so a browser can do it all itself: read the XRP Ledger over a websocket,
   quote swaps against order books and AMM pools, search XRP cycles, and follow each opportunity from the ledger it
   appears in until it disappears. Read-only: it only asks the ledger questions, it has no key and sends nothing but
   requests for data. Same algorithms as the Python code (liquidity.py, cycles.py, stream.py, ui.py); numbers are
   float64 instead of Decimal, and tests/test_xrpl_js_parity.py checks the two agree. */
(function (root) {
  "use strict";

  const DROP = 1e-6;
  const GOLDEN = (Math.sqrt(5) - 1) / 2;
  const FEE_UNITS = 100000;
  const SLOT_EXPIRED = 20;

  // ---------------------------------------------------------------- assets
  // An asset is identified by one string: "XRP", or "<currency>+<issuer>" with a 3-letter code or 40 hex digits.
  function labelOf(id) {
    if (id === "XRP") return "XRP";
    const cur = id.split("+")[0];
    if (cur.length !== 40) return cur;
    const bytes = [];
    for (let i = 0; i < 40; i += 2) bytes.push(parseInt(cur.slice(i, i + 2), 16));
    while (bytes.length && bytes[bytes.length - 1] === 0) bytes.pop();   // zero padding
    if (!bytes.length || bytes.some((b) => b < 32 || b > 126)) return cur;
    return String.fromCharCode.apply(null, bytes);
  }
  function pairKey(a, b) { return a < b ? a + "|" + b : b + "|" + a; }
  function rawId(amount) {
    if (typeof amount === "string") return "XRP";
    const cur = amount.currency;
    return (cur.length === 40 ? cur.toUpperCase() : cur) + "+" + amount.issuer;
  }
  function amountValue(a) { return typeof a === "string" ? Number(a) / 1e6 : Number(a.value); }
  function floorDrop(x) { return Math.floor(x * 1e6 + 1e-7) / 1e6; }
  function spec(id) {
    if (id === "XRP") return { currency: "XRP" };
    const i = id.indexOf("+");
    return { currency: id.slice(0, i), issuer: id.slice(i + 1) };
  }

  // ---------------------------------------------------------------- liquidity (liquidity.py)
  function reserves(pool, inId) { return inId === pool.a ? [pool.ra, pool.rb] : [pool.rb, pool.ra]; }
  function poolSwap(pool, inId, dx) {
    const [x, y] = reserves(pool, inId), m = 1 - pool.fee;
    const dy = y * m * dx / (x + m * dx);
    const nx = x + dx, ny = y - dy;
    const next = inId === pool.a ? { ...pool, ra: nx, rb: ny } : { ...pool, ra: ny, rb: nx };
    return [dy, next];
  }
  // input after which the pool's spot price (in per out) equals `price`; 0 if it is already there
  function ammInToPrice(x, y, m, price) {
    if (price * m * y <= x) return 0;
    const root = Math.sqrt((1 - m) * (1 - m) * x * x + 4 * m * m * price * x * y);
    return (root - (1 + m) * x) / (2 * m);
  }
  function market(inId, outId, offers, pool, rate) { return { inId, outId, offers: offers || [], pool: pool || null, rate: rate || 1 }; }
  function marketEmpty(m) { return m.offers.length === 0 && m.pool === null; }
  function poolId(m) { return pairKey(m.inId, m.outId); }
  function spotRate(m) {
    let best = 0;
    if (m.offers.length) best = m.offers[0].gets / m.offers[0].pays;
    if (m.pool) { const [x, y] = reserves(m.pool, m.inId); best = Math.max(best, y * (1 - m.pool.fee) / x); }
    return best / m.rate;
  }
  // output for spending `grossIn`, and the pool afterwards; `poolOverride` carries an earlier leg's trade forward
  function quote(m, grossIn, poolOverride) {
    let pool = poolOverride ? poolOverride : m.pool;
    let remaining = grossIn / m.rate, out = 0;
    if (remaining <= 0) return [0, pool];
    for (const o of m.offers) {
      if (pool) {
        const [x, y] = reserves(pool, m.inId);
        const dx = Math.min(ammInToPrice(x, y, 1 - pool.fee, o.pays / o.gets), remaining);
        if (dx > 0) {
          const [dy, next] = poolSwap(pool, m.inId, dx);
          pool = next; out += dy; remaining -= dx;
          if (remaining <= 0) return [out, pool];
        }
      }
      const take = Math.min(o.pays, remaining);
      out += o.gets * take / o.pays; remaining -= take;
      if (remaining <= 0) return [out, pool];
    }
    if (pool) { const [dy, next] = poolSwap(pool, m.inId, remaining); out += dy; pool = next; }
    return [out, pool];
  }

  // ---------------------------------------------------------------- cycles (cycles.py)
  function findCycles(markets, base, maxLegs) {
    const edges = new Map();
    for (const m of markets.values()) {
      if (marketEmpty(m)) continue;
      if (!edges.has(m.inId)) edges.set(m.inId, []);
      edges.get(m.inId).push(m.outId);
    }
    const cycles = [];
    const walk = (visited, legs) => {
      const cur = visited[visited.length - 1];
      for (const nxt of edges.get(cur) || []) {
        const m = markets.get(cur + ">" + nxt);
        if (nxt === base) cycles.push(legs.concat([m]));
        else if (!visited.includes(nxt) && legs.length + 1 < maxLegs) walk(visited.concat([nxt]), legs.concat([m]));
      }
    };
    walk([base], []);
    return cycles;
  }
  function runCycle(cycle, amountIn, legs, wholeDrops) {
    if (wholeDrops === undefined) wholeDrops = true;
    let amt = amountIn;
    const pools = new Map();
    for (const m of cycle) {
      const [out0, after] = quote(m, amt, pools.get(poolId(m)));
      if (after) pools.set(poolId(m), after);
      const out = wholeDrops && m.outId === "XRP" ? floorDrop(out0) : out0;
      if (legs) legs.push({ inId: m.inId, outId: m.outId, amountIn: amt, amountOut: out });
      amt = out;
    }
    return amt;
  }
  function bestSize(cycle, maxIn) {
    const profit = (x) => runCycle(cycle, x, null, false) - x;
    let a = 0, b = maxIn;
    let c = b - GOLDEN * (b - a), d = a + GOLDEN * (b - a);
    let fc = profit(c), fd = profit(d);
    for (let i = 0; i < 200; i++) {
      if (b - a <= DROP) break;
      if (fc < fd) { a = c; c = d; fc = fd; d = a + GOLDEN * (b - a); fd = profit(d); }
      else { b = d; d = c; fd = fc; c = b - GOLDEN * (b - a); fc = profit(c); }
    }
    const x = floorDrop((a + b) / 2);
    return x > 0 && runCycle(cycle, x) - x > 0 ? x : 0;
  }
  function candidates(markets, o) {
    const found = [];
    for (const cycle of findCycles(markets, "XRP", o.maxLegs)) {
      let rate = 1;
      for (const m of cycle) rate *= spotRate(m);
      if (rate <= 1) continue;   // profit is concave with slope rate-1 at 0, so nothing to find
      const x = bestSize(cycle, o.maxXrp);
      if (x <= 0) continue;
      const legs = [];
      const out = runCycle(cycle, x, legs);
      const profit = out - x - o.txCost;
      found.push({ legs, amountIn: x, amountOut: out, txCost: o.txCost, profit, profitPct: profit / x * 100,
                   path: [cycle[0].inId].concat(cycle.map((m) => m.outId)) });
    }
    found.sort((p, q) => q.profit - p.profit);
    return found;
  }
  const passes = (opp, minProfit, minPct) => opp.profit >= minProfit && opp.profitPct >= minPct;

  // ---------------------------------------------------------------- reading the ledger (snapshot.py)
  function parseOffers(result) {
    const offers = [];
    for (const o of result.offers || []) {
      const getsFull = amountValue(o.TakerGets), paysFull = amountValue(o.TakerPays);
      if (!(getsFull > 0)) continue;
      const gets = o.taker_gets_funded !== undefined ? amountValue(o.taker_gets_funded) : getsFull;
      const pays = o.taker_pays_funded !== undefined ? amountValue(o.taker_pays_funded) : paysFull * gets / getsFull;
      if (gets > 0 && pays > 0) offers.push({ pays, gets });
    }
    offers.sort((p, q) => p.pays / p.gets - q.pays / q.gets);
    return offers;
  }
  function poolFee(amm, account) {
    let units = amm.trading_fee;
    const slot = amm.auction_slot || {};
    const members = new Set([slot.account].concat((slot.auth_accounts || []).map((x) => x.account)));
    const live = (slot.time_interval === undefined ? SLOT_EXPIRED : slot.time_interval) < SLOT_EXPIRED;
    if (account && members.has(account) && live) units = slot.discounted_fee;
    return units / FEE_UNITS;
  }
  function parsePool(result, a, b, account) {
    const amm = result.amm;
    const a1 = rawId(amm.amount), a2 = rawId(amm.amount2);
    if (pairKey(a1, a2) !== pairKey(a, b)) throw { code: "badPool", message: "amm_info returned " + a1 + "/" + a2 };
    const r1 = amountValue(amm.amount), r2 = amountValue(amm.amount2);
    if (!(r1 > 0 && r2 > 0)) return null;
    return { a: a1, b: a2, ra: r1, rb: r2, fee: poolFee(amm, account), account: amm.account || null };
  }

  // ---------------------------------------------------------------- the stream (stream.py)
  class PairIndex {
    constructor(pairs) {
      this.pairs = [];
      const seen = new Set();
      for (const [a, b] of pairs) { const k = pairKey(a, b); if (!seen.has(k)) { seen.add(k); this.pairs.push([a, b]); } }
      this.byAmmAccount = new Map();
      this.keys = seen;
    }
    learnAccounts(pools) { for (const [k, p] of pools) if (p && p.account) this.byAmmAccount.set(p.account, k); }
    // pairs whose books or pool a validated transaction changed, from its metadata
    touched(msg) {
      const hit = new Set();
      const nodes = (msg.meta && msg.meta.AffectedNodes) || [];
      for (const node of nodes) {
        const body = node.ModifiedNode || node.CreatedNode || node.DeletedNode;
        if (!body) continue;
        const f = body.FinalFields || body.NewFields || {};
        if (body.LedgerEntryType === "Offer") {
          if (f.TakerGets !== undefined && f.TakerPays !== undefined) {
            const k = pairKey(rawId(f.TakerGets), rawId(f.TakerPays));
            if (this.keys.has(k)) hit.add(k);
          }
        } else if (body.LedgerEntryType === "AccountRoot") {
          const k = this.byAmmAccount.get(f.Account);
          if (k) hit.add(k);
        } else if (body.LedgerEntryType === "RippleState") {
          for (const side of ["HighLimit", "LowLimit"]) {
            const k = this.byAmmAccount.get((f[side] || {}).issuer);
            if (k) hit.add(k);
          }
        }
      }
      return hit;
    }
  }

  class Lifetimes {
    constructor() { this.live = new Map(); }
    update(ledger, opps) {
      const now = new Map(opps.map((o) => [o.path.join("|"), o]));
      const fresh = [];
      for (const [k, o] of now) {
        let rec = this.live.get(k);
        if (!rec) { rec = { first: ledger, best: o.profit, bestIn: o.amountIn }; this.live.set(k, rec); fresh.push(o); }
        rec.last = ledger;
        if (o.profit > rec.best) { rec.best = o.profit; rec.bestIn = o.amountIn; }
      }
      const ended = [];
      for (const k of Array.from(this.live.keys())) {
        if (now.has(k)) continue;
        const rec = this.live.get(k); this.live.delete(k);
        ended.push({ path: k.split("|"), first_ledger: rec.first, last_ledger: rec.last, ledgers_alive: rec.last - rec.first + 1,
                     best_profit: rec.best, best_amount_in: rec.bestIn });
      }
      return [fresh, ended];
    }
  }

  // A websocket speaking rippled's request/response protocol, with a cap on requests in flight.
  class Rpc {
    constructor(url, WS, onStream, onClose, maxInFlight) {
      this.url = url; this.WS = WS; this.onStream = onStream; this.onClose = onClose;
      this.max = maxInFlight || 12; this.nextId = 1; this.pending = new Map(); this.queue = []; this.inFlight = 0; this.closed = false;
    }
    open() {
      return new Promise((resolve, reject) => {
        let opened = false;
        const ws = (this.ws = new this.WS(this.url));
        ws.onopen = () => { opened = true; resolve(); };
        ws.onerror = () => { if (!opened) reject({ code: "connect", message: "could not open " + this.url }); };
        ws.onclose = () => {
          this.closed = true;
          for (const p of this.pending.values()) { clearTimeout(p.timer); p.reject({ code: "closed", message: "connection closed" }); }
          this.pending.clear(); this.queue.length = 0;
          if (!opened) reject({ code: "connect", message: "could not open " + this.url }); else this.onClose();
        };
        ws.onmessage = (ev) => {
          let msg; try { msg = JSON.parse(typeof ev.data === "string" ? ev.data : String(ev.data)); } catch (e) { return; }
          if (msg.id !== undefined && this.pending.has(msg.id)) {
            const p = this.pending.get(msg.id); this.pending.delete(msg.id); clearTimeout(p.timer); this.inFlight--;
            if (msg.status === "error") p.reject({ code: msg.error || "unknown", message: msg.error_message || "" });
            else p.resolve(msg.result !== undefined ? msg.result : msg);
            this.pump();
          } else if (this.onStream) this.onStream(msg);
        };
      });
    }
    request(command, params) {
      return new Promise((resolve, reject) => { this.queue.push({ command, params, resolve, reject }); this.pump(); });
    }
    pump() {
      while (!this.closed && this.inFlight < this.max && this.queue.length) {
        const job = this.queue.shift(), id = this.nextId++;
        this.inFlight++;
        const timer = setTimeout(() => {
          if (this.pending.delete(id)) { this.inFlight--; job.reject({ code: "timeout", message: job.command + " timed out" }); this.pump(); }
        }, 20000);
        this.pending.set(id, { resolve: job.resolve, reject: job.reject, timer });
        this.ws.send(JSON.stringify({ id, command: job.command, ...job.params }));
      }
    }
    close() { this.closed = true; try { this.ws.close(); } catch (e) { /* already closed */ } }
  }

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const median = (a) => { const s = a.slice().sort((x, y) => x - y), n = s.length; return n ? (n % 2 ? s[(n - 1) / 2] : (s[n / 2 - 1] + s[n / 2]) / 2) : null; };

  // ---------------------------------------------------------------- the engine (stream.py + ui.py)
  const DEFAULTS = {
    min_profit: 0.001, min_profit_pct: 0.25, tx_cost: 0.0001, max_legs: 4, max_xrp: 25, account: null,
    settle_ms: 350, full_every: 20, book_limit: 60, urls: ["wss://xrplcluster.com/", "wss://s1.ripple.com/"],
  };

  // Settings from the address bar, e.g. #min_profit=0.0001&max_xrp=50&max_legs=3. Unknown or out-of-range values are ignored.
  const LIMITS = { min_profit: [0, 100], min_profit_pct: [-100, 100], tx_cost: [0, 1], max_legs: [2, 5], max_xrp: [0.01, 100000] };
  function settingsFromHash(hash) {
    const out = {};
    for (const part of String(hash || "").replace(/^#/, "").split("&")) {
      const [k, v] = part.split("=");
      if (!LIMITS[k] || v === undefined || v === "") continue;
      const n = Number(v);
      if (!Number.isFinite(n) || n < LIMITS[k][0] || n > LIMITS[k][1]) continue;
      out[k] = k === "max_legs" ? Math.round(n) : n;
    }
    return out;
  }

  function create(options) {
    const S = { ...DEFAULTS, ...(options.settings || {}) };
    const WS = options.WebSocket || root.WebSocket;
    const now = options.now || (() => Date.now());
    const onUpdate = options.onUpdate || (() => {});
    const index = new PairIndex(options.pairs);
    const rates = new Map();       // issuer -> transfer rate
    const pools = new Map(), books = new Map(), changedAt = new Map(), markets = new Map();
    const tracker = new Lifetimes();
    const dash = { started: now(), status: { state: "starting", detail: "", attempts: 0 }, ledgers: [], live: new Map(),
                   ended: [], log: [], seen: 0, bestSeen: null, lastTop: null };
    let stopped = false, rpc = null, attempt = 0;

    const ledgerMax = 600, endedMax = 300, logMax = 200;
    const log = (ledger, text) => { dash.log.unshift({ t: Math.round(now() / 100) / 10, ledger, text }); if (dash.log.length > logMax) dash.log.pop(); };
    const setStatus = (state, detail, attempts) => { dash.status = { state, detail, attempts: attempts || 0 }; log(null, "stream " + state + ": " + detail); onUpdate(); };
    const labels = (path) => path.map(labelOf);

    async function readRates() {
      const issuers = new Set();
      for (const [a, b] of index.pairs) for (const id of [a, b]) if (id !== "XRP") issuers.add(id.slice(id.indexOf("+") + 1));
      await Promise.all(Array.from(issuers).filter((i) => !rates.has(i)).map(async (iss) => {
        try {
          const r = await rpc.request("account_info", { account: iss, ledger_index: "validated" });
          rates.set(iss, (r.account_data.TransferRate === undefined ? 1e9 : Number(r.account_data.TransferRate)) / 1e9);
        } catch (e) { rates.set(iss, 1); }
      }));
    }
    const rateOf = (id) => (id === "XRP" ? 1 : rates.get(id.slice(id.indexOf("+") + 1)) || 1);

    async function pinned(command, params, ledger) {
      try { return [await rpc.request(command, { ...params, ledger_index: ledger }), true]; }
      catch (e) {
        if (e.code !== "lgrNotFound") throw e;
        return [await rpc.request(command, { ...params, ledger_index: "validated" }), false];
      }
    }
    // Re-read the dirty pairs (all if null) at `ledger`; returns {skipped, consistent, count}
    async function refresh(dirty, ledger) {
      const pairs = dirty === null ? index.pairs : index.pairs.filter(([a, b]) => dirty.has(pairKey(a, b)));
      const skipped = []; let consistent = true;
      const fresh = new Map();
      await Promise.all(pairs.map(async ([a, b]) => {
        const k = pairKey(a, b);
        let pool = null;
        try {
          const [res, ok] = await pinned("amm_info", { asset: spec(a), asset2: spec(b) }, ledger);
          consistent = consistent && ok;
          pool = parsePool(res, a, b, S.account);
        } catch (e) {
          if (e.code === "badPool") skipped.push("pool " + labelOf(a) + "/" + labelOf(b) + ": " + e.message);
          else if (e.code !== "actNotFound") {
            if (e.code === "closed" || e.code === "timeout" || e.code === "connect") throw e;
            skipped.push("pool " + labelOf(a) + "/" + labelOf(b) + ": " + (e.code || e.message || e));
          }
        }
        const sides = {};
        for (const [x, y] of [[a, b], [b, a]]) {   // offers selling y for x
          try {
            const [res, ok] = await pinned("book_offers", { taker_gets: spec(y), taker_pays: spec(x), limit: S.book_limit }, ledger);
            consistent = consistent && ok;
            sides[x + ">" + y] = parseOffers(res);
          } catch (e) {
            if (e.code === "closed" || e.code === "timeout" || e.code === "connect") throw e;
            skipped.push("book " + labelOf(x) + "->" + labelOf(y) + ": " + (e.code || e.message || e));
            sides[x + ">" + y] = [];
          }
        }
        fresh.set(k, { pool, sides, a, b });
      }));
      for (const [k, f] of fresh) {
        const prev = pools.has(k) ? { pool: pools.get(k), ab: books.get(f.a + ">" + f.b), ba: books.get(f.b + ">" + f.a) } : null;
        if (prev && (JSON.stringify(prev.pool) !== JSON.stringify(f.pool) || JSON.stringify(prev.ab) !== JSON.stringify(f.sides[f.a + ">" + f.b])
            || JSON.stringify(prev.ba) !== JSON.stringify(f.sides[f.b + ">" + f.a]))) changedAt.set(k, ledger);   // the first read is not a change
        pools.set(k, f.pool);
        for (const e in f.sides) books.set(e, f.sides[e]);
        for (const [x, y] of [[f.a, f.b], [f.b, f.a]]) markets.set(x + ">" + y, market(x, y, books.get(x + ">" + y), f.pool, rateOf(x)));
      }
      index.learnAccounts(pools);
      return { skipped, consistent, count: pairs.length };
    }

    let sinceFull = 0;
    async function handleLedger(ledger, closedAt, dirty, full) {
      const began = now();
      const reads = await refresh(full ? null : dirty, typeof ledger === "number" ? ledger : "validated");
      const readDone = now();
      const cands = candidates(markets, { maxXrp: S.max_xrp, txCost: S.tx_cost, maxLegs: S.max_legs });
      const opps = cands.filter((o) => passes(o, S.min_profit, S.min_profit_pct));
      const [fresh, ended] = tracker.update(ledger, opps);
      const evalDone = now();
      sinceFull = full ? 0 : sinceFull + 1;
      const top = opps[0] || cands[0] || null;
      dash.ledgers.push({
        ledger, t: Math.round(now() / 100) / 10, full, pairs_read: reads.count, after_close_s: Math.round(evalDone - closedAt) / 1000,
        read_s: Math.round(readDone - began) / 1000, candidates: cands.length, opportunities: opps.length, skipped: reads.skipped.length,
        best_gross_xrp: cands.length ? cands[0].profit + cands[0].txCost : null, best_labels: top ? labels(top.path) : null,
      });
      if (dash.ledgers.length > ledgerMax) dash.ledgers.shift();
      const seenNow = new Set();
      for (const o of opps) {
        const key = o.path.join("|"); seenNow.add(key);
        const prev = dash.live.get(key);
        const view = { key, labels: labels(o.path), first_ledger: prev ? prev.first_ledger : ledger, last_ledger: ledger, amount_in: o.amountIn,
                       amount_out: o.amountOut, profit: o.profit, profit_pct: o.profitPct,
                       legs: o.legs.map((l) => ({ in: labelOf(l.inId), out: labelOf(l.outId), amount_in: l.amountIn, amount_out: l.amountOut })) };
        dash.live.set(key, view);
        if (!dash.bestSeen || view.profit > dash.bestSeen.profit) dash.bestSeen = { labels: view.labels, profit: view.profit, profit_pct: view.profit_pct, amount_in: view.amount_in, ledger };
      }
      for (const o of fresh) { dash.seen++; log(ledger, "new " + (o.profit >= 0 ? "+" : "") + o.profit.toFixed(5) + " XRP (" + (o.profitPct >= 0 ? "+" : "") + o.profitPct.toFixed(2) + "%) " + labels(o.path).join(" → ")); }
      for (const e of ended) {
        dash.live.delete(e.path.join("|"));
        const row = { labels: labels(e.path), first_ledger: e.first_ledger, last_ledger: e.last_ledger, ledgers_alive: e.ledgers_alive, best_profit: e.best_profit, best_amount_in: e.best_amount_in };
        dash.ended.unshift(row); if (dash.ended.length > endedMax) dash.ended.pop();
        log(ledger, "ended after " + e.ledgers_alive + " ledger(s), best " + (e.best_profit >= 0 ? "+" : "") + e.best_profit.toFixed(5) + " XRP: " + row.labels.join(" → "));
      }
      onUpdate();
    }

    async function session(url) {
      let dirty = new Set(), latest = null, timer = null, processing = false, first = true, done = null;
      const closed = new Promise((r) => { done = r; });
      const kick = async () => {
        timer = null;
        if (!latest || processing || stopped) return;
        const job = latest; latest = null;
        const d = dirty; dirty = new Set();
        processing = true;
        try { await handleLedger(job.ledger, job.closedAt, d, first || sinceFull >= S.full_every); first = false; }
        catch (e) { if (e && (e.code === "closed" || e.code === "timeout")) { done(); return; } log(job.ledger, "read failed: " + (e.code || e.message || e)); }
        processing = false;
        if (latest && !timer) timer = setTimeout(kick, 0);
      };
      rpc = new Rpc(url, WS, (msg) => {
        if (msg.type === "ledgerClosed") { latest = { ledger: msg.ledger_index, closedAt: now() }; if (!timer && !processing) timer = setTimeout(kick, S.settle_ms); }
        else if (msg.type === "transaction" && msg.validated) { for (const k of index.touched(msg)) dirty.add(k); }
      }, () => done(), 12);
      await rpc.open();
      await rpc.request("subscribe", { streams: ["ledger", "transactions"] });
      attempt = 0;
      setStatus("connected", url, 0);
      await readRates();
      const lr = await rpc.request("ledger", { ledger_index: "validated" });   // fill the page now, not at the next close
      latest = { ledger: Number(lr.ledger_index), closedAt: now() };
      timer = setTimeout(kick, 0);
      await closed;
      clearTimeout(timer);
      rpc.close();
    }

    async function run() {
      while (!stopped) {
        const url = S.urls[attempt % S.urls.length];
        try { await session(url); attempt = stopped ? attempt : attempt + 1; if (!stopped) setStatus("reconnecting", "the connection closed", attempt); }
        catch (e) { attempt++; if (!stopped) setStatus("reconnecting", (e && (e.message || e.code)) || String(e), attempt); }
        if (!stopped) await sleep(Math.min(1000 * Math.pow(2, Math.min(attempt, 4)), 20000));
      }
    }

    function marketRows() {
      const inOpp = new Set();
      for (const o of dash.live.values()) for (const l of o.legs) inOpp.add(pairKey(l.in, l.out));
      const last = dash.ledgers.length ? dash.ledgers[dash.ledgers.length - 1].ledger : null;
      return index.pairs.map(([a, b]) => {
        const k = pairKey(a, b), pool = pools.get(k) || null;
        const row = { a: labelOf(a), b: labelOf(b), offers_ab: (books.get(a + ">" + b) || []).length, offers_ba: (books.get(b + ">" + a) || []).length, pool: null,
                      changed: changedAt.has(k) ? changedAt.get(k) : null, in_opportunity: inOpp.has(pairKey(labelOf(a), labelOf(b))) };
        if (pool) {
          const [ra, rb] = pool.a === a ? [pool.ra, pool.rb] : [pool.rb, pool.ra];
          row.pool = { reserve_a: ra, reserve_b: rb, fee_pct: pool.fee * 100, price: rb ? ra / rb : null,
                       tvl_xrp: a === "XRP" ? 2 * ra : b === "XRP" ? 2 * rb : null };
        }
        return row;
      });
    }
    function snapshot() {
      const recent = dash.ledgers.slice(-100).filter((r) => !r.full).map((r) => r.after_close_s);
      const lifetimes = dash.ended.map((e) => e.ledgers_alive);
      const last = dash.ledgers.length ? dash.ledgers[dash.ledgers.length - 1].ledger : null;
      return {
        replay: false, now: Math.round(now() / 100) / 10,
        status: { ...dash.status, uptime_s: Math.round((now() - dash.started) / 1000), ledger: last },
        settings: { min_profit: S.min_profit, min_profit_pct: S.min_profit_pct, tx_cost: S.tx_cost, max_legs: S.max_legs, max_xrp: S.max_xrp, account: S.account },
        kpis: { live: dash.live.size, seen: dash.seen, ended: dash.ended.length, median_lifetime: median(lifetimes), best_seen: dash.bestSeen,
                ledgers: dash.ledgers.length, median_after_close_s: median(recent),
                p90_after_close_s: recent.length ? recent.slice().sort((x, y) => x - y)[Math.floor(recent.length * 0.9)] : null },
        ledgers: dash.ledgers.slice(), live: Array.from(dash.live.values()).sort((p, q) => q.profit - p.profit),
        ended: dash.ended.slice(), markets: marketRows(), log: dash.log.slice(),
      };
    }
    return { start() { run(); }, stop() { stopped = true; if (rpc) rpc.close(); }, snapshot, _internals: { index, markets, pools, books, refresh } };
  }

  const api = {
    labelOf, pairKey, rawId, amountValue, floorDrop, market, quote, spotRate, ammInToPrice, poolSwap, findCycles, runCycle, bestSize, candidates,
    passes, parseOffers, parsePool, PairIndex, Lifetimes, Rpc, create, settingsFromHash, DEFAULTS,
  };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  root.XrplEngine = api;
})(typeof globalThis !== "undefined" ? globalThis : this);
