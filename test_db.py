import os, sqlite3, tempfile, threading, unittest
from database import Database, DAY

NOW = 1_800_000_000


class SubscriptionTests(unittest.TestCase):
    def setUp(self): self.db = Database(":memory:")

    def test_extend_from_now_when_expired_and_from_expiry_when_active(self):
        e1 = self.db.grant(1, 30, now=NOW)
        self.assertEqual(e1, NOW + 30 * DAY)
        e2 = self.db.grant(1, 30, now=NOW + 10 * DAY)            # هنوز فعال -> از انقضا
        self.assertEqual(e2, NOW + 60 * DAY)
        e3 = self.db.grant(1, 7, now=NOW + 100 * DAY)            # منقضی -> از اکنون
        self.assertEqual(e3, NOW + 107 * DAY)

    def test_is_active_and_lists(self):
        self.db.grant(1, 1, now=NOW)
        self.assertTrue(self.db.is_active(1, NOW + 100)); self.assertFalse(self.db.is_active(1, NOW + DAY + 1))
        self.assertEqual(self.db.active_user_ids(NOW + 100), [1]); self.assertEqual(self.db.active_user_ids(NOW + 2 * DAY), [])

    def test_blocked_users_excluded_until_restart(self):
        self.db.upsert_user(1, "a", NOW); self.db.grant(1, 5, now=NOW)
        self.db.set_blocked(1)
        self.assertEqual(self.db.active_user_ids(NOW + 1), [])
        self.db.upsert_user(1, "a", NOW)                          # /start دوباره
        self.assertEqual(self.db.active_user_ids(NOW + 1), [1])

    def test_trial_once_only_and_not_after_payment(self):
        self.assertTrue(self.db.start_trial(1, 1, now=NOW))
        self.assertFalse(self.db.start_trial(1, 1, now=NOW + 5 * DAY))
        self.db.record_payment("c", 2, 500, "month", 30, now=NOW)
        self.assertFalse(self.db.start_trial(2, 1, now=NOW + 40 * DAY))
        self.db.grant(3, 10, now=NOW)
        self.assertFalse(self.db.start_trial(3, 1, now=NOW + DAY))  # اشتراک فعال دارد


class PaymentTests(unittest.TestCase):
    def setUp(self): self.db = Database(":memory:")

    def test_duplicate_charge_does_not_extend(self):
        r1 = self.db.record_payment("ch1", 5, 500, "month", 30, now=NOW)
        r2 = self.db.record_payment("ch1", 5, 500, "month", 30, now=NOW + 5)
        self.assertEqual((r1["status"], r2["status"]), ("ok", "duplicate"))
        self.assertEqual(self.db.expires_at(5), NOW + 30 * DAY)
        self.assertEqual(self.db.stats(NOW)["payments"], 1)

    def test_threads_race_same_charge_only_one_wins(self):
        results = []
        def worker(): results.append(self.db.record_payment("race", 9, 500, "month", 30, now=NOW)["status"])
        ts = [threading.Thread(target=worker) for _ in range(12)]
        [t.start() for t in ts]; [t.join() for t in ts]
        self.assertEqual(results.count("ok"), 1); self.assertEqual(results.count("duplicate"), 11)
        self.assertEqual(self.db.expires_at(9), NOW + 30 * DAY)

    def test_failure_inside_transaction_rolls_back(self):
        with self.assertRaises(sqlite3.IntegrityError):
            with self.db.tx() as c:
                c.execute("INSERT INTO payments(charge_id,user_id,stars,ts) VALUES('x',1,1,1)")
                c.execute("INSERT INTO payments(charge_id,user_id,stars,ts) VALUES('x',1,1,1)")
        self.assertIsNone(self.db.get_payment("x"))               # هیچ ثبت نیمه‌کاره‌ای نمانده
        self.assertEqual(self.db.stats(NOW)["payments"], 0)

    def test_referral_reward_once_on_first_payment_only(self):
        self.db.upsert_user(10, "ref", NOW); self.db.upsert_user(20, "new", NOW)
        self.assertTrue(self.db.set_referrer(20, 10))
        self.assertFalse(self.db.set_referrer(20, 10))
        r = self.db.record_payment("a", 20, 500, "month", 30, ref_bonus_days=3, now=NOW)
        self.assertEqual(r["referrer"], 10); self.assertEqual(self.db.expires_at(10), NOW + 3 * DAY)
        r = self.db.record_payment("b", 20, 500, "month", 30, ref_bonus_days=3, now=NOW + 1)
        self.assertIsNone(r["referrer"]); self.assertEqual(self.db.expires_at(10), NOW + 3 * DAY)

    def test_self_referral_and_unknown_referrer_rejected(self):
        self.db.upsert_user(1, "a", NOW)
        self.assertFalse(self.db.set_referrer(1, 1)); self.assertFalse(self.db.set_referrer(1, 999))

    def test_refund_reduces_days_and_is_idempotent(self):
        self.db.record_payment("a", 1, 500, "month", 30, now=NOW)
        r = self.db.mark_refunded("a", now=NOW + DAY)
        self.assertEqual(r["status"], "ok"); self.assertEqual(self.db.expires_at(1), NOW + DAY)
        self.assertEqual(self.db.mark_refunded("a")["status"], "already")
        self.assertEqual(self.db.mark_refunded("zzz")["status"], "not_found")
        self.assertEqual(self.db.stats(NOW)["revenue"], 0)


