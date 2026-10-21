"""The browser engine (xrpl_arb/static/engine.js) must give the same answers as the Python package it was ported from."""

import json
import random
import shutil
import subprocess
import unittest
from decimal import Decimal as D
from pathlib import Path

from xrpl_arb.cycles import candidates, run_cycle
from xrpl_arb.liquidity import Market, Offer, Pool
from xrpl_arb.model import XRP, Asset
from xrpl_arb.stream import PairIndex

ROOT = Path(__file__).resolve().parent.parent
NODE = shutil.which("node")
USD = Asset("USD", "rvYAfWj5gh67oV6fW32ZzP3Aw4Eubs59B")
EUR = Asset("EUR", "rhub8VRN55s94qWKDv6jmDy1pUykJzF3wq")
SOLO = Asset("534F4C4F00000000000000000000000000000000", "rsoLo2S1kiGeCcn6hCUXVrCpGMWLrRrLZz")
GBP = Asset("GBP", "rHb9CJAWyB4rj91VRWn96DkukG4bwdtyTh")
NODES = [XRP, USD, EUR, SOLO]


def py_market(spec):
    pool = spec["pool"] and Pool(Asset.parse(spec["pool"]["a"]), Asset.parse(spec["pool"]["b"]), D(str(spec["pool"]["ra"])),
                                 D(str(spec["pool"]["rb"])), D(str(spec["pool"]["fee"])))
    offers = tuple(Offer(D(str(p)), D(str(g))) for p, g in spec["offers"])
    return Market(Asset.parse(spec["in"]), Asset.parse(spec["out"]), offers, pool, D(str(spec["rate"])))


def random_markets(rng, nodes, noise=0.01):
    """Every direction among `nodes`: a few offers and (often) a pool, priced near a common fair value with noise."""
    fair = {a: 1.0 if a == XRP else rng.uniform(0.05, 3.0) for a in nodes}
    rates = {a: 1.0 if a == XRP else rng.choice([1.0, 1.0, 1.005]) for a in nodes}
    pools = {}
    specs = []
    for x in nodes:
        for y in nodes:
            if x == y:
                continue
            base = fair[x] / fair[y]  # units of y per unit of x
            offers = []
            for _ in range(rng.randint(0, 3)):
                gets = round(rng.uniform(2, 300), 6)
                pays = round(gets / (base * (1 + rng.gauss(0, noise))), 6)
                offers.append([pays, gets])
            offers.sort(key=lambda o: o[0] / o[1])
            key = frozenset((str(x), str(y)))
            if key not in pools:
                pools[key] = None
                if rng.random() < 0.6:
                    ra = round(rng.uniform(300, 5000), 4)
                    pools[key] = {"a": str(x), "b": str(y), "ra": ra, "rb": round(ra * base * (1 + rng.gauss(0, noise)), 4),
                                  "fee": rng.choice([0, 0.001, 0.00393])}
            specs.append({"in": str(x), "out": str(y), "offers": offers, "pool": pools[key], "rate": rates[x]})
    return specs


def build_cases():
    rng = random.Random(11)
    quotes = []
    for _ in range(80):
        spec = random_markets(rng, [XRP, USD])[rng.randint(0, 1)]
        quotes.append({"market": spec, "sizes": [0.001, 0.1, 1, 10, 100, 1000, 20000]})
    cycles = [{"markets": random_markets(rng, NODES, noise=0.02), "max_xrp": 100, "tx_cost": 0, "max_legs": 4} for _ in range(60)]
    labels = [str(a) for a in (XRP, USD, SOLO, GBP)] + [
        "4142434445464748494A4B4C4D4E4F5051525354+" + GBP.issuer,  # 20 printable bytes
        "0000000000000000000000000000000000000000+" + GBP.issuer,  # nothing but padding
        "0102030405060708090A0B0C0D0E0F1011121314+" + GBP.issuer,  # not printable
        "24545255 4D50".replace(" ", "") + "0" * 28 + "+" + GBP.issuer,  # $TRUMP
    ]
    stream = json.loads((ROOT / "tests" / "js" / "xrpl_stream_fixture.json").read_text())
    return {"labels": labels, "quotes": quotes, "cycles": cycles, "stream": stream, "synthetic": synthetic_messages(stream)}


