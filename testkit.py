"""ابزار تست: اگر telegram/httpx نصب نباشند، نسخه‌های ساختگی جایگزین می‌شوند (بدون اینترنت و توکن)."""
import sys, types, asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock


def install_stubs():
    try:
        import telegram, telegram.ext, telegram.error  # noqa
    except ImportError:
        tg = types.ModuleType("telegram")
        tg.Update = object
        class LabeledPrice:
            def __init__(self, label, amount): self.label, self.amount = label, amount
        class Btn:
            def __init__(self, text, callback_data=None): self.text, self.callback_data = text, callback_data
        class Markup:
            def __init__(self, kb): self.inline_keyboard = kb
        tg.LabeledPrice, tg.InlineKeyboardButton, tg.InlineKeyboardMarkup = LabeledPrice, Btn, Markup
        err = types.ModuleType("telegram.error")
        class TelegramError(Exception): pass
        class Forbidden(TelegramError): pass
        class BadRequest(TelegramError): pass
        class NetworkError(TelegramError): pass
        class RetryAfter(TelegramError):
            def __init__(self, retry_after): super().__init__("retry"); self.retry_after = retry_after
        err.TelegramError, err.Forbidden, err.BadRequest, err.NetworkError, err.RetryAfter = TelegramError, Forbidden, BadRequest, NetworkError, RetryAfter
        ext = types.ModuleType("telegram.ext")
        for n in ["Application", "CommandHandler", "CallbackQueryHandler", "PreCheckoutQueryHandler", "MessageHandler"]:
            setattr(ext, n, MagicMock())
        ext.filters = MagicMock()
        sys.modules.update({"telegram": tg, "telegram.error": err, "telegram.ext": ext})
    try:
        import httpx  # noqa
    except ImportError:
        hx = types.ModuleType("httpx")
        class HTTPError(Exception): pass
        hx.HTTPError = HTTPError
        hx.AsyncClient = MagicMock()
        sys.modules["httpx"] = hx


install_stubs()
from telegram.error import Forbidden, RetryAfter, TelegramError, NetworkError  # noqa: E402
from config import load_config  # noqa: E402
from database import Database  # noqa: E402
import signal_bot as B  # noqa: E402


async def _nosleep(_): return None
B._sleep = _nosleep


class FakeBot:
    def __init__(self):
        self.sent, self.invoices, self.refunds, self.fail = [], [], [], {}
        self.refund_error = None

    async def send_message(self, chat_id, text, **kw):
        exc = self.fail.get(chat_id)
        if exc is not None:
            if isinstance(exc, list):          # خطاهای متوالی (مثلاً RetryAfter سپس موفق)
                e = exc.pop(0) if exc else None
                if e: raise e
            else:
                raise exc
        self.sent.append((chat_id, text))

    async def send_invoice(self, **kw): self.invoices.append(kw)

    async def send_document(self, chat_id, document, filename=None, caption=None, **kw):
        self.documents = getattr(self, 'documents', [])
        self.documents.append((chat_id, filename, document.read()))

    async def refund_star_payment(self, user_id, telegram_payment_charge_id):
        if self.refund_error: raise self.refund_error
        self.refunds.append((user_id, telegram_payment_charge_id))

    async def get_me(self): return SimpleNamespace(username="testbot")

    def texts(self, uid): return [t for u, t in self.sent if u == uid]


ENV = {"BOT_TOKEN": "TESTTOKEN", "ADMIN_IDS": "1", "DB_PATH": ":memory:", "MIN_SCORE": "1"}


def make_ctx(env=None, args=None):
    cfg = load_config({**ENV, **(env or {})})
    db = Database(":memory:")
    bot = FakeBot()
    return SimpleNamespace(bot=bot, bot_data={"cfg": cfg, "db": db}, args=args or []), cfg, db, bot


def make_update(uid, data=None, payment=None, pre=None):
    msg = SimpleNamespace(reply_text=AsyncMock(), successful_payment=payment)
    cq = SimpleNamespace(data=data, answer=AsyncMock(), edit_message_text=AsyncMock()) if data else None
    return SimpleNamespace(effective_user=SimpleNamespace(id=uid, username=f"u{uid}"), message=msg,
                           callback_query=cq, pre_checkout_query=pre)


def make_pre(uid, payload, currency="XTR", amount=500):
    return SimpleNamespace(from_user=SimpleNamespace(id=uid), invoice_payload=payload, currency=currency,
                           total_amount=amount, answer=AsyncMock())


def make_payment(charge, payload, currency="XTR", amount=500):
    return SimpleNamespace(telegram_payment_charge_id=charge, invoice_payload=payload, currency=currency, total_amount=amount)


def reply_text(update):
    return update.message.reply_text.call_args.args[0]
