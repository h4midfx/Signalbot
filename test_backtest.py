import asyncio, unittest
import testkit  # noqa: F401  (stubهای httpx/telegram)
import httpx
import backtest as bt
import smc

TF = 900_000


def raw_from_df(df):
    return [[int(t), str(o), str(h), str(l), str(c), "1", int(t) + TF - 1, "0", 1, "0", "0", "0"]
            for t, o, h, l, c in df[["t", "o", "h", "l", "c"]].itertuples(index=False)]


class FakeClient:
    """سرور کندل ساختگی: به startTime و limit احترام می‌گذارد."""
    def __init__(self, raw, fail_first=0, garbage=False):
        self.raw, self.calls, self.fail_first, self.garbage = raw, 0, fail_first, garbage

    async def get(self, url, params=None, timeout=None):
        self.calls += 1
        if self.calls <= self.fail_first:
            raise httpx.HTTPError("boom")
        rows = [r for r in self.raw if params["startTime"] <= r[0] <= params.get("endTime", 10 ** 18)][: params["limit"]]
        data = {"code": -1} if self.garbage else rows
        class R:
            def raise_for_status(s): pass
            def json(s): return data
        return R()


async def _nosleep(_): return None
bt._sleep = _nosleep


class FetchHistoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_pagination_collects_all_candles_without_duplicates(self):
        df = smc.synthetic_candles(3200, 1); raw = raw_from_df(df); client = FakeClient(raw)
        out = await bt.fetch_history(client, "BTCUSDT", "15m", 60, now_ms=int(df["t"].iloc[-1]) + TF + 1)
        self.assertEqual(len(out), 3200); self.assertGreaterEqual(client.calls, 3)
        self.assertTrue((out["t"].diff().dropna() == TF).all())

    async def test_retry_and_permanent_failure(self):
        df = smc.synthetic_candles(200, 1); now = int(df["t"].iloc[-1]) + TF + 1
        out = await bt.fetch_history(FakeClient(raw_from_df(df), fail_first=2), "X", "15m", 5, now_ms=now)
        self.assertEqual(len(out), 200)
        with self.assertRaises(ValueError): await bt.fetch_history(FakeClient([], fail_first=99), "X", "15m", 5, now_ms=now)

    async def test_garbage_response_raises_value_error(self):
        with self.assertRaises(ValueError):
            await bt.fetch_history(FakeClient([], garbage=True), "X", "15m", 5, now_ms=10 ** 13)

    async def test_run_symbols_reports_failed_symbol_but_continues(self):
        df = smc.synthetic_candles(3000, 2); good = FakeClient(raw_from_df(df)); now = int(df["t"].iloc[-1]) + TF + 1
        class Mixed:
            async def get(s, url, params=None, timeout=None):
                if params["symbol"] == "BAD": raise httpx.HTTPError("x")
                return await good.get(url, params, timeout)
        orig = bt.fetch_history
        async def pinned(*a, **k): return await orig(*a, now_ms=now, **k)
        bt.fetch_history = pinned
        try:
            trades, failed = await bt.run_symbols(Mixed(), ["BTCUSDT", "BAD"], "15m", 40, 1, 0.0004, "http://x", retries=1)
        finally:
            bt.fetch_history = orig
        self.assertEqual(failed, ["BAD"]); self.assertGreater(len(trades), 0)


class BacktestLogicTests(unittest.TestCase):
    def setUp(self): self.df = smc.synthetic_candles(4000, 5)

    def test_fees_reduce_results_and_no_overlap_same_side(self):
        t0 = bt.run_backtest(self.df, 1, fee=0.0); t1 = bt.run_backtest(self.df, 1, fee=0.001)
        self.assertGreater(len(t0), 5)
        self.assertEqual([x["gross"] for x in t0], [x["gross"] for x in t1])
        self.assertTrue(all(b["net"] < a["net"] for a, b in zip(t0, t1)))
        for side in ("LONG", "SHORT"):
            seq = [x for x in t0 if x["side"] == side]
            for prev, nxt in zip(seq, seq[1:]): self.assertGreater(nxt["idx"], prev["closed"])

    def test_min_score_reduces_trades(self):
        self.assertGreaterEqual(len(bt.run_backtest(self.df, 1)), len(bt.run_backtest(self.df, 3)))

    def test_summary_fields_and_verdicts(self):
        self.assertEqual(bt.summarize([])["n"], 0)
        self.assertIn("نمونه کم", bt.summarize(bt.run_backtest(self.df, 1)[:5])["verdict"])
        mk = lambda r: dict(t=0, idx=0, closed=0, side="LONG", score=2, hits=1, outcome="sl", gross=r, net=r, cost=0)
        win = bt.summarize([dict(mk(0.5), t=i) for i in range(40)] + [dict(mk(0.4), t=99)])
        self.assertIn("مثبت", win["verdict"])
        lose = bt.summarize([dict(mk(-0.5), t=i) for i in range(40)] + [dict(mk(-0.4), t=99)])
        self.assertIn("ضررده", lose["verdict"])
        mixed = bt.summarize([dict(mk(1.0 if i % 2 else -1.0), t=i) for i in range(40)])
        self.assertIn("قابل تشخیص نیست", mixed["verdict"])

    def test_drawdown_and_profit_factor(self):
        mk = lambda t, r: dict(t=t, idx=t, closed=t, side="LONG", score=2, hits=0, outcome="sl", gross=r, net=r, cost=0)
        sm = bt.summarize([mk(1, 2.0), mk(2, -1.0), mk(3, -1.5), mk(4, 1.0)])
        self.assertAlmostEqual(sm["max_dd"], 2.5); self.assertAlmostEqual(sm["pf"], 3.0 / 2.5); self.assertAlmostEqual(sm["total"], 0.5)

    def test_report_text(self):
        txt = bt.format_report("TEST", bt.summarize(bt.run_backtest(self.df, 1)), 0.0004)
        for needle in ("بک‌تست", "R", "PF", "overfitting"): self.assertIn(needle, txt)
        self.assertIn("سیگنالی", bt.format_report("T", {"n": 0}, 0.0004))

    def test_no_future_candle_signal_is_skipped(self):
        # سیگنال روی آخرین کندل نتیجه‌ای ندارد و نباید در آمار بیاید
        tr = bt.run_backtest(self.df, 1)
        self.assertTrue(all(x["closed"] > x["idx"] for x in tr))


if __name__ == "__main__":
    unittest.main()
