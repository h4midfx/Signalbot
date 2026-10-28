import unittest
import numpy as np
import pandas as pd
import smc


def mirror(df):
    m = df.copy()
    m["o"], m["h"], m["l"], m["c"] = 200 - df["o"], 200 - df["l"], 200 - df["h"], 200 - df["c"]
    return m


HANDMADE = [
    (100, 101, 99, 100.5), (100.5, 102, 100, 101.5), (101.5, 103, 101, 102.5), (102.5, 105, 102, 104),
    (104, 104.5, 101.5, 102), (102, 102.5, 99.5, 100), (100, 100.5, 97, 98), (98, 98.5, 96, 97),
    (97, 99, 96.5, 98.5), (98.5, 100, 98, 99.5), (99.5, 101, 99, 100.5), (100.5, 102, 100, 101),
    (101, 101.5, 100, 100.2), (100.2, 104, 100, 103.8), (103.8, 107, 103.5, 106.5), (106.5, 107, 105, 106),
    (106, 106.2, 102.5, 103), (103, 103.2, 100.0, 100.8), (100.8, 103, 100.5, 102.5), (102.5, 104, 102, 103.5),
    (103.5, 105, 103, 104.5)]


def handmade_df():
    return pd.DataFrame([(i, *p) for i, p in enumerate(HANDMADE)], columns=["t", "o", "h", "l", "c"])


def geometry_ok(s):
    t = s["tps"]
    if s["side"] == "LONG":
        return s["sl"] < s["entry"] < t[0] < t[1] < t[2] < t[3] < t[4]
    return s["sl"] > s["entry"] > t[0] > t[1] > t[2] > t[3] > t[4]


class FvgTests(unittest.TestCase):
    def test_bullish_gap(self):
        self.assertTrue(smc._find_fvg([10, 11, 12, 15], [9, 10, 11, 13], 0, 3, "LONG"))

    def test_no_gap(self):
        self.assertFalse(smc._find_fvg([10, 11, 12, 13], [9, 9.5, 10, 11], 0, 3, "LONG"))

    def test_bearish_gap(self):
        self.assertTrue(smc._find_fvg([15, 12, 10, 9], [13, 11, 8, 7], 0, 3, "SHORT"))


class StructureTests(unittest.TestCase):
    def _padded(self, df):
        pad = pd.DataFrame([(-100 + i, 100, 100.4, 99.6, 100) for i in range(30)], columns=["t", "o", "h", "l", "c"])
        pad["t"] = range(30)
        d = df.copy(); d["t"] = d["t"] + 30
        return pd.concat([pad, d], ignore_index=True)

    def test_handmade_long_and_mirror_short(self):
        base = self._padded(handmade_df())
        longs = [s for s in smc.analyze(base, n=2, require_pd=False) if s["side"] == "LONG"]
        self.assertTrue(longs, "باید LONG پیدا شود")
        shorts = [s for s in smc.analyze(mirror(base), n=2, require_pd=False) if s["side"] == "SHORT"]
        self.assertTrue(shorts, "آینه‌ی ساختار باید SHORT بدهد")
        for s in longs + shorts:
            self.assertTrue(geometry_ok(s))
            r = abs(s["entry"] - s["sl"])
            self.assertAlmostEqual(abs(s["tps"][4] - s["entry"]), 5 * r)

    def test_geometry_on_synthetic(self):
        sigs = smc.analyze(smc.synthetic_candles(4000, 7))
        self.assertTrue(sigs)
        self.assertTrue(all(geometry_ok(s) for s in sigs))
        self.assertTrue(all(1 <= s["score"] <= 4 for s in sigs))

    def test_no_lookahead(self):
        df = smc.synthetic_candles(2500, 3)
        key = lambda s: (s["idx"], s["side"], round(s["entry"], 8))
        full = {key(s) for s in smc.analyze(df)}
        for cut in (800, 1400, 2000):
            part = {key(s) for s in smc.analyze(df.iloc[:cut].reset_index(drop=True))}
            self.assertEqual(part, {x for x in full if x[0] < cut}, f"repaint at {cut}")


