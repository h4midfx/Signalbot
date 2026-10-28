"""
لایه‌ی دیتابیس SQLite
- مهاجرت سازگار با دیتابیس نسخه‌ی قبلی (فقط جدول/ستون اضافه می‌شود، هیچ داده‌ای حذف نمی‌شود)
- همه‌ی عملیات مهم داخل تراکنش اتمیک (BEGIN IMMEDIATE) و با کوئری پارامتری
- زمان همیشه epoch ثانیه‌ی UTC است و برای تست‌پذیری قابل تزریق (now=...)
"""
from __future__ import annotations
import json
import sqlite3
import threading
import time
from contextlib import contextmanager

DAY = 86400

BASE_SCHEMA = """
CREATE TABLE IF NOT EXISTS subs(user_id INTEGER PRIMARY KEY, expires INTEGER);
CREATE TABLE IF NOT EXISTS payments(charge_id TEXT PRIMARY KEY, user_id INTEGER, stars INTEGER, ts INTEGER);
CREATE TABLE IF NOT EXISTS signals(
  id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT, candle INTEGER, side TEXT, entry REAL, sl REAL,
  tps TEXT, hits INTEGER DEFAULT 0, status TEXT DEFAULT 'open', UNIQUE(symbol, candle, side));
CREATE TABLE IF NOT EXISTS users(user_id INTEGER PRIMARY KEY, username TEXT, joined_ts INTEGER);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS promos(code TEXT PRIMARY KEY, days INTEGER, max_uses INTEGER, used INTEGER DEFAULT 0,
  expires_ts INTEGER, created_ts INTEGER);
CREATE TABLE IF NOT EXISTS promo_uses(code TEXT, user_id INTEGER, ts INTEGER, PRIMARY KEY(code, user_id));
"""

ADDED_COLUMNS = {
    "users": {"referred_by": "INTEGER", "trial_used": "INTEGER DEFAULT 0",
              "blocked": "INTEGER DEFAULT 0", "ref_rewarded": "INTEGER DEFAULT 0",
              "min_score": "INTEGER", "side_filter": "TEXT", "progress_off": "INTEGER DEFAULT 0"},
    "subs": {"remind_for": "INTEGER", "expired_for": "INTEGER"},
    "payments": {"plan_id": "TEXT", "days": "INTEGER", "refunded": "INTEGER DEFAULT 0"},
    "signals": {"score": "INTEGER", "kind": "TEXT", "tf": "TEXT", "created_ts": "INTEGER",
                "closed_ts": "INTEGER", "last_t": "INTEGER", "r": "REAL"},
}


def _now(now):
    return int(time.time()) if now is None else int(now)


