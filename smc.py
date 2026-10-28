"""
موتور Smart Money Concepts (SMC)

توجه: این منطق «اکتشافی/تقریبی» است و نسخه‌ی ساده‌شده‌ی مفاهیم SMC است، نه تعریف رسمی و
یکتا. هیچ ادعای دقت یا سودآوری ندارد؛ فقط روی داده‌ی واقعی بک‌تست و فوروارد‌تست می‌شود.

قواعد:
  1) پیوت سوینگ (های/لو) با تایید n کندل بعد  -> بدون نگاه به آینده
  2) BOS (ادامه روند) / CHoCH (تغییر روند) با «بسته‌شدن کندل» بالا/پایین آخرین پیوت
  3) Order Block = آخرین کندل مخالف قبل از حرکت شکننده‌ی ساختار
  4) تاییدیه‌ها: FVG، Liquidity Sweep، CHoCH  (score = 1 + تعداد تاییدیه‌ها)
  5) فیلتر Premium/Discount: خرید در نیمه‌ی پایین رنج، فروش در نیمه‌ی بالا
  6) سیگنال = اولین تماس قیمت با OB معتبر؛ SL پشت OB؛ TP1..TP5 = 1R..5R
  7) قاعده‌ی محافظه‌کارانه: اگر یک کندل هم SL و هم TP را لمس کند، SL اول حساب می‌شود.
  8) گزینه‌ی اختیاری BE_AFTER_TP1: بعد از TP1 حد ضرر به نقطه‌ی ورود می‌رود (با بک‌تست مقایسه کنید).
"""
from __future__ import annotations
import numpy as np
import pandas as pd

TP_MULTIPLES = (1.0, 2.0, 3.0, 4.0, 5.0)  # TP1..TP5 بر حسب R
TF_MS = {"1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
         "1h": 3_600_000, "2h": 7_200_000, "4h": 14_400_000}
MIN_CANDLES = 60


# ---------------------------------------------------------------- داده
def validate_candles(df: pd.DataFrame) -> None:
    """اگر داده نامعتبر باشد ValueError می‌دهد (NaN، قیمت غیرمثبت، های/لو ناسازگار، زمان نامرتب)."""
    if not isinstance(df, pd.DataFrame) or not {"t", "o", "h", "l", "c"}.issubset(df.columns):
        raise ValueError("ستون‌های t,o,h,l,c لازم است")
    v = df[["o", "h", "l", "c"]].to_numpy(dtype=float)
    if not np.isfinite(v).all():
        raise ValueError("داده شامل NaN/inf است")
    if (v <= 0).any():
        raise ValueError("قیمت غیرمثبت")
    o, h, l, c = v.T
    eps = 1e-9 * h
    if ((h + eps) < np.maximum(o, c)).any() or ((l - eps) > np.minimum(o, c)).any():
        raise ValueError("های/لو با open/close ناسازگار است")
    t = df["t"].to_numpy(dtype=float)
    if len(t) > 1 and not (np.diff(t) > 0).all():
        raise ValueError("زمان کندل‌ها باید صعودی و یکتا باشد")


def klines_to_df(raw, now_ms: int | None = None) -> pd.DataFrame:
    """پاسخ خام klines بایننس -> DataFrame معتبر. فقط کندل‌های بسته‌شده (ct < now_ms) نگه داشته می‌شوند."""
    if not isinstance(raw, list) or not raw:
        raise ValueError("پاسخ klines خالی یا نامعتبر")
    try:
        rows = [(r[0], r[1], r[2], r[3], r[4], r[6]) for r in raw]
    except (IndexError, TypeError, KeyError) as e:
        raise ValueError("ساختار klines نامعتبر") from e
    df = pd.DataFrame(rows, columns=["t", "o", "h", "l", "c", "ct"])
    for col in df.columns:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    if now_ms is not None:
        df = df[df["ct"] < now_ms]
    df = df.reset_index(drop=True)
    if len(df):
        validate_candles(df)
    return df


def synthetic_candles(n: int = 3000, seed: int = 0, drift_switch: int = 120, trending: bool = True) -> pd.DataFrame:
    """داده‌ی مصنوعی برای تست/آزمایش (نه بازار واقعی). trending=False یعنی بدون روند."""
    rng = np.random.default_rng(seed)
    price, rows, drift = 100.0, [], 0.0
    for i in range(n):
        if trending and i % drift_switch == 0:
            drift = rng.normal(0, 0.0015)
        vol = 0.004 * (1 + 0.5 * np.sin(i / 200))
        o = price
        c = o * (1 + drift + rng.normal(0, vol))
        h = max(o, c) * (1 + abs(rng.normal(0, vol / 2)))
        l = min(o, c) * (1 - abs(rng.normal(0, vol / 2)))
        rows.append((i * 900_000, o, h, l, c))
        price = c
    return pd.DataFrame(rows, columns=["t", "o", "h", "l", "c"])


