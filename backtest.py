"""
بک‌تست روی داده‌ی تاریخی «واقعی» — روی سروری اجرا کنید که به بایننس دسترسی دارد.

    python backtest.py BTCUSDT 90                 # یک نماد، ۹۰ روز
    python backtest.py ALL 60 --min-score 3       # همه‌ی نمادهای پیش‌فرض
    python backtest.py ETHUSDT 90 --fee 0.0005    # کارمزد هر سمت (پیش‌فرض ۰٫۰۴٪)
    python backtest.py BTCUSDT 90 --be            # مقایسه: انتقال SL به ورود بعد از TP1
    EXCHANGE=bybit python backtest.py BTCUSDT 90  # منبع داده‌ی Bybit

همین منطق از دستور ادمین /backtest هم استفاده می‌کند.
هشدار: نتیجه‌ی بک‌تست تضمین آینده نیست؛ پارامترها را روی همین داده «بهینه» نکنید (overfitting).
"""
from __future__ import annotations
import argparse
import asyncio
import math
import os
import time

import httpx

import exchange as ex
import smc

DEFAULT_SYMBOLS = "BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT,XRPUSDT,DOGEUSDT,ADAUSDT,AVAXUSDT,LINKUSDT,SUIUSDT"


async def _sleep(s: float):
    await asyncio.sleep(s)


async def fetch_history(client, symbol: str, timeframe: str, days: float, base_url: str | None = None,
                        timeout: float = 10.0, retries: int = 3, now_ms: int | None = None, exchange: str = "binance"):
    """تاریخچه‌ی کندل را پنجره‌به‌پنجره می‌گیرد (Binance/Bybit). در شکست دائمی ValueError می‌دهد."""
    base_url = base_url or ex.DEFAULT_URLS[exchange]
    tf, page = smc.TF_MS[timeframe], ex.PAGE[exchange]
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    start, rows, seen = now_ms - int(days * 86_400_000), [], set()
    for _ in range(120):
        if start >= now_ms:
            break
        end = start + page * tf - 1
        url, params = ex.build_request(exchange, base_url, symbol, timeframe, start, end, page)
        got = None
        for attempt in range(retries):
            try:
                r = await client.get(url, params=params, timeout=timeout)
                r.raise_for_status()
                got = ex.normalize(exchange, r.json(), timeframe)
                break
            except (httpx.HTTPError, ValueError):
                if attempt < retries - 1:
                    await _sleep(2 ** attempt)
        if got is None:
            raise ValueError(f"دریافت تاریخچه‌ی {symbol} ناموفق بود")
        for row in got:
            if row[0] not in seen:
                seen.add(row[0])
                rows.append(row)
        start = end + 1
    rows.sort(key=lambda r: int(r[0]))
    return smc.klines_to_df(rows, now_ms=now_ms)


def run_backtest(df, min_score: int = 2, fee: float = 0.0004, max_hold: int = 150, be: bool = False) -> list[dict]:
    """
    همان قواعد زنده: فقط سیگنال با امتیاز >= min_score، و تا بسته‌نشدن سیگنال باز هم‌جهت، سیگنال جدید نمی‌آید.
    کارمزد: رفت‌وبرگشت ۲×fee روی کل حجم، تبدیل‌شده به R.
    """
    busy = {"LONG": -1, "SHORT": -1}
    trades = []
    for s in smc.analyze(df):
        if s["score"] < min_score or s["idx"] <= busy[s["side"]]:
            continue
        res = smc.simulate(s, df, max_hold, be_after_tp1=be)
        if res["outcome"] == "open":
            continue
        risk = abs(s["entry"] - s["sl"])
        cost = 2 * fee * s["entry"] / risk
        busy[s["side"]] = res["closed_idx"]
        trades.append(dict(t=s["candle"], idx=s["idx"], closed=res["closed_idx"], side=s["side"], score=s["score"],
                           hits=res["hits"], outcome=res["outcome"], gross=res["r"], net=res["r"] - cost, cost=cost))
    return trades