class Database:
    def __init__(self, path: str = ":memory:"):
        self.conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        if path != ":memory:":
            self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self._migrate()

    # ------------------------------------------------------------ زیرساخت
    @contextmanager
    def tx(self):
        with self._lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield self.conn
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise
            else:
                self.conn.execute("COMMIT")

    def _migrate(self):
        with self.tx() as c:
            for stmt in BASE_SCHEMA.split(";"):
                if stmt.strip():
                    c.execute(stmt)
            for table, cols in ADDED_COLUMNS.items():
                have = {r["name"] for r in c.execute(f"PRAGMA table_info({table})")}
                for col, ddl in cols.items():
                    if col not in have:
                        c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
            # پر کردن users از داده‌های قدیمی (بدون حذف یا تغییر چیزی)
            c.execute("INSERT OR IGNORE INTO users(user_id, joined_ts) SELECT user_id, ts FROM payments")
            c.execute("INSERT OR IGNORE INTO users(user_id, joined_ts) SELECT user_id, expires FROM subs")
            c.execute("UPDATE signals SET last_t = candle WHERE last_t IS NULL")

    def close(self):
        self.conn.close()

    # ------------------------------------------------------------ کاربران
    def upsert_user(self, uid: int, username: str | None, now=None) -> bool:
        """True اگر کاربر جدید بود. /start دوباره، وضعیت blocked را پاک می‌کند."""
        with self.tx() as c:
            if c.execute("SELECT 1 FROM users WHERE user_id=?", (uid,)).fetchone():
                c.execute("UPDATE users SET username=?, blocked=0 WHERE user_id=?", (username, uid))
                return False
            c.execute("INSERT INTO users(user_id, username, joined_ts) VALUES(?,?,?)", (uid, username, _now(now)))
            return True

    def set_blocked(self, uid: int, blocked: bool = True):
        with self.tx() as c:
            c.execute("UPDATE users SET blocked=? WHERE user_id=?", (int(blocked), uid))

    def set_referrer(self, uid: int, ref: int) -> bool:
        """فقط یک‌بار، فقط برای کاربر بدون پرداخت، و نه خود شخص."""
        if uid == ref:
            return False
        with self.tx() as c:
            if not c.execute("SELECT 1 FROM users WHERE user_id=?", (ref,)).fetchone():
                return False
            me = c.execute("SELECT referred_by, trial_used FROM users WHERE user_id=?", (uid,)).fetchone()
            if not me or me["referred_by"] is not None or me["trial_used"]:
                return False
            if c.execute("SELECT 1 FROM payments WHERE user_id=?", (uid,)).fetchone():
                return False
            c.execute("UPDATE users SET referred_by=? WHERE user_id=?", (ref, uid))
            return True

    # ------------------------------------------------------------ تنظیمات کاربر
    def get_settings(self, uid: int) -> dict:
        r = self.conn.execute("SELECT min_score, side_filter, progress_off FROM users WHERE user_id=?", (uid,)).fetchone()
        return {"min_score": (r["min_score"] if r else None), "side": ((r["side_filter"] if r else None) or "ALL"),
                "progress": not (r["progress_off"] if r else 0)}

    def set_setting(self, uid: int, key: str, value) -> bool:
        """key: min_score (1..4 یا None) | side (ALL/LONG/SHORT) | progress (bool). مقدار نامعتبر = False."""
        if key == "min_score":
            if value is not None and value not in (1, 2, 3, 4):
                return False
            col, val = "min_score", value
        elif key == "side":
            if value not in ("ALL", "LONG", "SHORT"):
                return False
            col, val = "side_filter", value
        elif key == "progress":
            col, val = "progress_off", 0 if value else 1
        else:
            return False
        with self.tx() as c:
            c.execute("INSERT OR IGNORE INTO users(user_id, joined_ts) VALUES(?,?)", (uid, _now(None)))
            c.execute(f"UPDATE users SET {col}=? WHERE user_id=?", (val, uid))
        return True

    def recipients_for_signal(self, score: int, side: str, progress: bool = False, now=None) -> list[int]:
        """مشترکان فعال و غیربلاک که فیلتر شخصی‌شان (حداقل امتیاز/جهت) این سیگنال را می‌پذیرد."""
        q = ("SELECT s.user_id FROM subs s LEFT JOIN users u ON u.user_id=s.user_id "
             "WHERE s.expires>? AND COALESCE(u.blocked,0)=0 AND COALESCE(u.min_score,0)<=? "
             "AND COALESCE(u.side_filter,'ALL') IN ('ALL',?)")
        if progress:
            q += " AND COALESCE(u.progress_off,0)=0"
        return [r[0] for r in self.conn.execute(q, (_now(now), int(score or 0), side))]

    # ------------------------------------------------------------ کد تخفیف/هدیه
    def create_promo(self, code: str, days: int, max_uses: int, expires_ts: int | None = None, now=None) -> bool:
        with self.tx() as c:
            if c.execute("SELECT 1 FROM promos WHERE code=?", (code.upper(),)).fetchone():
                return False
            c.execute("INSERT INTO promos(code,days,max_uses,used,expires_ts,created_ts) VALUES(?,?,?,?,?,?)",
                      (code.upper(), days, max_uses, 0, expires_ts, _now(now)))
            return True

    def redeem_promo(self, code: str, uid: int, now=None) -> dict:
        """اتمیک: هر کاربر فقط یک‌بار از هر کد، با سقف تعداد استفاده و تاریخ انقضا."""
        now, code = _now(now), (code or "").upper()
        with self.tx() as c:
            p = c.execute("SELECT * FROM promos WHERE code=?", (code,)).fetchone()
            if not p:
                return {"status": "not_found"}
            if p["expires_ts"] and p["expires_ts"] < now:
                return {"status": "expired"}
            if p["used"] >= p["max_uses"]:
                return {"status": "exhausted"}
            if c.execute("SELECT 1 FROM promo_uses WHERE code=? AND user_id=?", (code, uid)).fetchone():
                return {"status": "already"}
            c.execute("INSERT INTO promo_uses(code,user_id,ts) VALUES(?,?,?)", (code, uid, now))
            c.execute("UPDATE promos SET used=used+1 WHERE code=?", (code,))
            return {"status": "ok", "days": p["days"], "expires": self._extend(c, uid, p["days"], now)}

    def list_promos(self) -> list[dict]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM promos ORDER BY created_ts DESC LIMIT 30")]

    def all_user_ids(self) -> list[int]:
        return [r[0] for r in self.conn.execute("SELECT user_id FROM users WHERE COALESCE(blocked,0)=0")]

    # ------------------------------------------------------------ اشتراک
    def expires_at(self, uid: int) -> int:
        r = self.conn.execute("SELECT expires FROM subs WHERE user_id=?", (uid,)).fetchone()
        return int(r["expires"]) if r and r["expires"] else 0

    def is_active(self, uid: int, now=None) -> bool:
        return self.expires_at(uid) > _now(now)

    def active_user_ids(self, now=None) -> list[int]:
        q = ("SELECT s.user_id FROM subs s LEFT JOIN users u ON u.user_id=s.user_id "
             "WHERE s.expires>? AND COALESCE(u.blocked,0)=0")
        return [r[0] for r in self.conn.execute(q, (_now(now),))]

    @staticmethod
    def _extend(c, uid: int, days: int, now: int) -> int:
        """تمدید از تاریخ انقضای فعلی (اگر فعال) یا از اکنون (اگر منقضی/نداشته)."""
        c.execute("INSERT OR IGNORE INTO users(user_id, joined_ts) VALUES(?,?)", (uid, now))
        row = c.execute("SELECT expires FROM subs WHERE user_id=?", (uid,)).fetchone()
        base = max(now, int(row["expires"] or 0)) if row else now
        new = base + int(days) * DAY
        if row:
            c.execute("UPDATE subs SET expires=? WHERE user_id=?", (new, uid))
        else:
            c.execute("INSERT INTO subs(user_id, expires) VALUES(?,?)", (uid, new))
        return new

    def grant(self, uid: int, days: int, now=None) -> int:
        with self.tx() as c:
            return self._extend(c, uid, days, _now(now))

    def start_trial(self, uid: int, days: int, now=None) -> bool:
        """تریال فقط یک‌بار؛ فقط برای کسی که هرگز پرداخت نکرده و اشتراک فعال ندارد."""
        now = _now(now)
        with self.tx() as c:
            c.execute("INSERT OR IGNORE INTO users(user_id, joined_ts) VALUES(?,?)", (uid, now))
            u = c.execute("SELECT trial_used FROM users WHERE user_id=?", (uid,)).fetchone()
            if u["trial_used"]:
                return False
            if c.execute("SELECT 1 FROM payments WHERE user_id=?", (uid,)).fetchone():
                return False
            row = c.execute("SELECT expires FROM subs WHERE user_id=?", (uid,)).fetchone()
            if row and row["expires"] and row["expires"] > now:
                return False
            c.execute("UPDATE users SET trial_used=1 WHERE user_id=?", (uid,))
            self._extend(c, uid, days, now)
            return True

    # ------------------------------------------------------------ پرداخت (اتمیک)
    def record_payment(self, charge_id: str, uid: int, stars: int, plan_id: str, days: int,
                       ref_bonus_days: int = 0, now=None) -> dict:
        """
        ثبت پرداخت + تمدید اشتراک (+ پاداش معرف) در «یک» تراکنش.
        اگر charge_id قبلاً ثبت شده باشد هیچ تغییری نمی‌دهد و status='duplicate' برمی‌گرداند.
        """
        now = _now(now)
        with self.tx() as c:
            if c.execute("SELECT 1 FROM payments WHERE charge_id=?", (charge_id,)).fetchone():
                return {"status": "duplicate", "expires": self.expires_at(uid), "referrer": None}
            c.execute("INSERT OR IGNORE INTO users(user_id, joined_ts) VALUES(?,?)", (uid, now))
            c.execute("INSERT INTO payments(charge_id,user_id,stars,ts,plan_id,days,refunded) VALUES(?,?,?,?,?,?,0)",
                      (charge_id, uid, stars, now, plan_id, days))
            expires = self._extend(c, uid, days, now)
            referrer = None
            if ref_bonus_days > 0:
                u = c.execute("SELECT referred_by, ref_rewarded FROM users WHERE user_id=?", (uid,)).fetchone()
                first = c.execute("SELECT COUNT(*) FROM payments WHERE user_id=?", (uid,)).fetchone()[0] == 1
                if u["referred_by"] and not u["ref_rewarded"] and first:
                    self._extend(c, u["referred_by"], ref_bonus_days, now)
                    c.execute("UPDATE users SET ref_rewarded=1 WHERE user_id=?", (uid,))
                    referrer = int(u["referred_by"])
            return {"status": "ok", "expires": expires, "referrer": referrer}

    def get_payment(self, charge_id: str):
        r = self.conn.execute("SELECT * FROM payments WHERE charge_id=?", (charge_id,)).fetchone()
        return dict(r) if r else None

    def mark_refunded(self, charge_id: str, now=None) -> dict:
        """بعد از بازگشت موفق Stars: پرداخت را refunded می‌کند و روزهای همان پرداخت را کم می‌کند."""
        now = _now(now)
        with self.tx() as c:
            p = c.execute("SELECT * FROM payments WHERE charge_id=?", (charge_id,)).fetchone()
            if not p:
                return {"status": "not_found"}
            if p["refunded"]:
                return {"status": "already"}
            c.execute("UPDATE payments SET refunded=1 WHERE charge_id=?", (charge_id,))
            row = c.execute("SELECT expires FROM subs WHERE user_id=?", (p["user_id"],)).fetchone()
            new = None
            if row and row["expires"]:
                new = max(now, int(row["expires"]) - int(p["days"] or 0) * DAY)
                if row["expires"] <= now:
                    new = int(row["expires"])
                c.execute("UPDATE subs SET expires=? WHERE user_id=?", (new, p["user_id"]))
            return {"status": "ok", "user_id": p["user_id"], "expires": new}

    # ------------------------------------------------------------ سیگنال
    def insert_signal(self, symbol, candle, side, entry, sl, tps, score, kind, tf, now=None):
        """id سیگنال جدید، یا None اگر تکراری باشد (UNIQUE روی symbol,candle,side)."""
        with self.tx() as c:
            cur = c.execute(
                "INSERT OR IGNORE INTO signals(symbol,candle,side,entry,sl,tps,score,kind,tf,created_ts,last_t) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (symbol, int(candle), side, entry, sl, json.dumps(tps), score, kind, tf, _now(now), int(candle)))
            return cur.lastrowid if cur.rowcount else None

    def has_open(self, symbol: str, side: str) -> bool:
        return self.conn.execute("SELECT 1 FROM signals WHERE symbol=? AND side=? AND status='open'",
                                 (symbol, side)).fetchone() is not None

    def open_signals(self) -> list[dict]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM signals WHERE status='open' ORDER BY id")]

    def update_signal(self, sid: int, hits: int, last_t: int, status: str = "open", r=None, now=None):
        with self.tx() as c:
            closed = _now(now) if status != "open" else None
            c.execute("UPDATE signals SET hits=?, last_t=?, status=?, r=COALESCE(?, r), closed_ts=COALESCE(?, closed_ts) "
                      "WHERE id=? AND status='open'", (hits, int(last_t), status, r, closed, sid))

    def performance(self, recent: int = 10, since: int | None = None) -> dict:
        """since: فقط سیگنال‌هایی که بعد از این زمان بسته شده‌اند (برای گزارش هفتگی)."""
        rows = self.conn.execute(
            "SELECT symbol, side, hits, status, r FROM signals "
            "WHERE status IN ('sl','done','expired','be') AND r IS NOT NULL AND (? IS NULL OR closed_ts>=?) "
            "ORDER BY id DESC", (since, since)).fetchall()
        n = len(rows)
        out = {"n": n, "open": self.conn.execute("SELECT COUNT(*) FROM signals WHERE status='open'").fetchone()[0],
               "recent": [dict(r) for r in rows[:recent]]}
        if n:
            out.update(
                tp1=sum(r["hits"] >= 1 for r in rows) / n, tp3=sum(r["hits"] >= 3 for r in rows) / n,
                tp5=sum(r["hits"] >= 5 for r in rows) / n, sl=sum(r["status"] == "sl" for r in rows) / n,
                avg_r=sum(r["r"] for r in rows) / n, win=sum(r["r"] > 0 for r in rows) / n)
        return out

    # ------------------------------------------------------------ متا و بکاپ
    def get_meta(self, key: str):
        r = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return r["value"] if r else None

    def set_meta(self, key: str, value: str):
        with self.tx() as c:
            c.execute("INSERT OR REPLACE INTO meta(key, value) VALUES(?,?)", (key, str(value)))

    def backup_to(self, path: str):
        """نسخه‌ی پشتیبان سازگار (حتی هنگام کار ربات) با API رسمی SQLite."""
        with self._lock:
            dest = sqlite3.connect(path)
            try:
                self.conn.backup(dest)
            finally:
                dest.close()

    # ------------------------------------------------------------ آمار و یادآوری
    def stats(self, now=None) -> dict:
        now = _now(now)
        q = lambda sql, *a: self.conn.execute(sql, a).fetchone()[0]
        rev = "SELECT COALESCE(SUM(stars),0) FROM payments WHERE COALESCE(refunded,0)=0"
        return dict(
            users=q("SELECT COUNT(*) FROM users"), blocked=q("SELECT COUNT(*) FROM users WHERE blocked=1"),
            active=q("SELECT COUNT(*) FROM subs WHERE expires>?", now),
            trials=q("SELECT COUNT(*) FROM users WHERE trial_used=1"),
            payers=q("SELECT COUNT(DISTINCT user_id) FROM payments WHERE COALESCE(refunded,0)=0"),
            payments=q("SELECT COUNT(*) FROM payments WHERE COALESCE(refunded,0)=0"),
            refunds=q("SELECT COUNT(*) FROM payments WHERE refunded=1"),
            revenue=q(rev), revenue_7d=q(rev + " AND ts>=?", now - 7 * DAY),
            revenue_30d=q(rev + " AND ts>=?", now - 30 * DAY))

    def expiring_soon(self, now, window: int):
        q = ("SELECT s.user_id, s.expires FROM subs s LEFT JOIN users u ON u.user_id=s.user_id "
             "WHERE s.expires>? AND s.expires<=? AND COALESCE(u.blocked,0)=0 "
             "AND (s.remind_for IS NULL OR s.remind_for!=s.expires)")
        return [(r[0], r[1]) for r in self.conn.execute(q, (now, now + window))]

    def mark_reminded(self, uid: int, expires: int):
        with self.tx() as c:
            c.execute("UPDATE subs SET remind_for=? WHERE user_id=?", (expires, uid))

    def just_expired(self, now, lookback: int = 3 * DAY):
        q = ("SELECT s.user_id, s.expires FROM subs s LEFT JOIN users u ON u.user_id=s.user_id "
             "WHERE s.expires<=? AND s.expires>? AND COALESCE(u.blocked,0)=0 "
             "AND (s.expired_for IS NULL OR s.expired_for!=s.expires)")
        return [(r[0], r[1]) for r in self.conn.execute(q, (now, now - lookback))]

    def mark_expired_notified(self, uid: int, expires: int):
        with self.tx() as c:
            c.execute("UPDATE subs SET expired_for=? WHERE user_id=?", (expires, uid))
