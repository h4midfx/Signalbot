import json, time, unittest
import pandas as pd
import testkit
from testkit import (B, FakeBot, make_ctx, make_update, make_pre, make_payment, reply_text,
                     Forbidden, RetryAfter, NetworkError, TelegramError)
import smc
from config import ConfigError, load_config

TF = 900_000


def find_signal(min_score=1, seeds=range(60)):
    for seed in seeds:
        df = smc.synthetic_candles(3000, seed)
        for s in smc.analyze(df):
            if s["score"] >= min_score and s["idx"] > 300:
                return df, s
    raise AssertionError("no synthetic signal found")


def cut_at(df, s):
    return df.iloc[: s["idx"] + 1].reset_index(drop=True)


def now_for(df): return int(df["t"].iloc[-1]) + TF + 1000


class ConfigTests(unittest.TestCase):
    def test_defaults_and_errors(self):
        cfg = load_config({"BOT_TOKEN": "t", "ADMIN_ID": "5"})
        self.assertEqual([p.id for p in cfg.plans], ["week", "month", "quarter"]); self.assertEqual(cfg.admin_ids, {5})
        for env in ({}, {"BOT_TOKEN": "t"}, {"BOT_TOKEN": "t", "ADMIN_IDS": "x"},
                    {"BOT_TOKEN": "t", "ADMIN_IDS": "1", "PLANS": "a:30"},
                    {"BOT_TOKEN": "t", "ADMIN_IDS": "1", "PLANS": "a:30:99999"},
                    {"BOT_TOKEN": "t", "ADMIN_IDS": "1", "TIMEFRAME": "7m"},
                    {"BOT_TOKEN": "t", "ADMIN_IDS": "1", "SYMBOLS": "btc/usdt"}):
            with self.assertRaises(ConfigError): load_config(env)

    def test_legacy_env_names(self):
        cfg = load_config({"BOT_TOKEN": "t", "ADMIN_ID": "5", "PRICE_STARS": "300", "SUB_DAYS": "10"})
        self.assertEqual((cfg.plans[0].days, cfg.plans[0].stars), (10, 300))


class InvoiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_buy_shows_plan_buttons(self):
        ctx, cfg, db, bot = make_ctx(); u = make_update(10)
        await B.buy(u, ctx)
        kb = u.message.reply_text.call_args.kwargs["reply_markup"].inline_keyboard
        self.assertEqual([r[0].callback_data for r in kb], [f"buy:{p.id}" for p in cfg.plans])

    async def test_invoice_uses_stars_and_matching_amount(self):
        ctx, cfg, db, bot = make_ctx(); u = make_update(10, data="buy:month")
        await B.buy_button(u, ctx)
        inv = bot.invoices[0]
        self.assertEqual((inv["currency"], inv["provider_token"], inv["payload"]), ("XTR", "", "sub:month:10"))
        self.assertEqual(inv["prices"][0].amount, cfg.plan("month").stars)

    async def test_unknown_plan_button_creates_no_invoice(self):
        ctx, *_ , bot = make_ctx(); await B.buy_button(make_update(10, data="buy:hack"), ctx)
        await B.buy_button(make_update(10, data="other"), ctx)
        self.assertEqual(bot.invoices, [])

    async def test_precheckout_accepts_valid_and_rejects_invalid(self):
        ctx, *_ = make_ctx()
        cases = [("sub:month:10", "XTR", 500, True), ("evil", "XTR", 500, False), ("sub:month:11", "XTR", 500, False),
                 ("sub:month:10", "USD", 500, False), ("sub:month:10", "XTR", 1, False), ("sub:nope:10", "XTR", 500, False),
                 ("sub:month:10:x", "XTR", 500, False), ("sub:month:abc", "XTR", 500, False), ("", "XTR", 500, False)]
        for payload, cur, amt, expect in cases:
            pre = make_pre(10, payload, cur, amt); await B.precheckout(make_update(10, pre=pre), ctx)
            self.assertIs(pre.answer.call_args.kwargs["ok"], expect, (payload, cur, amt))
            if not expect: self.assertTrue(pre.answer.call_args.kwargs["error_message"])


