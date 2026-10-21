// End to end: the browser engine against a scripted fake websocket that answers like a rippled server.
// A triangle opens when EUR->XRP gets better, stays open for a ledger, then closes. Exits non-zero on any failure.
"use strict";
const assert = require("assert");
const E = require("../../xrpl_arb/static/engine.js");

const ISS = "rvYAfWj5gh67oV6fW32ZzP3Aw4Eubs59B", ISS2 = "rhub8VRN55s94qWKDv6jmDy1pUykJzF3wq";
const USD = "USD+" + ISS, EUR = "EUR+" + ISS2;
const iou = (id, v) => ({ currency: id.split("+")[0], issuer: id.split("+")[1], value: String(v) });
const amt = (id, v) => (id === "XRP" ? String(Math.round(v * 1e6)) : iou(id, v));
const spec2id = (s) => (s.currency === "XRP" ? "XRP" : s.currency + "+" + s.issuer);

// the fake ledger: books[in>out] = [[pays, gets], ...] (offers selling `out` for `in`)
const EUR_XRP = EUR + ">XRP";
const ledger = { books: { ["XRP>" + USD]: [[100, 50]], [USD + ">" + EUR]: [[50, 45]], [EUR_XRP]: [[45, 90]] }, closed: 100, calls: [] };

class FakeWS {
  constructor(url) {
    FakeWS.instances.push(this); this.url = url; this.sent = [];
    setTimeout(() => this.onopen && this.onopen(), 0);
  }
  push(msg) { this.onmessage({ data: JSON.stringify(msg) }); }
  close() { setTimeout(() => this.onclose && this.onclose(), 0); }
  send(data) {
    const req = JSON.parse(data); this.sent.push(req); ledger.calls.push(req);
    setTimeout(() => this.reply(req), 0);
  }
  reply(req) {
    const ok = (result) => this.push({ id: req.id, status: "success", type: "response", result });
    const fail = (error) => this.push({ id: req.id, status: "error", type: "response", error, error_message: "" });
    if (req.command === "subscribe") return ok({});
    if (req.command === "account_info") return ok({ account_data: {} });
    if (req.command === "ledger") return ok({ ledger_index: ledger.closed });
    if (req.command === "amm_info") return fail("actNotFound");
    if (req.command === "book_offers") {
      const key = spec2id(req.taker_pays) + ">" + spec2id(req.taker_gets);
      const inId = spec2id(req.taker_pays), outId = spec2id(req.taker_gets);
      return ok({ offers: (ledger.books[key] || []).map(([p, g]) => ({ TakerGets: amt(outId, g), TakerPays: amt(inId, p) })) });
    }
    fail("unknownCmd");
  }
}
FakeWS.instances = [];

const waitFor = async (cond, what) => {
  for (let i = 0; i < 400; i++) { if (cond()) return; await new Promise((r) => setTimeout(r, 5)); }
  throw new Error("timed out waiting for " + what);
};
const offerChanged = () => ({ type: "transaction", validated: true, ledger_index: ledger.closed,
  meta: { AffectedNodes: [{ ModifiedNode: { LedgerEntryType: "Offer", FinalFields: { TakerGets: iou(EUR, 45), TakerPays: "90000000" } } }] } });
const close = (ws, n) => { ledger.closed = n; ws.push({ type: "ledgerClosed", ledger_index: n }); };

assert.deepStrictEqual(E.settingsFromHash("#min_profit=0.0001&max_legs=3.4&max_xrp=50"), { min_profit: 0.0001, max_legs: 3, max_xrp: 50 });
assert.deepStrictEqual(E.settingsFromHash("#max_xrp=abc&bogus=1&min_profit=-5&tx_cost=2&max_legs=9&=&x"), {}, "bad input is ignored");
assert.deepStrictEqual(E.settingsFromHash(""), {});