class SignalTests(unittest.TestCase):
    def setUp(self): self.db = Database(":memory:")

    def test_unique_signal_and_open_flag(self):
        a = self.db.insert_signal("BTCUSDT", 1000, "LONG", 100, 99, [101, 102, 103, 104, 105], 2, "BOS", "15m", NOW)
        b = self.db.insert_signal("BTCUSDT", 1000, "LONG", 100, 99, [101, 102, 103, 104, 105], 2, "BOS", "15m", NOW)
        self.assertIsNotNone(a); self.assertIsNone(b)
        self.assertTrue(self.db.has_open("BTCUSDT", "LONG")); self.assertFalse(self.db.has_open("BTCUSDT", "SHORT"))

    def test_closed_signal_cannot_be_reopened_or_changed(self):
        sid = self.db.insert_signal("X", 1, "LONG", 100, 99, [1] * 5, 2, "BOS", "15m", NOW)
        self.db.update_signal(sid, 2, 5, "sl", -0.2)
        self.db.update_signal(sid, 5, 9, "done", 3.0)             # باید نادیده گرفته شود
        row = self.db.conn.execute("SELECT hits,status,r FROM signals WHERE id=?", (sid,)).fetchone()
        self.assertEqual((row["hits"], row["status"], row["r"]), (2, "sl", -0.2))

    def test_performance(self):
        for i, (hits, st, r) in enumerate([(5, "done", 3.0), (0, "sl", -1.0), (2, "sl", 0.0)]):
            sid = self.db.insert_signal("X", i, "LONG", 100, 99, [1] * 5, 2, "BOS", "15m", NOW)
            self.db.update_signal(sid, hits, i, st, r)
        p = self.db.performance()
        self.assertEqual(p["n"], 3); self.assertAlmostEqual(p["avg_r"], 2 / 3); self.assertAlmostEqual(p["tp5"], 1 / 3)


class MetaBackupTests(unittest.TestCase):
    def test_meta_roundtrip_and_backup_file(self):
        db = Database(":memory:"); self.assertIsNone(db.get_meta("k")); db.set_meta("k", 5); self.assertEqual(db.get_meta("k"), "5")
        db.record_payment("c", 1, 500, "month", 30, now=NOW)
        path = os.path.join(tempfile.mkdtemp(), "b.db"); db.backup_to(path)
        self.assertEqual(Database(path).get_payment("c")["stars"], 500)

    def test_performance_since_window(self):
        db = Database(":memory:")
        for i, ts in enumerate((NOW, NOW + 10 * DAY)):
            sid = db.insert_signal("X", i, "LONG", 100, 99, [1] * 5, 2, "BOS", "15m", ts)
            db.update_signal(sid, 1, i, "sl", -0.5, now=ts)
        self.assertEqual(db.performance()["n"], 2); self.assertEqual(db.performance(since=NOW + 5 * DAY)["n"], 1)


class MigrationTests(unittest.TestCase):
    def test_old_v1_database_is_preserved(self):
        path = os.path.join(tempfile.mkdtemp(), "old.db")
        old = sqlite3.connect(path)
        old.executescript("""
        CREATE TABLE subs(user_id INTEGER PRIMARY KEY, expires INTEGER);
        CREATE TABLE payments(charge_id TEXT PRIMARY KEY, user_id INTEGER, stars INTEGER, ts INTEGER);
        CREATE TABLE signals(id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT, candle INTEGER, side TEXT, entry REAL,
            sl REAL, tps TEXT, hits INTEGER DEFAULT 0, status TEXT DEFAULT 'open', UNIQUE(symbol, candle, side));
        INSERT INTO subs VALUES(7, 4102444800);
        INSERT INTO payments VALUES('oldcharge', 7, 500, 1700000000);
        INSERT INTO signals(symbol,candle,side,entry,sl,tps,hits,status) VALUES('BTCUSDT',123,'LONG',100,99,'[101,102,103,104,105]',2,'open');
        """); old.commit(); old.close()
        db = Database(path); db2 = Database(path)                  # مهاجرت دوباره بی‌خطر است
        self.assertEqual(db.expires_at(7), 4102444800)
        self.assertEqual(db.get_payment("oldcharge")["stars"], 500)
        s = db.open_signals()[0]
        self.assertEqual((s["hits"], s["last_t"]), (2, 123))
        self.assertEqual(db.stats(NOW)["users"], 1)
        self.assertEqual(db.record_payment("oldcharge", 7, 500, "month", 30)["status"], "duplicate")
        db.close(); db2.close()


class ReminderTests(unittest.TestCase):
    def test_reminder_once_per_expiry(self):
        db = Database(":memory:"); db.grant(1, 1, now=NOW)
        due = db.expiring_soon(NOW + 100, 2 * DAY)
        self.assertEqual(len(due), 1)
        db.mark_reminded(*due[0]); self.assertEqual(db.expiring_soon(NOW + 200, 2 * DAY), [])
        db.grant(1, 5, now=NOW + 300)                              # تمدید -> انقضای جدید -> یادآوری جدید ممکن
        self.assertEqual(len(db.expiring_soon(NOW + 5 * DAY, 2 * DAY)), 1)
        exp = db.just_expired(NOW + 7 * DAY); self.assertEqual(len(exp), 1)
        db.mark_expired_notified(*exp[0]); self.assertEqual(db.just_expired(NOW + 7 * DAY), [])


if __name__ == "__main__":
    unittest.main()