class PaymentFlowTests(unittest.IsolatedAsyncioTestCase):
    async def pay(self, ctx, uid=10, charge="ch_1", payload=None, cur="XTR", amt=500):
        u = make_update(uid, payment=make_payment(charge, payload or f"sub:month:{uid}", cur, amt))
        await B.successful_payment(u, ctx); return u

    async def test_valid_payment_activates_and_notifies_admin(self):
        ctx, cfg, db, bot = make_ctx(); await self.pay(ctx)
        self.assertTrue(db.is_active(10)); self.assertTrue(any("ch_1" in t for t in bot.texts(1)))
        self.assertTrue(any("فعال" in t for t in bot.texts(10)))

    async def test_duplicate_payment_does_not_extend_again(self):
        ctx, cfg, db, bot = make_ctx(); await self.pay(ctx); first = db.expires_at(10)
        await self.pay(ctx); self.assertEqual(db.expires_at(10), first)
        self.assertEqual(db.stats()["payments"], 1); self.assertTrue(any("قبلاً" in t for t in bot.texts(10)))

    async def test_concurrent_duplicates(self):
        import asyncio
        ctx, cfg, db, bot = make_ctx()
        await asyncio.gather(*(self.pay(ctx, charge="same") for _ in range(6)))
        self.assertEqual(db.stats()["payments"], 1); self.assertEqual(db.expires_at(10) - int(time.time()) // 1 > 29 * 86400, True)

    async def test_second_distinct_payment_stacks(self):
        ctx, cfg, db, bot = make_ctx(); await self.pay(ctx, charge="a"); e1 = db.expires_at(10)
        await self.pay(ctx, charge="b"); self.assertEqual(db.expires_at(10), e1 + 30 * 86400)

    async def test_wrong_amount_currency_or_user_never_activates(self):
        for kw in ({"amt": 1}, {"cur": "USD"}, {"payload": "sub:month:99"}, {"payload": "junk"}):
            ctx, cfg, db, bot = make_ctx(); await self.pay(ctx, **kw)
            self.assertFalse(db.is_active(10), kw); self.assertEqual(db.stats()["payments"], 0)
            self.assertTrue(any("نامعتبر" in t for t in bot.texts(1)))

    async def test_admin_notification_failure_does_not_break_activation(self):
        ctx, cfg, db, bot = make_ctx(); bot.fail[1] = NetworkError("down")
        await self.pay(ctx); self.assertTrue(db.is_active(10))

    async def test_user_blocked_bot_after_paying_still_recorded(self):
        ctx, cfg, db, bot = make_ctx(); bot.fail[10] = Forbidden("blocked")
        await self.pay(ctx); self.assertEqual(db.stats()["payments"], 1)

    async def test_referral_bonus_and_notification(self):
        ctx, cfg, db, bot = make_ctx(); db.upsert_user(77, "ref"); db.upsert_user(10, "n"); db.set_referrer(10, 77)
        await self.pay(ctx); self.assertTrue(db.is_active(77)); self.assertTrue(any("هدیه" in t for t in bot.texts(77)))

    async def test_logs_do_not_leak_full_charge_id(self):
        ctx, cfg, db, bot = make_ctx()
        with self.assertLogs("signalbot", level="INFO") as cm:
            await self.pay(ctx, charge="SECRETCHARGEID123", amt=1)
            await self.pay(ctx, charge="SECRETCHARGEID456"); await self.pay(ctx, charge="SECRETCHARGEID456")
        self.assertNotIn("SECRETCHARGE", "\n".join(cm.output))

    async def test_refund_flow_and_admin_only(self):
        ctx, cfg, db, bot = make_ctx(); await self.pay(ctx, charge="rf")
        ctx.args = ["10", "rf"]
        u = make_update(555); await B.refund(u, ctx); self.assertEqual(bot.refunds, [])          # کاربر عادی
        bot.refund_error = TelegramError("fail"); u = make_update(1); await B.refund(u, ctx)
        self.assertTrue(db.is_active(10)); self.assertEqual(db.get_payment("rf")["refunded"], 0)   # خطا -> بدون تغییر
        bot.refund_error = None; await B.refund(make_update(1), ctx)
        self.assertEqual(bot.refunds, [(10, "rf")]); self.assertEqual(db.get_payment("rf")["refunded"], 1)
        self.assertFalse(db.is_active(10))
        u = make_update(1); await B.refund(u, ctx); self.assertIn("قبلاً", reply_text(u)); self.assertEqual(len(bot.refunds), 1)


class BroadcastTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_active_users_receive_and_errors_isolated(self):
        ctx, cfg, db, bot = make_ctx()
        for uid in (10, 20, 30, 40): db.grant(uid, 5)
        db.conn.execute("INSERT OR REPLACE INTO subs(user_id, expires) VALUES(50, ?)", (int(time.time()) - 10,))   # منقضی
        bot.fail[20] = Forbidden("blocked"); bot.fail[30] = NetworkError("x")
        r = await B.broadcast(bot, db, db.active_user_ids(), "hi")
        self.assertEqual(r, {"ok": 2, "blocked": 1, "failed": 1})
        self.assertEqual({u for u, _ in bot.sent}, {10, 40}); self.assertEqual(bot.texts(50), [])
        self.assertNotIn(20, db.active_user_ids())                                      # بلاک‌کننده علامت خورد

    async def test_retry_after_is_retried_once(self):
        ctx, cfg, db, bot = make_ctx(); db.grant(10, 5); bot.fail[10] = [RetryAfter(1)]
        r = await B.broadcast(bot, db, [10], "hi"); self.assertEqual(r["ok"], 1); self.assertEqual(len(bot.texts(10)), 1)


class SignalFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.ctx, self.cfg, self.db, self.bot = make_ctx()
        for uid in (10, 20): self.db.grant(uid, 5)
        self.db.conn.execute("INSERT OR REPLACE INTO subs(user_id, expires) VALUES(30, ?)", (int(time.time()) - 10,))
        self.df, self.sig = find_signal(); self.cut = cut_at(self.df, self.sig)

    async def test_signal_goes_only_to_active_subscribers_with_five_targets(self):
        n = await B.process_new_signals(self.ctx, {"BTCUSDT": self.cut}, now_ms=now_for(self.cut))
        self.assertEqual(n, 1)
        for uid in (10, 20):
            t = self.bot.texts(uid)[0]; self.assertTrue(all(f"TP{i}" in t for i in range(1, 6))); self.assertIn("SMC", t)
        self.assertEqual(self.bot.texts(30), []); self.assertEqual(self.bot.texts(99), [])
        self.assertTrue(any("📤" in t for t in self.bot.texts(1)))

    async def test_no_duplicate_on_consecutive_scans(self):
        for _ in range(3): await B.process_new_signals(self.ctx, {"BTCUSDT": self.cut}, now_ms=now_for(self.cut))
        self.assertEqual(len(self.bot.texts(10)), 1)
        self.assertEqual(self.db.conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0], 1)

    async def test_stale_candles_not_sent(self):
        n = await B.process_new_signals(self.ctx, {"BTCUSDT": self.cut}, now_ms=now_for(self.cut) + 10 * TF)
        self.assertEqual(n, 0); self.assertEqual(self.bot.texts(10), [])

    async def test_min_score_filter(self):
        ctx, cfg, db, bot = make_ctx({"MIN_SCORE": "4"}); db.grant(10, 5)
        df, s = find_signal(1); s2 = [x for x in smc.analyze(df) if x["idx"] == s["idx"]][0]
        if s2["score"] < 4:
            n = await B.process_new_signals(ctx, {"X": cut_at(df, s)}, now_ms=now_for(cut_at(df, s)))
            self.assertEqual(n, 0)

    async def test_bad_or_missing_data_is_skipped_safely(self):
        bad = self.cut.copy(); bad.loc[100, "c"] = float("nan")
        n = await B.process_new_signals(self.ctx, {"A": None, "B": bad, "C": self.cut.iloc[:10]}, now_ms=now_for(self.cut))
        self.assertEqual(n, 0)

    async def test_no_stacking_while_same_side_open(self):
        await B.process_new_signals(self.ctx, {"BTCUSDT": self.cut}, now_ms=now_for(self.cut))
        self.assertTrue(self.db.has_open("BTCUSDT", self.sig["side"]))
        later = [s for s in smc.analyze(self.df) if s["idx"] > self.sig["idx"] and s["side"] == self.sig["side"]]
        if later:
            c2 = cut_at(self.df, later[0]); before = len(self.bot.texts(10))
            await B.process_new_signals(self.ctx, {"BTCUSDT": c2}, now_ms=now_for(c2))
            self.assertEqual(len(self.bot.texts(10)), before)

    # ---- ردیابی
    def future(self, prices_hl):
        t0 = int(self.cut["t"].iloc[-1]); px = self.sig["entry"]
        rows = [(t0 + (k + 1) * TF, px, max(px, h), min(px, l), px) for k, (h, l) in enumerate(prices_hl)]
        return pd.concat([self.cut, pd.DataFrame(rows, columns=["t", "o", "h", "l", "c"])], ignore_index=True)

    async def test_tracking_announces_each_target_once(self):
        await B.process_new_signals(self.ctx, {"BTCUSDT": self.cut}, now_ms=now_for(self.cut))
        long_, e, tps = self.sig["side"] == "LONG", self.sig["entry"], self.sig["tps"]
        hl = lambda tp: (tp, e) if long_ else (e, tp)
        self.bot.sent.clear()
        d1 = self.future([hl(tps[0]), hl(tps[1])]); await B.track_open(self.ctx, {"BTCUSDT": d1})
        await B.track_open(self.ctx, {"BTCUSDT": d1})                                   # دوباره: چیزی اعلام نشود
        msgs = " ".join(self.bot.texts(10)); self.assertIn("TP1", msgs); self.assertIn("TP2", msgs); self.assertNotIn("TP3", msgs)
        self.assertEqual(len(self.bot.texts(10)), 1)
        d2 = self.future([hl(tps[0]), hl(tps[1]), hl(tps[2]), hl(tps[3]), hl(tps[4])]); self.bot.sent.clear()
        await B.track_open(self.ctx, {"BTCUSDT": d2}); m2 = " ".join(self.bot.texts(10))
        self.assertIn("TP3", m2); self.assertIn("TP5", m2); self.assertNotIn("TP1", m2); self.assertIn("🏆", m2)
        row = self.db.conn.execute("SELECT status,hits,r FROM signals").fetchone()
        self.assertEqual((row["status"], row["hits"]), ("done", 5)); self.assertAlmostEqual(row["r"], 3.0)
        self.bot.sent.clear(); await B.track_open(self.ctx, {"BTCUSDT": self.future([hl(tps[4])] * 7)})
        self.assertEqual(self.bot.sent, [])                                             # تکمیل‌شده دوباره پردازش نمی‌شود

    async def test_stop_loss_and_same_candle_conservative(self):
        await B.process_new_signals(self.ctx, {"BTCUSDT": self.cut}, now_ms=now_for(self.cut))
        long_, tps, sl = self.sig["side"] == "LONG", self.sig["tps"], self.sig["sl"]
        both = (tps[0], sl) if long_ else (sl, tps[0])           # یک کندل هم TP1 هم SL را لمس می‌کند
        hi, lo = (tps[0], sl) if long_ else (sl, tps[0])
        e = self.sig["entry"]; self.bot.sent.clear()
        await B.track_open(self.ctx, {"BTCUSDT": self.future([(max(e, hi, lo), min(e, hi, lo))])})
        row = self.db.conn.execute("SELECT status,hits,r FROM signals").fetchone()
        self.assertEqual((row["status"], row["hits"]), ("sl", 0)); self.assertAlmostEqual(row["r"], -1.0)
        self.assertIn("حد ضرر", " ".join(self.bot.texts(10))); self.assertNotIn("TP1", " ".join(self.bot.texts(10)))

    async def test_expiry_closes_signal(self):
        await B.process_new_signals(self.ctx, {"BTCUSDT": self.cut}, now_ms=now_for(self.cut))
        e = self.sig["entry"]; d = self.future([(e, e)] * (self.cfg.max_hold + 5)); self.bot.sent.clear()
        await B.track_open(self.ctx, {"BTCUSDT": d})
        self.assertEqual(self.db.conn.execute("SELECT status FROM signals").fetchone()[0], "expired")

    async def test_send_errors_do_not_break_tracking(self):
        await B.process_new_signals(self.ctx, {"BTCUSDT": self.cut}, now_ms=now_for(self.cut))
        for uid in (10, 20, 1): self.bot.fail[uid] = NetworkError("down")
        e, tps = self.sig["entry"], self.sig["tps"]; long_ = self.sig["side"] == "LONG"
        d = self.future([(tps[0], e) if long_ else (e, tps[0])])
        changed = await B.track_open(self.ctx, {"BTCUSDT": d})
        self.assertEqual(changed, 1); self.assertEqual(self.db.conn.execute("SELECT hits FROM signals").fetchone()[0], 1)


class CommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_registers_user_and_referral(self):
        ctx, cfg, db, bot = make_ctx(); db.upsert_user(77, "ref")
        ctx.args = ["ref_77"]; u = make_update(10); await B.start(u, ctx)
        self.assertIn("/buy", reply_text(u)); self.assertEqual(db.conn.execute("SELECT referred_by FROM users WHERE user_id=10").fetchone()[0], 77)
        ctx.args = ["ref_abc"]; await B.start(make_update(11), ctx)                               # ورودی خراب کرش نکند

    async def test_trial_once(self):
        ctx, cfg, db, bot = make_ctx(); u = make_update(10); await B.trial(u, ctx); self.assertTrue(db.is_active(10))
        u = make_update(10); await B.trial(u, ctx); self.assertIn("قبلاً", reply_text(u))
        ctx2, *_ = make_ctx({"TRIAL_DAYS": "0"}); u = make_update(10); await B.trial(u, ctx2); self.assertIn("فعال نیست", reply_text(u))

    async def test_status(self):
        ctx, cfg, db, bot = make_ctx(); u = make_update(10); await B.status(u, ctx); self.assertIn("❌", reply_text(u))
        db.grant(10, 3); u = make_update(10); await B.status(u, ctx); self.assertIn("✅", reply_text(u))

    async def test_admin_commands_restricted(self):
        ctx, cfg, db, bot = make_ctx(args=["10", "30"])
        for fn in (B.stats, B.grant, B.signal_cmd, B.announce, B.refund, B.testsignals):
            u = make_update(555); await fn(u, ctx); self.assertFalse(u.message.reply_text.called, fn.__name__)
        self.assertFalse(db.is_active(10)); self.assertEqual(bot.sent, [])

    async def test_grant_validation(self):
        ctx, cfg, db, bot = make_ctx()
        for args in ([], ["x", "3"], ["10", "0"], ["10", "99999"], ["-5", "3"]):
            ctx.args = args; u = make_update(1); await B.grant(u, ctx); self.assertIn("استفاده", reply_text(u)); self.assertFalse(db.is_active(10))
        ctx.args = ["10", "30"]; await B.grant(make_update(1), ctx); self.assertTrue(db.is_active(10))

    async def test_signal_and_announce_audiences(self):
        ctx, cfg, db, bot = make_ctx(); db.upsert_user(10, "a"); db.upsert_user(20, "b"); db.grant(10, 5)
        ctx.args = ["hello", "all"]; u = make_update(1); await B.announce(u, ctx)
        self.assertEqual({x for x, _ in bot.sent} - {1}, {10, 20}); bot.sent.clear()
        await B.signal_cmd(make_update(1), ctx); self.assertEqual({x for x, _ in bot.sent}, {10})
        ctx.args = []; u = make_update(1); await B.announce(u, ctx); self.assertIn("متن", reply_text(u))

    async def test_stats_output(self):
        ctx, cfg, db, bot = make_ctx(); db.record_payment("x", 10, 500, "month", 30)
        u = make_update(1); await B.stats(u, ctx); self.assertIn("500⭐", reply_text(u))

    async def test_testsignals_is_isolated(self):
        ctx, cfg, db, bot = make_ctx(); db.grant(10, 5)
        u = make_update(1); await B.testsignals(u, ctx)
        self.assertEqual(bot.sent, [])                                                           # هیچ پیامی به کاربران
        self.assertEqual(db.conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0], 0)
        self.assertEqual(db.stats()["payments"], 0)
        first = u.message.reply_text.call_args_list[0].args[0]
        self.assertIn("✅ سالم", first); self.assertIn("نامعتبر: 0", first); self.assertIn("مصنوعی", first)

    async def test_risk_calculator(self):
        ctx, *_ = make_ctx(args=["1000", "1", "100", "98"]); u = make_update(10); await B.risk(u, ctx)
        t = reply_text(u); self.assertIn("10.00", t); self.assertIn("5 واحد", t)
        for args in ([], ["a", "b", "c", "d"], ["1000", "1", "100", "100"], ["1000", "0", "100", "98"], ["-1", "1", "1", "2"], ["nan", "1", "1", "2"]):
            ctx.args = args; u = make_update(10); await B.risk(u, ctx); self.assertIn("استفاده", reply_text(u))

    async def test_performance_text(self):
        ctx, cfg, db, bot = make_ctx(); u = make_update(10); await B.performance(u, ctx); self.assertIn("هنوز", reply_text(u))
        sid = db.insert_signal("X", 1, "LONG", 100, 99, [1] * 5, 2, "BOS", "15m"); db.update_signal(sid, 3, 2, "sl", 0.2)
        u = make_update(10); await B.performance(u, ctx); self.assertIn("۳۰", reply_text(u))