def summarize(trades: list[dict]) -> dict:
    n = len(trades)
    if n == 0:
        return {"n": 0}
    trades = sorted(trades, key=lambda x: x["t"])
    net = [x["net"] for x in trades]
    avg = sum(net) / n
    sd = math.sqrt(sum((v - avg) ** 2 for v in net) / (n - 1)) if n > 1 else 0.0
    se = sd / math.sqrt(n) if n > 1 else float("inf")
    gains, losses = sum(v for v in net if v > 0), -sum(v for v in net if v < 0)
    cum = peak = dd = 0.0
    for v in net:
        cum += v
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
    grp = lambda key: {k: (len(g), sum(x["net"] for x in g) / len(g)) for k in sorted({x[key] for x in trades})
                       for g in [[x for x in trades if x[key] == k]]}
    if n < 30:
        verdict = "نمونه کم است (<۳۰)؛ نتیجه قابل اتکا نیست."
    elif avg - 2 * se > 0:
        verdict = "میانگین مثبت است و از صفر قابل تشخیص (تقریبی، ~۹۵٪). این یعنی فقط روی گذشته؛ تضمین آینده نیست."
    elif avg + 2 * se < 0:
        verdict = "میانگین منفی است. این استراتژی در این بازه ضررده بوده؛ برای فروش مناسب نیست."
    else:
        verdict = "میانگین از صفر قابل تشخیص نیست؛ شواهد کافی برای سودآوری وجود ندارد."
    return dict(n=n, avg_net=avg, avg_gross=sum(x["gross"] for x in trades) / n, se=se,
                win=sum(v > 0 for v in net) / n, tp1=sum(x["hits"] >= 1 for x in trades) / n,
                tp3=sum(x["hits"] >= 3 for x in trades) / n, tp5=sum(x["hits"] >= 5 for x in trades) / n,
                sl=sum(x["outcome"] == "sl" for x in trades) / n,
                pf=(gains / losses) if losses > 0 else float("inf"), max_dd=dd, total=cum,
                by_side=grp("side"), by_score=grp("score"), verdict=verdict)


def format_report(title: str, sm: dict, fee: float) -> str:
    if sm["n"] == 0:
        return f"📉 {title}\nسیگنالی در این بازه تولید نشد."
    pf = "∞" if sm["pf"] == float("inf") else f"{sm['pf']:.2f}"
    sides = " | ".join(f"{k}: {n} معامله، {a:+.2f}R" for k, (n, a) in sm["by_side"].items())
    scores = " | ".join(f"امتیاز {k}: {n}، {a:+.2f}R" for k, (n, a) in sm["by_score"].items())
    return (f"📈 بک‌تست {title}\nتعداد معامله: {sm['n']}\n"
            f"میانگین هر معامله: {sm['avg_net']:+.3f}R خالص ({sm['avg_gross']:+.3f}R قبل از کارمزد) ± {sm['se']:.3f}\n"
            f"جمع کل: {sm['total']:+.1f}R | بیشترین افت: {sm['max_dd']:.1f}R | ضریب سود (PF): {pf}\n"
            f"سیگنال مثبت: {sm['win']:.0%} | TP1+: {sm['tp1']:.0%} | TP3+: {sm['tp3']:.0%} | TP5: {sm['tp5']:.0%} | SL: {sm['sl']:.0%}\n"
            f"{sides}\n{scores}\n(کارمزد رفت‌وبرگشت {2 * fee:.2%} لحاظ شده؛ لغزش قیمت نه)\n\n"
            f"🔎 {sm['verdict']}\n⚠️ پارامترها را روی همین داده تنظیم نکنید (overfitting).")


async def run_symbols(client, symbols, timeframe, days, min_score, fee, base_url, max_hold=150, timeout=10.0, retries=3,
                      exchange="binance", be=False):
    """-> (همه‌ی معامله‌ها، فهرست نمادهای ناموفق)"""
    all_trades, failed = [], []
    for sym in symbols:
        try:
            df = await fetch_history(client, sym, timeframe, days, base_url, timeout, retries, exchange=exchange)
            all_trades += await asyncio.to_thread(run_backtest, df, min_score, fee, max_hold, be)
        except ValueError:
            failed.append(sym)
    return all_trades, failed


async def _main(args):
    syms = DEFAULT_SYMBOLS.split(",") if args.symbol.upper() == "ALL" else [args.symbol.upper()]
    tf, exch = os.getenv("TIMEFRAME", "15m"), os.getenv("EXCHANGE", "binance").lower()
    url = (os.getenv("EXCHANGE_URL") or ex.DEFAULT_URLS[exch]).rstrip("/")
    async with httpx.AsyncClient() as client:
        trades, failed = await run_symbols(client, syms, tf, args.days, args.min_score, args.fee, url,
                                           exchange=exch, be=args.be)
    tag = f"{args.symbol.upper()} | {exch} | {tf} | {args.days} روز | MIN_SCORE={args.min_score}" + (" | BE بعد از TP1" if args.be else "")
    print(format_report(tag, summarize(trades), args.fee))
    if failed:
        print("ناموفق:", ", ".join(failed))


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="بک‌تست SMC روی داده‌ی واقعی بایننس")
    ap.add_argument("symbol", help="مثلاً BTCUSDT یا ALL")
    ap.add_argument("days", type=int, nargs="?", default=60)
    ap.add_argument("--min-score", type=int, default=2)
    ap.add_argument("--fee", type=float, default=0.0004)
    ap.add_argument("--be", action="store_true", help="بعد از TP1 حد ضرر به ورود منتقل شود")
    asyncio.run(_main(ap.parse_args()))