class DataGuardTests(unittest.TestCase):
    def test_empty_and_short(self):
        self.assertEqual(smc.analyze(pd.DataFrame(columns=["t", "o", "h", "l", "c"])), [])
        self.assertEqual(smc.analyze(smc.synthetic_candles(20)), [])
        self.assertEqual(smc.analyze(None), [])

    def test_invalid_values(self):
        df = smc.synthetic_candles(200)
        bad = df.copy(); bad.loc[50, "c"] = float("nan")
        with self.assertRaises(ValueError): smc.analyze(bad)
        bad = df.copy(); bad.loc[50, "h"] = bad.loc[50, "l"] - 1
        with self.assertRaises(ValueError): smc.analyze(bad)
        bad = df.copy(); bad.loc[50, "o"] = -1
        with self.assertRaises(ValueError): smc.analyze(bad)
        bad = df.copy(); bad.loc[50, "t"] = bad.loc[49, "t"]
        with self.assertRaises(ValueError): smc.analyze(bad)
        with self.assertRaises(ValueError): smc.analyze(df.drop(columns=["c"]))

    def test_klines_parsing(self):
        raw = [[i * 900000, "100", "101", "99", "100.5", "5", i * 900000 + 899999, "0", 1, "0", "0", "0"] for i in range(5)]
        df = smc.klines_to_df(raw, now_ms=4 * 900000 + 100)      # آخرین کندل هنوز بسته نشده
        self.assertEqual(len(df), 4)
        for bad in ([], None, "x", [[1, 2]], [[0, "a", "b", "c", "d", "e", 1]]):
            with self.assertRaises(ValueError): smc.klines_to_df(bad)


class OutcomeTests(unittest.TestCase):
    base = dict(idx=0, side="LONG", entry=100.0, sl=99.0, tps=[101, 102, 103, 104, 105])

    def sim(self, h, l, c, sig=None):
        n = len(h)
        return smc.simulate(sig or self.base, pd.DataFrame({"t": range(n), "o": [100] * n, "h": h, "l": l, "c": c}))

    def test_all_targets(self):
        r = self.sim([100, 106, 106], [100, 100, 100], [100, 105, 105])
        self.assertEqual((r["hits"], r["outcome"]), (5, "done")); self.assertAlmostEqual(r["r"], 3.0)

    def test_stop_loss(self):
        r = self.sim([100, 100.5], [100, 98.9], [100, 99])
        self.assertTrue(r["stopped"]); self.assertAlmostEqual(r["r"], -1.0)

    def test_two_targets_then_stop(self):
        r = self.sim([100, 102.5, 102], [100, 100, 98.9], [100, 102, 99])
        self.assertEqual(r["hits"], 2); self.assertTrue(r["stopped"]); self.assertAlmostEqual(r["r"], 0.0)

    def test_same_candle_stop_wins(self):
        r = self.sim([100, 106], [100, 98], [100, 100])
        self.assertTrue(r["stopped"]); self.assertEqual(r["hits"], 0)

    def test_short_side(self):
        sig = dict(idx=0, side="SHORT", entry=100.0, sl=101.0, tps=[99, 98, 97, 96, 95])
        r = self.sim([100, 100.2], [100, 94], [100, 95], sig)
        self.assertEqual(r["hits"], 5)

    def test_expired_marks_to_market(self):
        r = self.sim([100, 100.5], [100, 99.5], [100, 100.5])
        self.assertEqual(r["outcome"], "expired"); self.assertAlmostEqual(r["r"], 0.5)

    def test_advance_reports_events_in_order(self):
        hits, ev, out, used = smc.advance("LONG", 99, [101, 102, 103, 104, 105], 0, [101.5, 103.2], [100, 100])
        self.assertEqual((hits, out, used), (3, None, 2))
        self.assertEqual(ev, [("tp", 1), ("tp", 2), ("tp", 3)])

    def test_each_target_only_once_across_calls(self):
        h1, e1, _, _ = smc.advance("LONG", 99, [101, 102, 103, 104, 105], 0, [101.5], [100])
        h2, e2, _, _ = smc.advance("LONG", 99, [101, 102, 103, 104, 105], h1, [101.6], [100])
        self.assertEqual(e1, [("tp", 1)]); self.assertEqual(e2, [])


class BacktestTests(unittest.TestCase):
    def test_backtest_runs_without_profit_claim(self):
        rs = []
        for seed in range(6):
            df = smc.synthetic_candles(4000, seed, trending=False)
            rs += [smc.simulate(s, df)["r"] for s in smc.analyze(df)]
        self.assertGreater(len(rs), 20)
        self.assertTrue(np.isfinite(rs).all())
        print(f"\n[بک‌تست مصنوعی بدون روند] n={len(rs)} avgR={np.mean(rs):+.3f} (فقط برای سلامت کد؛ نه سودآوری)")


if __name__ == "__main__":
    unittest.main()