class FetchTests(unittest.IsolatedAsyncioTestCase):
    def raw(self, n=100):
        return [[i * TF, "100", "101", "99", "100.5", "5", i * TF + TF - 1, "0", 1, "0", "0", "0"] for i in range(n)]

    async def test_retry_then_success(self):
        import httpx
        ctx, cfg, db, bot = make_ctx(); calls = {"n": 0}
        class Resp:
            def raise_for_status(self): pass
            def json(inner): return self.raw()
        class Client:
            async def get(c, *a, **k):
                calls["n"] += 1
                if calls["n"] < 3: raise httpx.HTTPError("boom")
                return Resp()
        df = await B.fetch_klines(Client(), "BTCUSDT", cfg)
        self.assertEqual(calls["n"], 3); self.assertGreater(len(df), 10)

    async def test_gives_up_and_rejects_garbage(self):
        import httpx
        ctx, cfg, db, bot = make_ctx()
        class Bad:
            async def get(c, *a, **k): raise httpx.HTTPError("down")
        self.assertIsNone(await B.fetch_klines(Bad(), "X", cfg))
        class Garbage:
            async def get(c, *a, **k):
                class R:
                    def raise_for_status(s): pass
                    def json(s): return {"code": -1}
                return R()
        self.assertIsNone(await B.fetch_klines(Garbage(), "X", cfg))