(async () => {
  const pairs = [["XRP", USD], [USD, EUR], [EUR, "XRP"]];
  const updates = [];
  const eng = E.create({ pairs, WebSocket: FakeWS, settings: { settle_ms: 5, tx_cost: 0.00002, max_xrp: 1000, full_every: 1000, urls: ["wss://fake/"] },
                         onUpdate: () => updates.push(1) });
  eng.start();
  await waitFor(() => eng.snapshot().ledgers.length === 1, "the first full read");
  const ws = FakeWS.instances[0];
  assert.deepStrictEqual(ws.sent[0], { id: 1, command: "subscribe", streams: ["ledger", "transactions"] });
  let s = eng.snapshot();
  assert.strictEqual(s.status.state, "connected");
  assert.deepStrictEqual([s.ledgers[0].ledger, s.ledgers[0].full, s.ledgers[0].pairs_read, s.ledgers[0].opportunities], [100, true, 3, 0]);
  assert.strictEqual(s.markets.length, 3);
  assert.ok(s.markets.every((m) => m.changed === null), "reading at the start is not a change");

  // ledger 101: EUR->XRP now pays 2.5, so XRP -> USD -> EUR -> XRP returns 12.5%
  ledger.books[EUR_XRP] = [[45, 112.5]];
  const before = ledger.calls.length;
  ws.push(offerChanged()); close(ws, 101);
  await waitFor(() => eng.snapshot().ledgers.length === 2, "ledger 101");
  s = eng.snapshot();
  const reads = ledger.calls.slice(before).filter((c) => c.command === "amm_info" || c.command === "book_offers");
  assert.strictEqual(reads.length, 3, "one pair re-read: its pool and two books");
  assert.ok(reads.every((c) => c.ledger_index === 101), "reads are pinned to the closed ledger");
  assert.deepStrictEqual([s.ledgers[1].pairs_read, s.ledgers[1].opportunities, s.kpis.live, s.kpis.seen], [1, 1, 1, 1]);
  assert.deepStrictEqual(s.live[0].labels, ["XRP", "USD", "EUR", "XRP"]);
  assert.ok(Math.abs(s.live[0].profit - 12.5) < 1e-3, "profit " + s.live[0].profit);
  assert.ok(s.markets.find((m) => m.a === "EUR" && m.b === "XRP").changed === 101);

  close(ws, 102);                                   // nothing changed: still open, nothing re-read
  await waitFor(() => eng.snapshot().ledgers.length === 3, "ledger 102");
  s = eng.snapshot();
  assert.deepStrictEqual([s.ledgers[2].pairs_read, s.ledgers[2].opportunities, s.live[0].first_ledger, s.live[0].last_ledger], [0, 1, 101, 102]);

  ledger.books[EUR_XRP] = [[45, 90]];             // back to 2.0: it closes
  ws.push(offerChanged()); close(ws, 103);
  await waitFor(() => eng.snapshot().ledgers.length === 4, "ledger 103");
  s = eng.snapshot();
  assert.strictEqual(s.kpis.live, 0);
  assert.deepStrictEqual([s.ended.length, s.ended[0].ledgers_alive, s.ended[0].first_ledger, s.kpis.median_lifetime], [1, 2, 101, 2]);
  assert.ok(s.log.some((l) => /^new \+12\./.test(l.text)) && s.log.some((l) => /ended after 2 ledger/.test(l.text)));
  assert.doesNotThrow(() => JSON.stringify(s));      // the page gets plain data
  assert.ok(updates.length >= 4, "the page is told after every ledger");

  // a dropped connection reconnects and reads everything again
  ws.onclose();
  await waitFor(() => FakeWS.instances.length === 2, "a second connection");
  await waitFor(() => eng.snapshot().ledgers.length === 5, "the re-read after reconnecting");
  s = eng.snapshot();
  assert.deepStrictEqual([s.ledgers[4].full, s.ledgers[4].pairs_read, s.status.state], [true, 3, "connected"]);
  eng.stop();
  console.log("engine ok");
  process.exit(0);
})().catch((e) => { console.error(e); process.exit(1); });