def synthetic_messages(fx):
    """Every metadata branch the mapping reads, in the node shapes mainnet uses. The recorded messages never touch a pool
    through its RippleState alone (that is how a pair with no XRP in it shows up), so this fills that hole."""
    acct, key = sorted(fx["amm_accounts"].items())[0]
    a, b = key.split("|")
    amount = lambda i, v="1": "1000000" if i == "XRP" else {"currency": i.split("+")[0], "issuer": i.split("+")[1], "value": v}
    other = {"currency": "GBP", "issuer": GBP.issuer, "value": "1"}

    def msg(*nodes):
        return {"type": "transaction", "validated": True, "ledger_index": 1, "meta": {"AffectedNodes": list(nodes)}}

    def node(kind, entry, **f):
        return {kind: {"LedgerEntryType": entry, "NewFields" if kind == "CreatedNode" else "FinalFields": f}}

    limit = lambda who: {"currency": "USD", "issuer": who, "value": "0"}
    return [
        msg(node("ModifiedNode", "RippleState", HighLimit=limit(GBP.issuer), LowLimit=limit(acct))),   # pool on the low side
        msg(node("ModifiedNode", "RippleState", HighLimit=limit(acct), LowLimit=limit(GBP.issuer))),   # pool on the high side
        msg(node("ModifiedNode", "RippleState", HighLimit=limit(GBP.issuer), LowLimit=limit(GBP.issuer))),  # someone else's line
        msg(node("ModifiedNode", "AccountRoot", Account=acct, Balance="5")),
        msg(node("ModifiedNode", "AccountRoot", Account=GBP.issuer, Balance="5")),
        msg(node("CreatedNode", "Offer", TakerGets=amount(a), TakerPays=amount(b))),
        msg(node("DeletedNode", "Offer", TakerGets=amount(b), TakerPays=amount(a))),
        msg(node("ModifiedNode", "Offer", TakerGets=other, TakerPays=amount(a))),                     # a pair nobody watches
        msg(node("ModifiedNode", "DirectoryNode", Owner=acct)),
        msg(node("ModifiedNode", "RippleState", HighLimit=limit(GBP.issuer), LowLimit=limit(acct)),
            node("ModifiedNode", "AccountRoot", Account=GBP.issuer)),                                  # several nodes, one pair
        {"type": "transaction", "validated": True, "ledger_index": 1},                                 # no metadata at all
    ]