class ReminderJobTests(unittest.IsolatedAsyncioTestCase):
    async def test_reminders_sent_once(self):
        ctx, cfg, db, bot = make_ctx(); db.grant(10, 1)
        await B.reminder_job(ctx); await B.reminder_job(ctx)
        self.assertEqual(len(bot.texts(10)), 1)
        db.conn.execute("UPDATE subs SET expires=?, remind_for=NULL WHERE user_id=10", (int(time.time()) - 60,))
        await B.reminder_job(ctx); self.assertTrue(any("تمام شد" in t for t in bot.texts(10)))



class OpenCmdTests(unittest.IsolatedAsyncioTestCase):
    async def test_open_signals_subscribers_only(self):
        ctx, cfg, db, bot = make_ctx(); db.grant(10, 5)
        db.insert_signal("BTCUSDT", 1, "LONG", 100, 99, [101, 102, 103, 104, 105], 2, "BOS", "15m")
        u = make_update(555); await B.open_cmd(u, ctx); self.assertIn("/buy", reply_text(u)); self.assertNotIn("BTCUSDT", reply_text(u))
        u = make_update(10); await B.open_cmd(u, ctx); t = reply_text(u)
        self.assertIn("BTCUSDT", t); self.assertIn("TP1: 101", t); self.assertIn("دنبال", t)
        u = make_update(1); await B.open_cmd(u, ctx); self.assertIn("BTCUSDT", reply_text(u))          # ادمین
        ctx2, _, db2, _ = make_ctx(); db2.grant(10, 5); u = make_update(10); await B.open_cmd(u, ctx2); self.assertIn("فعلاً", reply_text(u))


