"""
ربات سیگنال SMC با اشتراک Telegram Stars
اجرا:  python signal_bot.py      (تنظیمات از متغیرهای محیطی؛ فایل .env.example را ببینید)
"""
from __future__ import annotations
import asyncio
import json
import logging
import math
import os
import re
import shutil
import tempfile
import time
from datetime import datetime, timezone

import httpx
from telegram import Update, LabeledPrice, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import Forbidden, RetryAfter, TelegramError
from telegram.ext import (Application, CommandHandler, CallbackQueryHandler,
                          PreCheckoutQueryHandler, MessageHandler, filters)

import backtest as bt
import exchange as ex
import smc
from config import Config, load_config
from database import Database

log = logging.getLogger("signalbot")

DISCLAIMER = ("⚠️ توصیه مالی نیست. فیوچرز با اهرم ریسک بالایی دارد و ممکن است کل سرمایه از بین برود. "
              "ریسک هر معامله را حداکثر ۱ تا ۲٪ سرمایه نگه دارید. نتایج گذشته تضمین آینده نیست.")
CURRENCY = "XTR"
RATE_SLEEP = 0.04  # ~25 پیام در ثانیه


async def _sleep(s: float):  # جدا شده تا در تست قابل جایگزینی باشد
    await asyncio.sleep(s)


def _ctx(context):
    return context.bot_data["cfg"], context.bot_data["db"]


def _mask(s: str) -> str:
    return "…" + s[-4:] if s and len(s) > 4 else "…"


def fmt(p: float) -> str:
    return f"{p:.2f}" if p >= 100 else f"{p:.4f}" if p >= 1 else f"{p:.6f}"