@unittest.skipUnless(NODE, "needs node")
class EngineParity(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cases = build_cases()
        run = subprocess.run([NODE, str(ROOT / "tests" / "js" / "xrpl_parity.js")], input=json.dumps(cls.cases), capture_output=True,
                             text=True, timeout=120)
        if run.returncode:
            raise AssertionError(run.stderr)
        cls.js = json.loads(run.stdout)

    def test_labels(self):
        for text, js in zip(self.cases["labels"], self.js["labels"]):
            asset = XRP if text == "XRP" else Asset(*text.split("+"))
            self.assertEqual("XRP" if asset.is_xrp else asset.label, js, text)
        self.assertEqual(self.js["labels"][-1], "$TRUMP")
        self.assertEqual(self.js["labels"][-2], "0102030405060708090A0B0C0D0E0F1011121314")  # not printable: shown as hex

    def test_quotes_match_the_python_to_rounding(self):
        checked = 0
        for case, js_out in zip(self.cases["quotes"], self.js["quotes"]):
            m = py_market(case["market"])
            for size, got in zip(case["sizes"], js_out):
                want = float(m.quote(D(str(size)))[0])
                self.assertAlmostEqual(got, want, delta=1e-9 * max(1.0, abs(want)), msg=f"{case['market']} size {size}")
                checked += 1
        self.assertGreater(checked, 500)

    def test_spot_rates_match(self):
        for case, got in zip(self.cases["quotes"], self.js["spot"]):
            m = py_market(case["market"])
            self.assertAlmostEqual(got, float(m.spot_rate()), delta=1e-12 * max(1.0, got))

    def test_cycles_and_best_sizes_match(self):
        found = compared = 0
        for case, js_opps in zip(self.cases["cycles"], self.js["cycles"]):
            markets = {}
            for spec in case["markets"]:
                m = py_market(spec)
                markets[(m.asset_in, m.asset_out)] = m
            py = {tuple(o.path): o for o in candidates(markets, max_xrp=D(case["max_xrp"]), tx_cost=D(case["tx_cost"]), max_legs=case["max_legs"])}
            js = {tuple(o["path"]): o for o in js_opps}
            for path in set(py) | set(js):
                if path in py and path in js:
                    p, j = py[path], js[path]
                    # The profit curve is nearly flat at its peak, so float64 and Decimal can settle a few drops (or a few
                    # thousandths of an XRP) apart on the best size while the profit agrees to ~1e-12: profit is the check.
                    self.assertAlmostEqual(j["profit"], float(p.profit), delta=2e-5 + 1e-6 * abs(j["profit"]), msg=str(path))
                    # Profit can be flat over a range of sizes (a zero-fee pool round-trips for nothing once the offers are used
                    # up), so two correct engines may pick different sizes. The size JS chose must earn the Python optimum
                    # when run through the Python maths.
                    cycle = tuple(markets[(Asset.parse(a), Asset.parse(b))] for a, b in zip(path[:-1], path[1:]))
                    x = D(str(j["amountIn"]))
                    self.assertGreaterEqual(float(run_cycle(cycle, x) - x - D(case["tx_cost"])), float(p.profit) - 2e-5, msg=str(path))
                    compared += 1
                else:  # a cycle only one side keeps must be on the edge of profitable
                    lone = float(py[path].profit) if path in py else js[path]["profit"]
                    self.assertLess(lone, 5e-4, f"{path} found by one side only")
            found += len(py)
        self.assertGreater(compared, 40, "the random graphs should contain real arbitrage to compare")
        self.assertGreater(found, 40)

    def test_stream_mapping_matches_on_real_mainnet_metadata(self):
        fx = self.cases["stream"]
        pairs = [(Asset.parse(a), Asset.parse(b)) for a, b in fx["pairs"]]
        index = PairIndex(pairs)
        by_key = {"|".join(sorted(str(a) for a in k)): k for k in (frozenset(p) for p in pairs)}
        index.learn_accounts({by_key[key]: Pool(*sorted(by_key[key], key=str), D(1), D(1), D(0), acct) for acct, key in fx["amm_accounts"].items()})
        py = [sorted("|".join(sorted(str(a) for a in k)) for k in index.touched(m)) for m in fx["messages"]]
        self.assertEqual(py, fx["expected"])  # the recording still means what it did when it was taken
        self.assertEqual(self.js["touched"], fx["expected"])
        self.assertGreaterEqual(sum(bool(e) for e in fx["expected"]), 10)

    def test_every_metadata_branch_maps_the_same_way(self):
        fx = self.cases["stream"]
        pairs = [(Asset.parse(a), Asset.parse(b)) for a, b in fx["pairs"]]
        index = PairIndex(pairs)
        by_key = {"|".join(sorted(str(a) for a in k)): k for k in (frozenset(p) for p in pairs)}
        index.learn_accounts({by_key[key]: Pool(*sorted(by_key[key], key=str), D(1), D(1), D(0), acct) for acct, key in fx["amm_accounts"].items()})
        py = [sorted("|".join(sorted(str(a) for a in k)) for k in index.touched(m)) for m in self.cases["synthetic"]]
        key = sorted(fx["amm_accounts"].items())[0][1]
        self.assertEqual(py, [[key], [key], [], [key], [], [key], [key], [], [], [key], []])  # what each message should mean
        self.assertEqual(self.js["touched_synthetic"], py)
        self.assertGreaterEqual(sum(not e for e in fx["expected"]), 10)


@unittest.skipUnless(NODE, "needs node")
class EngineEndToEnd(unittest.TestCase):
    def test_engine_follows_an_opportunity_over_a_scripted_websocket(self):
        run = subprocess.run([NODE, str(ROOT / "tests" / "js" / "xrpl_engine_test.js")], capture_output=True, text=True, timeout=120)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertIn("engine ok", run.stdout)


if __name__ == "__main__":
    unittest.main()