class BacktestCmdTests(unittest.IsolatedAsyncioTestCase):
    async def run_cmd(self, args, uid=1):
        ctx, cfg, db, bot = make_ctx(args=args); u = make_update(uid)
        df = smc.synthetic_candles(3000, 4)
        async def fake_run(client, symbols, tf, days, min_score, fee, url, max_hold=150, timeout=10, retries=3, **kw):
            self.kw = kw
            self.seen = (symbols, days, min_score)
            return B.bt.run_backtest(df, 1, fee), (["SUIUSDT"] if "ALL" in args else [])
        orig = B.bt.run_symbols; B.bt.run_symbols = fake_run; self.addCleanup(setattr, B.bt, 'run_symbols', orig)
        await B.backtest_cmd(u, ctx); return u, bot

    async def test_runs_and_reports_with_failed_symbols(self):
        u, bot = await self.run_cmd(["ALL", "30"])
        texts = [c.args[0] for c in u.message.reply_text.call_args_list]
        self.assertIn("⏳", texts[0]); self.assertIn("بک‌تست", texts[-1]); self.assertIn("SUIUSDT", texts[-1])
        self.assertEqual(len(self.seen[0]), 10); self.assertEqual(bot.sent, [])

    async def test_validation_and_admin_only(self):
        for args in (["DOGEBTC", "30"], ["BTCUSDT", "1"], ["BTCUSDT", "999"]):
            u, _ = await self.run_cmd(args); self.assertIn("استفاده", reply_text(u))
        u, _ = await self.run_cmd(["BTCUSDT", "30"], uid=555); self.assertFalse(u.message.reply_text.called)
        u, _ = await self.run_cmd([], uid=1); self.assertEqual(self.seen[0], ["BTCUSDT"]); self.assertEqual(self.seen[1], 30)