# ---------------------------------------------------------------- اندیکاتورها
def _atr(df: pd.DataFrame, period: int = 14) -> np.ndarray:
    c = df["c"]
    tr = pd.concat([df["h"] - df["l"], (df["h"] - c.shift()).abs(), (df["l"] - c.shift()).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean().to_numpy()


def _find_fvg(h, l, start: int, end: int, side: str) -> bool:
    """آیا بین start..end یک FVG هم‌جهت وجود دارد؟"""
    for k in range(max(start + 2, 2), end + 1):
        if side == "LONG" and l[k] > h[k - 2]:
            return True
        if side == "SHORT" and h[k] < l[k - 2]:
            return True
    return False


# ---------------------------------------------------------------- تحلیل
def analyze(df: pd.DataFrame, n: int = 5, max_age: int = 40, require_pd: bool = True,
            tp_multiples=TP_MULTIPLES) -> list[dict]:
    """
    روی همه‌ی کندل‌های (بسته‌شده) اجرا می‌شود و همه‌ی سیگنال‌ها را برمی‌گرداند.
    در استفاده‌ی زنده فقط سیگنال‌هایی با idx == آخرین کندل مهم‌اند.
    داده‌ی خالی/کوتاه -> []   |   داده‌ی نامعتبر -> ValueError
    """
    if df is None or len(df) < 2 * n + 20:
        return []
    validate_candles(df)
    o, h, l, c = (df[x].to_numpy(dtype=float) for x in ("o", "h", "l", "c"))
    t = df["t"].to_numpy()
    atr = _atr(df)
    N = len(df)

    trend = 0
    last_sh = last_sl = prev_sl = prev_sh = None  # (idx, price)
    sh_broken = sl_broken = True
    zones: list[dict] = []
    signals: list[dict] = []

    for i in range(N):
        p = i - n  # پیوت در p با داده‌ی تا i تایید می‌شود
        if p >= n:
            win_h, win_l = h[p - n:p + n + 1], l[p - n:p + n + 1]
            if h[p] >= win_h.max() and (win_h == h[p]).sum() == 1:
                prev_sh, last_sh, sh_broken = last_sh, (p, h[p]), False
            if l[p] <= win_l.min() and (win_l == l[p]).sum() == 1:
                prev_sl, last_sl, sl_broken = last_sl, (p, l[p]), False

        if last_sh and not sh_broken and c[i] > last_sh[1]:  # شکست صعودی
            kind = "CHoCH" if trend == -1 else "BOS"
            trend, sh_broken = 1, True
            start = last_sl[0] if last_sl else max(0, i - 25)
            ob = next((j for j in range(i - 1, start - 1, -1) if c[j] < o[j]), None)
            if ob is not None and last_sl:
                rng_hi, rng_lo = h[start:i + 1].max(), last_sl[1]
                zones.append(dict(
                    side="LONG", top=h[ob], bottom=l[ob], created=i, ob_idx=ob, kind=kind,
                    fvg=_find_fvg(h, l, start, i, "LONG"),
                    swept=bool(prev_sl and last_sl[1] < prev_sl[1]),
                    eq=(rng_hi + rng_lo) / 2, used=False))

        if last_sl and not sl_broken and c[i] < last_sl[1]:  # شکست نزولی
            kind = "CHoCH" if trend == 1 else "BOS"
            trend, sl_broken = -1, True
            start = last_sh[0] if last_sh else max(0, i - 25)
            ob = next((j for j in range(i - 1, start - 1, -1) if c[j] > o[j]), None)
            if ob is not None and last_sh:
                rng_lo, rng_hi = l[start:i + 1].min(), last_sh[1]
                zones.append(dict(
                    side="SHORT", top=h[ob], bottom=l[ob], created=i, ob_idx=ob, kind=kind,
                    fvg=_find_fvg(h, l, start, i, "SHORT"),
                    swept=bool(prev_sh and last_sh[1] > prev_sh[1]),
                    eq=(rng_hi + rng_lo) / 2, used=False))

        for z in zones:
            if z["used"] or z["created"] >= i:
                continue
            if i - z["created"] > max_age:
                z["used"] = True
                continue
            long_ = z["side"] == "LONG"
            if (long_ and c[i] < z["bottom"]) or (not long_ and c[i] > z["top"]):
                z["used"] = True  # OB با بسته‌شدن کندل شکسته شد
                continue
            touched = (l[i] <= z["top"]) if long_ else (h[i] >= z["bottom"])
            if not touched:
                continue
            z["used"] = True  # فقط اولین تماس
            mid = (z["top"] + z["bottom"]) / 2
            if require_pd and ((long_ and mid > z["eq"]) or (not long_ and mid < z["eq"])):
                continue
            entry = c[i]
            sl = (z["bottom"] - 0.1 * atr[i]) if long_ else (z["top"] + 0.1 * atr[i])
            risk = abs(entry - sl)
            if not (0.3 * atr[i] <= risk <= 4 * atr[i]):
                continue
            m = 1 if long_ else -1
            score = 1 + int(z["fvg"]) + int(z["swept"]) + int(z["kind"] == "CHoCH")
            signals.append(dict(
                idx=i, candle=int(t[i]), side=z["side"], entry=float(entry), sl=float(sl),
                tps=[float(entry + m * risk * x) for x in tp_multiples],
                kind=z["kind"], fvg=z["fvg"], swept=z["swept"], score=score,
                ob_top=float(z["top"]), ob_bottom=float(z["bottom"])))
        if len(zones) > 64:
            zones = [z for z in zones if not z["used"]]
    return signals


# ---------------------------------------------------------------- پیگیری نتیجه
def advance(side: str, sl: float, tps, hits: int, highs, lows, entry: float | None = None,
            be_after_tp1: bool = False):
    """
    کندل‌های جدید را به ترتیب پردازش می‌کند.  برمی‌گرداند: (hits, events, outcome, consumed)
    events: لیست ("tp", k) یا ("sl", None) یا ("be", None) به ترتیب وقوع
    outcome: None (هنوز باز) | "sl" | "be" (برگشت به ورود بعد از TP1) | "done" (TP5)
    قاعده‌ی محافظه‌کارانه: در هر کندل ابتدا حد ضرر بررسی می‌شود، بعد TPها.
    be_after_tp1: بعد از TP1 حد ضرر به نقطه‌ی ورود منتقل می‌شود (از کندل «بعد» از لمس TP1 اعمال می‌شود).
    """
    long_ = side == "LONG"
    events, outcome, consumed = [], None, 0
    for k in range(len(highs)):
        consumed = k + 1
        be_active = be_after_tp1 and hits >= 1 and entry is not None
        stop = entry if be_active else sl
        if (long_ and lows[k] <= stop) or (not long_ and highs[k] >= stop):
            events.append(("be", None) if be_active else ("sl", None))
            outcome = "be" if be_active else "sl"
            break
        while hits < 5 and ((long_ and highs[k] >= tps[hits]) or (not long_ and lows[k] <= tps[hits])):
            hits += 1
            events.append(("tp", hits))
        if hits == 5:
            outcome = "done"
            break
    return hits, events, outcome, consumed


def realized_r(hits: int, outcome: str | None, entry: float, sl: float, side: str, last_close: float | None = None) -> float:
    """نتیجه بر حسب R با فرض خروج ۲۰٪ در هر TP. outcome: sl | done | expired | None"""
    base = sum(0.2 * TP_MULTIPLES[k] for k in range(hits))
    rest = 0.2 * (5 - hits)
    risk = abs(entry - sl)
    if outcome == "sl":
        return base - rest
    if outcome == "be":
        return base
    if outcome == "expired" and last_close is not None and risk > 0:
        unreal = (last_close - entry) / risk if side == "LONG" else (entry - last_close) / risk
        return base + rest * unreal
    return base


def simulate(sig: dict, df: pd.DataFrame, max_hold: int = 150, be_after_tp1: bool = False) -> dict:
    """شبیه‌سازی نتیجه‌ی یک سیگنال روی کندل‌های بعدی (برای بک‌تست)."""
    start = sig["idx"] + 1
    end = min(len(df), start + max_hold)
    if start >= end:
        return dict(hits=0, stopped=False, r=0.0, outcome="open", closed_idx=sig["idx"])
    hi = df["h"].to_numpy(float)[start:end]
    lo = df["l"].to_numpy(float)[start:end]
    hits, _, outcome, consumed = advance(sig["side"], sig["sl"], sig["tps"], 0, hi, lo,
                                         entry=sig["entry"], be_after_tp1=be_after_tp1)
    final = outcome or "expired"
    last_close = float(df["c"].iloc[start + consumed - 1])
    r = realized_r(hits, final, sig["entry"], sig["sl"], sig["side"], last_close)
    return dict(hits=hits, stopped=outcome in ("sl", "be"), r=r, outcome=final, closed_idx=start + consumed - 1)