def fmt_date(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


# ============================================================ ارسال پیام امن
async def safe_send(bot, uid: int, text: str, db: Database | None = None) -> str:
    """'ok' | 'blocked' | 'failed'  — هیچ‌وقت استثنا بالا نمی‌دهد."""
    for attempt in range(2):
        try:
            await bot.send_message(uid, text)
            return "ok"
        except Forbidden:
            if db:
                db.set_blocked(uid, True)
            return "blocked"
        except RetryAfter as e:
            wait = getattr(e.retry_after, "total_seconds", lambda: e.retry_after)()
            if attempt == 0:
                await _sleep(float(wait) + 1)
                continue
            return "failed"
        except (TelegramError, asyncio.TimeoutError) as e:
            log.warning("send failed uid=%s: %s", uid, type(e).__name__)
            return "failed"
    return "failed"


async def broadcast(bot, db: Database, user_ids, text: str) -> dict:
    res = {"ok": 0, "blocked": 0, "failed": 0}
    for uid in user_ids:
        res[await safe_send(bot, uid, text, db)] += 1
        await _sleep(RATE_SLEEP)
    return res


async def notify_admins(context, text: str):
    cfg, _ = _ctx(context)
    for aid in cfg.admin_ids:
        await safe_send(context.bot, aid, text)


async def post_channel(context, text: str):
    cfg, _ = _ctx(context)
    if cfg.channel_id:
        await safe_send(context.bot, cfg.channel_id, text)


# ============================================================ اعتبارسنجی فاکتور
def validate_invoice(cfg: Config, payload: str, currency: str, amount: int, uid: int):
    """(ok, پیام خطا، پلن). payload باید 'sub:<plan>:<uid>' و متعلق به همین کاربر باشد."""
    parts = (payload or "").split(":")
    if len(parts) != 3 or parts[0] != "sub":
        return False, "سفارش نامعتبر است", None
    plan = cfg.plan(parts[1])
    if plan is None:
        return False, "پلن نامعتبر است", None
    if not parts[2].isdigit() or int(parts[2]) != uid:
        return False, "این سفارش متعلق به شما نیست", None
    if currency != CURRENCY:
        return False, "ارز نامعتبر است", None
    if amount != plan.stars:
        return False, "مبلغ نامعتبر است", None
    return True, None, plan


# ============================================================ متن‌ها
def signal_text(sym: str, s: dict, tf: str, sid: int | None = None, test: bool = False, be: bool = False) -> str:
    emoji = "🟢" if s["side"] == "LONG" else "🔴"
    tags = [s["kind"]] + (["FVG"] if s["fvg"] else []) + (["Liquidity Sweep"] if s["swept"] else [])
    tps = "\n".join(f"🎯 TP{i + 1}: {fmt(x)}" for i, x in enumerate(s["tps"]))
    head = ("🧪 [تست] " if test else "") + f"{emoji} {s['side']} | {sym} | {tf}" + (f" | #{sid}" if sid else "")
    return (f"{head}\n🧠 SMC: {' + '.join(tags)} + Order Block\n⭐ قدرت سیگنال: {s['score']}/4\n\n"
            f"📍 ورود: {fmt(s['entry'])}\n🛑 حد ضرر: {fmt(s['sl'])}\n{tps}\n\n"
            f"💡 در هر TP حدود ۲۰٪ حجم ببندید. محاسبه حجم: /risk\n"
            + ("🔒 بعد از TP1 حد ضرر را به نقطه‌ی ورود ببرید.\n" if be else "") + DISCLAIMER)


def perf_text(p: dict, title: str = "عملکرد ثبت‌شده‌ی ربات") -> str:
    if not p["n"]:
        return f"هنوز سیگنال بسته‌شده‌ای ثبت نشده است. (سیگنال باز: {p['open']})"
    lines = [f"📊 {title} ({p['n']} سیگنال بسته‌شده، {p['open']} باز)",
             f"• رسیدن به حداقل TP1: {p['tp1']:.0%}", f"• رسیدن به حداقل TP3: {p['tp3']:.0%}",
             f"• رسیدن به TP5: {p['tp5']:.0%}", f"• برخورد به حد ضرر: {p['sl']:.0%}",
             f"• میانگین نتیجه: {p['avg_r']:+.2f}R | سیگنال‌های مثبت: {p['win']:.0%}", "", "آخرین نتایج:"]
    for r in p["recent"]:
        mark = "✅" if r["r"] > 0 else "❌" if r["r"] < 0 else "➖"
        lines.append(f"{mark} {r['symbol']} {r['side']} | TP{r['hits']} | {r['r']:+.2f}R")
    if p["n"] < 30:
        lines.append("\n⚠️ تعداد نمونه کم است (<۳۰)؛ این آمار هنوز معنی‌دار نیست.")
    lines.append("نتایج گذشته تضمین آینده نیست.")
    return "\n".join(lines)


# ============================================================ دستورهای کاربر
async def start(update, context):
    cfg, db = _ctx(context)
    u = update.effective_user
    is_new = db.upsert_user(u.id, getattr(u, "username", None))
    if is_new and context.args and context.args[0].startswith("ref_"):
        try:
            db.set_referrer(u.id, int(context.args[0][4:]))
        except ValueError:
            pass
    lines = ["سلام 👋", "سیگنال‌های فیوچرز بر پایه‌ی Smart Money Concepts (BOS/CHoCH، Order Block، FVG، Liquidity Sweep) "
             "با ۵ هدف سود (TP1–TP5) و اطلاع‌رسانی لحظه‌ای هر هدف/حد ضرر.", ""]
    lines += [f"💳 {p.label}: {p.stars} ⭐" for p in cfg.plans]
    lines += ["", "/buy خرید اشتراک", "/status وضعیت اشتراک"]
    if cfg.trial_days > 0:
        lines.append(f"/trial تست رایگان {cfg.trial_days} روزه")
    lines += ["/settings تنظیمات شخصی (حداقل امتیاز، جهت، اعلان TP/SL)", "/promo کد هدیه", "/open سیگنال‌های باز (مشترکان)", "/performance عملکرد ثبت‌شده", "/risk محاسبه حجم معامله", "/ref دعوت دوستان و دریافت روز رایگان",
              "/terms قوانین | /paysupport پشتیبانی پرداخت", "", DISCLAIMER]
    await update.message.reply_text("\n".join(lines))


async def buy(update, context):
    cfg, _ = _ctx(context)
    kb = [[InlineKeyboardButton(f"{p.label} ({p.days} روز) — {p.stars} ⭐", callback_data=f"buy:{p.id}")]
          for p in cfg.plans]
    await update.message.reply_text("پلن مورد نظر را انتخاب کنید:", reply_markup=InlineKeyboardMarkup(kb))


async def buy_button(update, context):
    cfg, _ = _ctx(context)
    q = update.callback_query
    await q.answer()
    plan = cfg.plan((q.data or "").split(":", 1)[-1]) if (q.data or "").startswith("buy:") else None
    if plan is None:
        return
    uid = update.effective_user.id
    await context.bot.send_invoice(
        chat_id=uid, title=f"اشتراک سیگنال {plan.label}", description=f"{plan.days} روز دریافت سیگنال‌های SMC (TP1–TP5)",
        payload=f"sub:{plan.id}:{uid}", currency=CURRENCY, prices=[LabeledPrice(plan.label, plan.stars)],
        provider_token="")


async def precheckout(update, context):
    cfg, _ = _ctx(context)
    q = update.pre_checkout_query
    ok, err, _plan = validate_invoice(cfg, q.invoice_payload, q.currency, q.total_amount, q.from_user.id)
    if not ok:
        log.warning("pre_checkout rejected: %s", err)
    await q.answer(ok=ok, error_message=None if ok else err)


async def successful_payment(update, context):
    cfg, db = _ctx(context)
    sp, uid = update.message.successful_payment, update.effective_user.id
    ok, err, plan = validate_invoice(cfg, sp.invoice_payload, sp.currency, sp.total_amount, uid)
    if not ok:  # دفاع در عمق: هیچ اشتراکی فعال نمی‌شود
        log.error("invalid successful_payment charge=%s reason=%s", _mask(sp.telegram_payment_charge_id), err)
        await notify_admins(context, f"⚠️ پرداخت نامعتبر از کاربر {uid} ({err}). نیاز به بررسی دستی.\n"
                                     f"charge: {sp.telegram_payment_charge_id}")
        await safe_send(context.bot, uid, "پرداخت دریافت شد اما به‌صورت خودکار تایید نشد. لطفاً /paysupport را بزنید.")
        return
    res = db.record_payment(sp.telegram_payment_charge_id, uid, sp.total_amount, plan.id, plan.days, cfg.ref_bonus_days)
    if res["status"] == "duplicate":
        log.info("duplicate payment ignored charge=%s", _mask(sp.telegram_payment_charge_id))
        await safe_send(context.bot, uid, "این پرداخت قبلاً ثبت شده است ✅")
        return
    await safe_send(context.bot, uid, f"✅ پرداخت موفق. اشتراک {plan.label} تا {fmt_date(res['expires'])} (UTC) فعال است.")
    await notify_admins(context, f"💰 {sp.total_amount}⭐ ({plan.id}) از کاربر {uid}\ncharge: {sp.telegram_payment_charge_id}")
    if res["referrer"]:
        await safe_send(context.bot, res["referrer"], f"🎁 دوست دعوت‌شده‌ی شما خرید کرد؛ {cfg.ref_bonus_days} روز اشتراک هدیه گرفتید!")


async def status(update, context):
    cfg, db = _ctx(context)
    exp, now = db.expires_at(update.effective_user.id), time.time()
    if exp > now:
        await update.message.reply_text(f"✅ اشتراک فعال — {(exp - now) / 86400:.1f} روز باقی مانده (تا {fmt_date(exp)} UTC)")
    else:
        extra = " یا /trial" if cfg.trial_days > 0 else ""
        await update.message.reply_text(f"❌ اشتراک فعال ندارید. /buy{extra}")


async def trial(update, context):
    cfg, db = _ctx(context)
    if cfg.trial_days <= 0:
        return await update.message.reply_text("تست رایگان فعال نیست. /buy")
    u = update.effective_user
    db.upsert_user(u.id, getattr(u, "username", None))
    if db.start_trial(u.id, cfg.trial_days):
        await update.message.reply_text(f"🎁 تست رایگان {cfg.trial_days} روزه فعال شد. سیگنال‌ها را همین‌جا دریافت می‌کنید.")
    else:
        await update.message.reply_text("تست رایگان قبلاً استفاده شده یا اشتراک فعال دارید. /buy")


async def ref(update, context):
    cfg, db = _ctx(context)
    me = context.bot_data.get("bot_username")
    if not me:
        me = context.bot_data["bot_username"] = (await context.bot.get_me()).username
    uid = update.effective_user.id
    await update.message.reply_text(
        f"لینک دعوت شما:\nhttps://t.me/{me}?start=ref_{uid}\n\n"
        f"هر دوستی که با این لینک وارد شود و اولین خریدش را انجام دهد، {cfg.ref_bonus_days} روز اشتراک رایگان می‌گیرید.")


async def performance(update, context):
    _, db = _ctx(context)
    await update.message.reply_text(perf_text(db.performance()))


async def risk(update, context):
    """/risk موجودی درصد_ریسک ورود حد_ضرر"""
    try:
        bal, pct, entry, sl = (float(x) for x in context.args[:4])
        if len(context.args) < 4 or not all(map(math.isfinite, (bal, pct, entry, sl))):
            raise ValueError
        if bal <= 0 or not 0 < pct <= 100 or entry <= 0 or sl <= 0 or entry == sl:
            raise ValueError
    except (ValueError, TypeError):
        return await update.message.reply_text("استفاده: /risk موجودی درصد_ریسک قیمت_ورود حد_ضرر\nمثال: /risk 1000 1 144.41 142.57")
    risk_amt = bal * pct / 100
    qty = risk_amt / abs(entry - sl)
    notional = qty * entry
    await update.message.reply_text(
        f"💰 ریسک: {risk_amt:.2f}\n📦 حجم: {qty:.6g} واحد\n💵 ارزش موقعیت: {notional:.2f}\n"
        f"⚙️ اهرم لازم (تقریبی): {notional / bal:.1f}x\n(کارمزد و لغزش در نظر گرفته نشده است)")


async def paysupport(update, context):
    cfg, _ = _ctx(context)
    who = f"@{cfg.support_username}" if cfg.support_username else "ادمین ربات"
    await update.message.reply_text(f"مشکل پرداخت یا درخواست بازگشت وجه: پیام به {who}\n(شناسه‌ی کاربری خود را بفرستید: {update.effective_user.id})")


async def terms(update, context):
    await update.message.reply_text(
        "قوانین:\n• سیگنال‌ها آموزشی/اطلاعاتی‌اند و توصیه مالی نیستند.\n• هیچ سودی تضمین نمی‌شود و ضرر ممکن است.\n"
        "• اشتراک خدمت دیجیتال است؛ بازگشت وجه فقط در صورت مشکل فنی پرداخت و با بررسی پشتیبانی (/paysupport).\n"
        "• مسئولیت معاملات با خود شماست.")


def settings_view(cfg: Config, st: dict):
    ms = st["min_score"]
    eff = max(ms or 0, cfg.min_score)
    text = ("⚙️ تنظیمات شما\n"
            f"• حداقل امتیاز سیگنال: {ms if ms else 'پیش‌فرض'} (اثر واقعی: {eff}؛ حداقل سراسری ربات {cfg.min_score} است)\n"
            f"• جهت: {({'ALL': 'همه', 'LONG': 'فقط خرید (LONG)', 'SHORT': 'فقط فروش (SHORT)'})[st['side']]}\n"
            f"• اعلان TP/SL: {'روشن' if st['progress'] else 'خاموش'}\n\n"
            "امتیاز بالاتر = سیگنال کمتر و گزینش‌شده‌تر.")
    mark = lambda cond, label: ("✅ " if cond else "") + label
    kb = [[InlineKeyboardButton(mark((ms or 0) == n, f"امتیاز {n}+"), callback_data=f"set:ms:{n}") for n in (1, 2, 3, 4)],
          [InlineKeyboardButton(mark(st["side"] == v, label), callback_data=f"set:side:{v}")
           for v, label in (("ALL", "همه"), ("LONG", "خرید"), ("SHORT", "فروش"))],
          [InlineKeyboardButton(mark(st["progress"], "اعلان TP/SL روشن"), callback_data="set:prog:1"),
           InlineKeyboardButton(mark(not st["progress"], "خاموش"), callback_data="set:prog:0")]]
    return text, InlineKeyboardMarkup(kb)


async def settings_cmd(update, context):
    cfg, db = _ctx(context)
    u = update.effective_user
    db.upsert_user(u.id, getattr(u, "username", None))
    text, kb = settings_view(cfg, db.get_settings(u.id))
    await update.message.reply_text(text, reply_markup=kb)


async def settings_button(update, context):
    cfg, db = _ctx(context)
    q, uid = update.callback_query, update.effective_user.id
    await q.answer()
    parts = (q.data or "").split(":")
    if len(parts) != 3 or parts[0] != "set":
        return
    _, key, val = parts
    ok = False
    if key == "ms" and val.isdigit():
        ok = db.set_setting(uid, "min_score", int(val))
    elif key == "side":
        ok = db.set_setting(uid, "side", val)
    elif key == "prog" and val in ("0", "1"):
        ok = db.set_setting(uid, "progress", val == "1")
    if ok:
        text, kb = settings_view(cfg, db.get_settings(uid))
        await q.edit_message_text(text, reply_markup=kb)


PROMO_MSG = {"not_found": "کد نامعتبر است.", "expired": "این کد منقضی شده است.",
             "exhausted": "ظرفیت این کد تمام شده است.", "already": "شما قبلاً از این کد استفاده کرده‌اید."}


async def promo(update, context):
    _, db = _ctx(context)
    u = update.effective_user
    code = (context.args[0] if context.args else "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{3,20}", code):
        return await update.message.reply_text("استفاده: /promo کد")
    db.upsert_user(u.id, getattr(u, "username", None))
    r = db.redeem_promo(code, u.id)
    if r["status"] == "ok":
        await update.message.reply_text(f"🎁 {r['days']} روز اشتراک هدیه فعال شد (تا {fmt_date(r['expires'])} UTC).")
    else:
        await update.message.reply_text(PROMO_MSG[r["status"]])


async def open_cmd(update, context):
    cfg, db = _ctx(context)
    uid = update.effective_user.id
    if uid not in cfg.admin_ids and not db.is_active(uid):
        return await update.message.reply_text("مشاهده‌ی سیگنال‌های باز فقط برای مشترکان است. /buy")
    rows = db.open_signals()
    if not rows:
        return await update.message.reply_text("فعلاً سیگنال بازی وجود ندارد.")
    lines = ["📂 سیگنال‌های باز:"]
    for r in rows:
        tps = json.loads(r["tps"])
        nxt = f"هدف بعدی TP{r['hits'] + 1}: {fmt(tps[r['hits']])}" if r["hits"] < 5 else "همه‌ی اهداف"
        lines.append(f"\n#{r['id']} {r['symbol']} {r['side']} | ورود {fmt(r['entry'])} | SL {fmt(r['sl'])}\n"
                     f"   تا اکنون: TP{r['hits']} | {nxt}")
    lines.append("\n⚠️ اگر قیمت از نقطه‌ی ورود دور شده، دنبال آن وارد نشوید؛ منتظر ستاپ بعدی بمانید.")
    await update.message.reply_text("\n".join(lines))


# ============================================================ دستورهای ادمین
def admin_only(fn):
    async def wrapper(update, context):
        cfg, _ = _ctx(context)
        if update.effective_user.id in cfg.admin_ids:
            return await fn(update, context)
    wrapper.__name__ = fn.__name__
    return wrapper


@admin_only
async def stats(update, context):
    _, db = _ctx(context)
    s, p = db.stats(), db.performance(0)
    perf = f"{p['n']} بسته‌شده | {p['open']} باز" + (f" | میانگین {p['avg_r']:+.2f}R" if p["n"] else "")
    await update.message.reply_text(
        f"👥 کاربران: {s['users']} (بلاک‌کرده: {s['blocked']})\n✅ مشترک فعال: {s['active']} | تریال: {s['trials']}\n"
        f"💳 پرداخت‌ها: {s['payments']} از {s['payers']} نفر | بازگشت: {s['refunds']}\n"
        f"💰 درآمد: {s['revenue']}⭐ (۷ روز: {s['revenue_7d']} | ۳۰ روز: {s['revenue_30d']})\n📈 سیگنال‌ها: {perf}")


@admin_only
async def grant(update, context):
    cfg, db = _ctx(context)
    try:
        uid, days = int(context.args[0]), int(context.args[1])
        if uid <= 0 or not 1 <= days <= 3650:
            raise ValueError
    except (ValueError, IndexError):
        return await update.message.reply_text("استفاده: /grant USER_ID DAYS  (۱ تا ۳۶۵۰ روز)")
    exp = db.grant(uid, days)
    await safe_send(context.bot, uid, f"🎁 {days} روز اشتراک هدیه گرفتید (تا {fmt_date(exp)} UTC).")
    await update.message.reply_text(f"انجام شد؛ اشتراک {uid} تا {fmt_date(exp)}")


async def _send_text_cmd(update, context, everyone: bool):
    _, db = _ctx(context)
    text = " ".join(context.args).strip()
    if not text or len(text) > 3500:
        return await update.message.reply_text("متن لازم است (حداکثر ۳۵۰۰ کاراکتر).")
    ids = db.all_user_ids() if everyone else db.active_user_ids()
    r = await broadcast(context.bot, db, ids, text if everyone else f"{text}\n\n{DISCLAIMER}")
    await update.message.reply_text(f"ارسال شد — موفق: {r['ok']} | بلاک: {r['blocked']} | ناموفق: {r['failed']}")


@admin_only
async def signal_cmd(update, context):
    await _send_text_cmd(update, context, everyone=False)


@admin_only
async def announce(update, context):
    await _send_text_cmd(update, context, everyone=True)


@admin_only
async def newpromo(update, context):
    """/newpromo CODE DAYS MAXUSES [EXPIRES_IN_DAYS]"""
    _, db = _ctx(context)
    try:
        code, days, maxu = context.args[0], int(context.args[1]), int(context.args[2])
        exp_days = int(context.args[3]) if len(context.args) > 3 else None
        if not re.fullmatch(r"[A-Za-z0-9_-]{3,20}", code) or not 1 <= days <= 3650 or not 1 <= maxu <= 100000 \
                or (exp_days is not None and not 1 <= exp_days <= 3650):
            raise ValueError
    except (ValueError, IndexError):
        return await update.message.reply_text("استفاده: /newpromo CODE DAYS MAXUSES [EXPIRES_IN_DAYS]\nمثال: /newpromo WELCOME7 7 100 30")
    ok = db.create_promo(code, days, maxu, int(time.time()) + exp_days * 86400 if exp_days else None)
    await update.message.reply_text(f"✅ کد {code.upper()} ساخته شد ({days} روز، {maxu} بار)" if ok else "این کد قبلاً وجود دارد.")


@admin_only
async def promos(update, context):
    _, db = _ctx(context)
    rows = db.list_promos()
    if not rows:
        return await update.message.reply_text("کدی ثبت نشده است.")
    await update.message.reply_text("\n".join(f"{r['code']}: {r['days']} روز | {r['used']}/{r['max_uses']}"
                                              + (f" | تا {fmt_date(r['expires_ts'])}" if r["expires_ts"] else "") for r in rows))


@admin_only
async def backtest_cmd(update, context):
    """/backtest [نماد|ALL] [روز]  — بک‌تست روی تاریخچه‌ی واقعی بایننس (۵ تا ۱۸۰ روز)"""
    cfg, _ = _ctx(context)
    target = context.args[0].upper() if context.args else cfg.symbols[0]
    days = int(context.args[1]) if len(context.args) > 1 and context.args[1].isdigit() else 30
    if not (target == "ALL" or target in cfg.symbols) or not 5 <= days <= 180:
        return await update.message.reply_text(
            f"استفاده: /backtest نماد|ALL روز\nنمادها: {', '.join(cfg.symbols)}\nروز: ۵ تا ۱۸۰")
    await update.message.reply_text("⏳ در حال دریافت تاریخچه و اجرای بک‌تست... (ممکن است چند ده ثانیه طول بکشد)")
    symbols = list(cfg.symbols) if target == "ALL" else [target]
    try:
        async with httpx.AsyncClient() as client:
            trades, failed = await bt.run_symbols(client, symbols, cfg.timeframe, days, cfg.min_score, 0.0004,
                                                  cfg.exchange_url, cfg.max_hold, cfg.http_timeout, cfg.http_retries,
                                                  exchange=cfg.exchange, be=cfg.be_after_tp1)
    except Exception as e:  # noqa: BLE001
        log.warning("backtest failed: %s", type(e).__name__)
        return await update.message.reply_text("بک‌تست ناموفق بود (مشکل شبکه/داده).")
    title = (f"{target} | {cfg.exchange} | {cfg.timeframe} | {days} روز | MIN_SCORE={cfg.min_score}"
             + (" | BE بعد از TP1" if cfg.be_after_tp1 else ""))
    text = bt.format_report(title, bt.summarize(trades), 0.0004)
    if failed:
        text += f"\n\n⚠️ دریافت داده ناموفق: {', '.join(failed)}"
    await update.message.reply_text(text)


async def send_backup(context, chat_id) -> bool:
    _, db = _ctx(context)
    folder = tempfile.mkdtemp()
    try:
        path = os.path.join(folder, f"backup_{datetime.now(timezone.utc):%Y%m%d_%H%M}.db")
        db.backup_to(path)
        with open(path, "rb") as f:
            await context.bot.send_document(chat_id=chat_id, document=f, filename=os.path.basename(path),
                                            caption="🗄 پشتیبان دیتابیس (محرمانه؛ شامل شناسه‌ی کاربران)")
        return True
    except (TelegramError, OSError) as e:
        log.warning("backup send failed: %s", type(e).__name__)
        return False
    finally:
        shutil.rmtree(folder, ignore_errors=True)


@admin_only
async def backup_cmd(update, context):
    ok = await send_backup(context, update.effective_user.id)
    if not ok:
        await update.message.reply_text("ارسال پشتیبان ناموفق بود.")


@admin_only
async def refund(update, context):
    _, db = _ctx(context)
    try:
        uid, charge = int(context.args[0]), context.args[1]
    except (ValueError, IndexError):
        return await update.message.reply_text("استفاده: /refund USER_ID CHARGE_ID")
    pay = db.get_payment(charge)
    if not pay or pay["user_id"] != uid:
        return await update.message.reply_text("پرداختی با این مشخصات پیدا نشد.")
    if pay["refunded"]:
        return await update.message.reply_text("این پرداخت قبلاً بازگردانده شده است.")
    try:
        await context.bot.refund_star_payment(user_id=uid, telegram_payment_charge_id=charge)
    except TelegramError as e:
        return await update.message.reply_text(f"بازگشت وجه ناموفق بود: {type(e).__name__} — دیتابیس تغییری نکرد.")
    r = db.mark_refunded(charge)
    await update.message.reply_text(f"✅ بازگشت وجه انجام شد ({r['status']}).")
    await safe_send(context.bot, uid, "مبلغ پرداختی شما بازگردانده شد.")


@admin_only
async def testsignals(update, context):
    """بررسی آزمایشی منطق روی داده‌ی مصنوعی. به هیچ کاربری پیام نمی‌دهد، چیزی در دیتابیس نمی‌نویسد."""
    cfg, _ = _ctx(context)
    seed = int(context.args[0]) if context.args and context.args[0].isdigit() else 1
    df = smc.synthetic_candles(3000, seed)
    sigs = smc.analyze(df)
    bad = 0
    for s in sigs:
        t = s["tps"]
        ok = (s["sl"] < s["entry"] < t[0] < t[4]) if s["side"] == "LONG" else (s["sl"] > s["entry"] > t[0] > t[4])
        bad += not ok
    cut = 2000
    prefix = {(s["idx"], s["side"]) for s in smc.analyze(df.iloc[:cut].reset_index(drop=True))}
    full = {(s["idx"], s["side"]) for s in sigs if s["idx"] < cut}
    results = [smc.simulate(s, df) for s in sigs]
    avg = sum(r["r"] for r in results) / len(results) if results else 0.0
    await update.message.reply_text(
        f"🧪 تست روی داده‌ی مصنوعی (seed={seed})\nسیگنال: {len(sigs)} | ترتیب SL/TP نامعتبر: {bad}\n"
        f"عدم نگاه به آینده: {'✅ سالم' if prefix == full else '❌ مشکل'}\nمیانگین R روی داده‌ی مصنوعی: {avg:+.2f}\n"
        "(این عدد هیچ ربطی به سودآوری واقعی ندارد. هیچ پیامی به کاربران ارسال نشد.)")
    if sigs:
        await update.message.reply_text(signal_text("TESTUSDT", sigs[0], cfg.timeframe, test=True, be=cfg.be_after_tp1))


# ============================================================ دریافت داده و اسکن
async def fetch_klines(client, symbol: str, cfg: Config):
    err = None
    for attempt in range(cfg.http_retries):
        try:
            url, params = ex.build_request(cfg.exchange, cfg.exchange_url, symbol, cfg.timeframe, limit=500)
            r = await client.get(url, params=params, timeout=cfg.http_timeout)
            r.raise_for_status()
            rows = ex.normalize(cfg.exchange, r.json(), cfg.timeframe)
            return smc.klines_to_df(rows, now_ms=int(time.time() * 1000))
        except (httpx.HTTPError, ValueError) as e:
            err = e
            if attempt < cfg.http_retries - 1:
                await _sleep(2 ** attempt)
    log.warning("fetch %s failed after retries: %s", symbol, type(err).__name__)
    return None


async def fetch_all(client, cfg: Config) -> dict:
    sem = asyncio.Semaphore(cfg.concurrency)

    async def one(sym):
        async with sem:
            return sym, await fetch_klines(client, sym, cfg)
    return dict(await asyncio.gather(*(one(s) for s in cfg.symbols)))


async def process_new_signals(context, dfs: dict, now_ms: int | None = None) -> int:
    cfg, db = _ctx(context)
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    tf_ms, sent = smc.TF_MS[cfg.timeframe], 0
    for sym, df in dfs.items():
        if df is None or len(df) < smc.MIN_CANDLES:
            continue
        last = len(df) - 1
        if now_ms - (int(df["t"].iloc[last]) + tf_ms) > 3 * tf_ms:
            log.warning("%s: candles are stale, skipped", sym)
            continue
        try:
            sigs = smc.analyze(df)
        except ValueError as e:
            log.warning("%s: invalid data: %s", sym, e)
            continue
        for s in sigs:
            if s["idx"] != last or s["score"] < cfg.min_score or db.has_open(sym, s["side"]):
                continue
            sid = db.insert_signal(sym, s["candle"], s["side"], s["entry"], s["sl"], s["tps"],
                                   s["score"], s["kind"], cfg.timeframe)
            if sid is None:
                continue
            res = await broadcast(context.bot, db, db.recipients_for_signal(s["score"], s["side"]),
                                  signal_text(sym, s, cfg.timeframe, sid, be=cfg.be_after_tp1))
            sent += 1
            await notify_admins(context, f"📤 سیگنال #{sid} {sym} {s['side']} — موفق: {res['ok']} | بلاک: {res['blocked']} | ناموفق: {res['failed']}")
    return sent


async def track_open(context, dfs: dict) -> int:
    """نتیجه را اول در دیتابیس ذخیره می‌کند و بعد اعلام می‌کند؛ پس هر هدف فقط یک‌بار اعلام می‌شود."""
    cfg, db = _ctx(context)
    tf_ms, changed = smc.TF_MS[cfg.timeframe], 0
    for row in db.open_signals():
        df = dfs.get(row["symbol"])
        if df is None or df.empty:
            continue
        new = df[df["t"] > (row["last_t"] or row["candle"])]
        if new.empty:
            continue
        tps = json.loads(row["tps"])
        hits, events, outcome, used = smc.advance(row["side"], row["sl"], tps, row["hits"],
                                                  new["h"].to_numpy(float), new["l"].to_numpy(float),
                                                  entry=row["entry"], be_after_tp1=cfg.be_after_tp1)
        last_t = int(new["t"].iloc[used - 1])
        close = float(new["c"].iloc[used - 1])
        if outcome is None and (last_t - row["candle"]) / tf_ms > cfg.max_hold:
            outcome = "expired"
        if not events and outcome is None:
            db.update_signal(row["id"], hits, last_t)
            continue
        status_ = outcome or "open"
        r = smc.realized_r(hits, outcome, row["entry"], row["sl"], row["side"], close) if outcome else None
        db.update_signal(row["id"], hits, last_t, status_, r)   # اول ذخیره
        changed += 1
        lines = []
        for kind, k in events:
            lines.append(f"✅ TP{k} لمس شد" if kind == "tp" else
                         f"🔒 قیمت به ورود برگشت؛ بسته شد بدون ضرر (بعد از {hits} هدف)" if kind == "be" else
                         f"🛑 حد ضرر فعال شد (بعد از {hits} هدف)")
        if outcome == "done":
            lines.append("🏆 همه‌ی اهداف کامل شد")
        if outcome == "expired":
            lines.append("⏱ سیگنال به‌دلیل گذشت زمان بسته شد")
        if r is not None:
            lines.append(f"نتیجه: {r:+.2f}R")
        text = f"#{row['id']} {row['symbol']} {row['side']}\n" + "\n".join(lines)
        await broadcast(context.bot, db, db.recipients_for_signal(row["score"] or 0, row["side"], progress=True), text)
        await post_channel(context, text)
    return changed


async def scan_job(context):
    cfg, _ = _ctx(context)
    lock = context.bot_data.setdefault("scan_lock", asyncio.Lock())
    if lock.locked():
        return
    async with lock:
        try:
            async with httpx.AsyncClient() as client:
                dfs = await fetch_all(client, cfg)
            await process_new_signals(context, dfs)
            await track_open(context, dfs)
        except Exception:  # نباید زمان‌بندی را بکشد
            log.exception("scan_job failed")


async def reminder_job(context):
    cfg, db = _ctx(context)
    now = int(time.time())
    try:
        for uid, exp in db.expiring_soon(now, 2 * 86400):
            await safe_send(context.bot, uid, "⏳ اشتراک شما کمتر از ۲ روز دیگر تمام می‌شود. برای تمدید: /buy", db)
            db.mark_reminded(uid, exp)
            await _sleep(RATE_SLEEP)
        for uid, exp in db.just_expired(now):
            await safe_send(context.bot, uid, "اشتراک شما تمام شد. برای ادامه‌ی دریافت سیگنال: /buy", db)
            db.mark_expired_notified(uid, exp)
            await _sleep(RATE_SLEEP)
    except Exception:
        log.exception("reminder_job failed")


async def weekly_job(context):
    """هر ۷ روز: گزارش عملکرد برای مشترکان/کانال + بکاپ دیتابیس برای ادمین."""
    cfg, db = _ctx(context)
    now = int(time.time())
    try:
        last = db.get_meta("last_weekly")
        if last is None:                       # اولین اجرا: فقط نقطه‌ی شروع را ثبت کن
            db.set_meta("last_weekly", now)
            return
        if now - int(last) < 7 * 86400:
            return
        p = db.performance(since=int(last))
        if p["n"]:
            text = perf_text(p, "گزارش هفتگی")
            await broadcast(context.bot, db, db.active_user_ids(), text)
            await post_channel(context, text)
        for aid in cfg.admin_ids:
            await send_backup(context, aid)
        db.set_meta("last_weekly", now)
    except Exception:
        log.exception("weekly_job failed")


async def on_error(update, context):
    log.error("handler error: %s", type(context.error).__name__, exc_info=context.error)


# ============================================================ اجرا
class RedactingFormatter(logging.Formatter):
    def __init__(self, secret: str):
        super().__init__("%(asctime)s %(levelname)s %(name)s: %(message)s")
        self.secret = secret

    def format(self, record):
        return super().format(record).replace(self.secret, "<TOKEN>")


def setup_logging(token: str):
    h = logging.StreamHandler()
    h.setFormatter(RedactingFormatter(token))
    logging.basicConfig(level=logging.INFO, handlers=[h], force=True)
    for noisy in ("httpx", "httpcore", "telegram.ext"):   # URL درخواست‌ها شامل توکن است
        logging.getLogger(noisy).setLevel(logging.WARNING)


def build_application(cfg: Config, db: Database):
    app = Application.builder().token(cfg.token).build()
    app.bot_data.update(cfg=cfg, db=db)
    for name, fn in [("start", start), ("buy", buy), ("plans", buy), ("status", status), ("trial", trial),
                     ("ref", ref), ("performance", performance), ("risk", risk), ("paysupport", paysupport),
                     ("terms", terms), ("stats", stats), ("grant", grant), ("signal", signal_cmd),
                     ("announce", announce), ("refund", refund), ("testsignals", testsignals),
                     ("open", open_cmd), ("backtest", backtest_cmd), ("backup", backup_cmd),
                     ("settings", settings_cmd), ("promo", promo), ("newpromo", newpromo), ("promos", promos)]:
        app.add_handler(CommandHandler(name, fn))
    app.add_handler(CallbackQueryHandler(buy_button, pattern=r"^buy:"))
    app.add_handler(CallbackQueryHandler(settings_button, pattern=r"^set:"))
    app.add_handler(PreCheckoutQueryHandler(precheckout))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment))
    app.add_error_handler(on_error)
    app.job_queue.run_repeating(scan_job, interval=cfg.scan_every, first=10)
    app.job_queue.run_repeating(reminder_job, interval=6 * 3600, first=60)
    app.job_queue.run_repeating(weekly_job, interval=6 * 3600, first=120)
    return app


def main():
    cfg = load_config()
    setup_logging(cfg.token)
    app = build_application(cfg, Database(cfg.db_path))
    log.info("bot starting | exchange=%s | plans=%s | symbols=%d | tf=%s | db=%s | be=%s", cfg.exchange,
             [p.id for p in cfg.plans], len(cfg.symbols), cfg.timeframe, cfg.db_path, cfg.be_after_tp1)
    app.run_polling()


if __name__ == "__main__":
    main()