class BackupAndWeeklyTests(unittest.IsolatedAsyncioTestCase):
    async def test_backup_command_sends_valid_sqlite_file_to_admin_only(self):
        import sqlite3, tempfile, os
        ctx, cfg, db, bot = make_ctx(); db.record_payment("keep", 10, 500, "month", 30)
        u = make_update(555); await B.backup_cmd(u, ctx); self.assertEqual(getattr(bot, "documents", []), [])
        await B.backup_cmd(make_update(1), ctx)
        chat, name, blob = bot.documents[0]; self.assertEqual(chat, 1); self.assertTrue(name.endswith(".db"))
        p = os.path.join(tempfile.mkdtemp(), "x.db")
        with open(p, "wb") as fh: fh.write(blob)
        self.assertEqual(sqlite3.connect(p).execute("SELECT stars FROM payments WHERE charge_id='keep'").fetchone()[0], 500)

    async def test_backup_failure_is_handled(self):
        ctx, cfg, db, bot = make_ctx()
        async def boom(**kw): raise TelegramError("x")
        bot.send_document = boom; u = make_update(1); await B.backup_cmd(u, ctx); self.assertIn("ناموفق", reply_text(u))

    async def test_weekly_job_schedule_report_and_backup(self):
        ctx, cfg, db, bot = make_ctx(); db.grant(10, 30)
        await B.weekly_job(ctx)                                    # اولین اجرا: فقط ثبت نقطه‌ی شروع
        self.assertEqual((bot.sent, getattr(bot, "documents", [])), ([], [])); self.assertIsNotNone(db.get_meta("last_weekly"))
        await B.weekly_job(ctx); self.assertEqual(bot.sent, [])    # هنوز ۷ روز نشده
        db.set_meta("last_weekly", int(time.time()) - 8 * 86400)
        sid = db.insert_signal("X", 1, "LONG", 100, 99, [1] * 5, 2, "BOS", "15m"); db.update_signal(sid, 3, 2, "sl", 0.2)
        await B.weekly_job(ctx)
        self.assertTrue(any("گزارش هفتگی" in t for t in bot.texts(10))); self.assertEqual(len(bot.documents), 1)
        n = len(bot.sent); await B.weekly_job(ctx); self.assertEqual(len(bot.sent), n)                  # دوباره ارسال نمی‌شود


class WiringTests(unittest.TestCase):
    def test_build_application_registers_everything(self):
        ctx, cfg, db, bot = make_ctx()
        app = B.build_application(cfg, db)
        self.assertGreaterEqual(app.add_handler.call_count, 22)       # 19 دستور + callback + precheckout + payment
        self.assertEqual(app.job_queue.run_repeating.call_count, 3)
        self.assertIs(app.bot_data.update.call_args.kwargs["db"], db)


class RedactionTests(unittest.TestCase):
    def test_token_is_redacted_from_logs(self):
        import logging
        f = B.RedactingFormatter("123:SECRET")
        rec = logging.LogRecord("x", logging.ERROR, "", 0, "url https://api.telegram.org/bot123:SECRET/getMe", None, None)
        self.assertNotIn("SECRET", f.format(rec))


if __name__ == "__main__":
    unittest.main()
