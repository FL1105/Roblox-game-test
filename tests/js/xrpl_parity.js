// Runs the browser engine on cases that tests/test_xrpl_js_parity.py also runs through the Python package.
// Reads {labels, quotes, cycles, stream} as JSON on stdin and prints the engine's answers as JSON.
"use strict";
const fs = require("fs");
const E = require("../../xrpl_arb/static/engine.js");

const input = JSON.parse(fs.readFileSync(0, "utf8"));

function mk(spec) {
  const pool = spec.pool ? { a: spec.pool.a, b: spec.pool.b, ra: spec.pool.ra, rb: spec.pool.rb, fee: spec.pool.fee, account: null } : null;
  return E.market(spec.in, spec.out, spec.offers.map(([pays, gets]) => ({ pays, gets })), pool, spec.rate);
}

const out = {};
out.labels = input.labels.map(E.labelOf);
out.quotes = input.quotes.map((c) => c.sizes.map((s) => E.quote(mk(c.market), s)[0]));
out.spot = input.quotes.map((c) => E.spotRate(mk(c.market)));

out.cycles = input.cycles.map((c) => {
  const markets = new Map();
  for (const spec of c.markets) markets.set(spec.in + ">" + spec.out, mk(spec));
  return E.candidates(markets, { maxXrp: c.max_xrp, txCost: c.tx_cost, maxLegs: c.max_legs })
    .map((o) => ({ path: o.path, amountIn: o.amountIn, amountOut: o.amountOut, profit: o.profit }));
});

const idx = new E.PairIndex(input.stream.pairs);
for (const [acct, key] of Object.entries(input.stream.amm_accounts)) idx.byAmmAccount.set(acct, key);
out.touched = input.stream.messages.map((m) => Array.from(idx.touched(m)).sort());
out.touched_synthetic = input.synthetic.map((m) => Array.from(idx.touched(m)).sort());

process.stdout.write(JSON.stringify(out));
