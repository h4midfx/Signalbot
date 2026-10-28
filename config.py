"""تنظیمات از متغیرهای محیطی (هیچ مقدار محرمانه‌ای در کد نیست)."""
from __future__ import annotations
import os
import re
from dataclasses import dataclass

import exchange as ex
import smc


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Plan:
    id: str
    days: int
    stars: int
    label: str


@dataclass(frozen=True)
class Config:
    token: str
    admin_ids: frozenset
    plans: tuple
    trial_days: int
    ref_bonus_days: int
    symbols: tuple
    timeframe: str
    min_score: int
    scan_every: int
    db_path: str
    channel_id: str
    support_username: str
    exchange: str
    exchange_url: str
    be_after_tp1: bool
    http_timeout: float
    http_retries: int
    concurrency: int
    max_hold: int

    def plan(self, plan_id: str):
        return next((p for p in self.plans if p.id == plan_id), None)


_LABELS = {7: "هفتگی", 30: "ماهانه", 90: "۳ ماهه", 180: "۶ ماهه", 365: "سالانه"}
DEFAULT_PLANS = "week:7:150,month:30:500,quarter:90:1200"
DEFAULT_SYMBOLS = "BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT,XRPUSDT,DOGEUSDT,ADAUSDT,AVAXUSDT,LINKUSDT,SUIUSDT"


def _int(env, key, default, lo, hi):
    raw = env.get(key, str(default)).strip()
    try:
        v = int(raw)
    except ValueError:
        raise ConfigError(f"{key} باید عدد صحیح باشد") from None
    if not lo <= v <= hi:
        raise ConfigError(f"{key} باید بین {lo} و {hi} باشد")
    return v


def parse_plans(spec: str) -> tuple:
    plans = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        bits = part.split(":")
        if len(bits) != 3 or not re.fullmatch(r"[a-z0-9]{1,12}", bits[0]):
            raise ConfigError(f"فرمت پلن نامعتبر: «{part}» (مثال: month:30:500)")
        try:
            days, stars = int(bits[1]), int(bits[2])
        except ValueError:
            raise ConfigError(f"روز/قیمت پلن «{part}» باید عدد باشد") from None
        if not 1 <= days <= 3650:
            raise ConfigError("مدت پلن باید ۱ تا ۳۶۵۰ روز باشد")
        if not 1 <= stars <= 10000:
            raise ConfigError("قیمت پلن باید ۱ تا ۱۰۰۰۰ Star باشد")
        plans.append(Plan(bits[0], days, stars, _LABELS.get(days, f"{days} روزه")))
    if not plans:
        raise ConfigError("حداقل یک پلن لازم است")
    if len({p.id for p in plans}) != len(plans):
        raise ConfigError("شناسه‌ی پلن‌ها باید یکتا باشد")
    return tuple(plans)


def load_config(env=None) -> Config:
    env = os.environ if env is None else env
    token = env.get("BOT_TOKEN", "").strip()
    if not token:
        raise ConfigError("BOT_TOKEN تنظیم نشده است")

    ids = env.get("ADMIN_IDS", env.get("ADMIN_ID", ""))
    try:
        admin_ids = frozenset(int(x) for x in ids.replace(" ", "").split(",") if x)
    except ValueError:
        raise ConfigError("ADMIN_IDS باید لیست آیدی عددی باشد") from None
    if not admin_ids:
        raise ConfigError("ADMIN_IDS تنظیم نشده است")

    if "PLANS" in env:
        plans = parse_plans(env["PLANS"])
    elif "PRICE_STARS" in env:  # سازگاری با نسخه‌ی قبلی
        d, s = _int(env, "SUB_DAYS", 30, 1, 3650), _int(env, "PRICE_STARS", 500, 1, 10000)
        plans = (Plan("sub", d, s, _LABELS.get(d, f"{d} روزه")),)
    else:
        plans = parse_plans(DEFAULT_PLANS)

    symbols = tuple(s.strip().upper() for s in env.get("SYMBOLS", DEFAULT_SYMBOLS).split(",") if s.strip())
    if not symbols or any(not re.fullmatch(r"[A-Z0-9]{3,20}", s) for s in symbols):
        raise ConfigError("SYMBOLS نامعتبر است")

    tf = env.get("TIMEFRAME", "15m")
    if tf not in smc.TF_MS:
        raise ConfigError(f"TIMEFRAME باید یکی از {', '.join(smc.TF_MS)} باشد")

    exchange = env.get("EXCHANGE", "binance").strip().lower()
    if exchange not in ex.EXCHANGES:
        raise ConfigError(f"EXCHANGE باید یکی از {', '.join(ex.EXCHANGES)} باشد")
    # روی Railway اگر Volume وصل باشد، دیتابیس به‌صورت خودکار روی آن ساخته می‌شود
    vol = env.get("RAILWAY_VOLUME_MOUNT_PATH", "").rstrip("/")
    db_path = env.get("DB_PATH") or (f"{vol}/bot.db" if vol else "bot.db")

    return Config(
        token=token, admin_ids=admin_ids, plans=plans,
        trial_days=_int(env, "TRIAL_DAYS", 1, 0, 30),
        ref_bonus_days=_int(env, "REF_BONUS_DAYS", 3, 0, 365),
        symbols=symbols, timeframe=tf,
        min_score=_int(env, "MIN_SCORE", 2, 1, 4),
        scan_every=_int(env, "SCAN_EVERY", 300, 30, 3600),
        db_path=db_path,
        channel_id=env.get("PUBLIC_CHANNEL_ID", "").strip(),
        support_username=env.get("SUPPORT_USERNAME", "").strip().lstrip("@"),
        exchange=exchange,
        exchange_url=(env.get("EXCHANGE_URL") or ex.DEFAULT_URLS[exchange]).rstrip("/"),
        be_after_tp1=env.get("BE_AFTER_TP1", "0").strip().lower() in ("1", "true", "yes", "on"),
        http_timeout=float(_int(env, "HTTP_TIMEOUT", 10, 2, 60)),
        http_retries=_int(env, "HTTP_RETRIES", 3, 1, 6),
        concurrency=_int(env, "CONCURRENCY", 5, 1, 20),
        max_hold=_int(env, "MAX_HOLD_CANDLES", 150, 10, 2000),
    )
