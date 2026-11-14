from __future__ import annotations
import asyncio, html, logging, os, re, threading
from typing import Any
import httpx
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CallbackQueryHandler, ContextTypes

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

log = logging.getLogger("telegram_notifier")
API_BASE = os.getenv("TRADING_API_BASE", "http://127.0.0.1:8000").rstrip("/")
HTTP_TIMEOUT = 10.0
STOP_LOSS_PCT, TAKE_PROFIT_PCT = 0.10, 0.30

_pending: dict[str, str] = {}
_inflight: set[str] = set()
_listener_thread: threading.Thread | None = None

def _env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing environment variable: {name}")
    return value

def _num(plan: dict[str, Any], *keys: str, default: float | None = None) -> float | None:
    for key in keys:
        if plan.get(key) is not None:
            return float(plan[key])
    return default

def _format_plan(ticker: str, plan: dict[str, Any]) -> str:
    entry = _num(plan, "entry_price", "entry")
    shares = _num(plan, "shares", "qty", "quantity")
    if entry is None or shares is None:
        raise ValueError("trade_plan must include entry_price and shares")
    stop = _num(plan, "stop_loss", "stop", default=entry * (1 - STOP_LOSS_PCT))
    target = _num(plan, "take_profit", "target", default=entry * (1 + TAKE_PROFIT_PCT))
    risk = _num(plan, "max_risk_usd", "max_risk", default=(entry - stop) * shares)
    return (
        "📈 <b>Trade Approval Request</b>\n\n"
        f"<b>Timeframe:</b> {'Short-Term (Intraday)' if plan.get('timeframe') == 'INTRADAY' else 'Long-Term (Swing)'}\n"
        f"<b>Ticker:</b> {html.escape(ticker)}\n"
        f"<b>Entry Price:</b> ${entry:,.2f}\n"
        f"<b>Stop Loss:</b> ${stop:,.2f}\n"
        f"<b>Take Profit:</b> ${target:,.2f}\n"
        f"<b>Shares:</b> {shares:g}\n"
        f"<b>Max Risk:</b> ${risk:,.2f}"
    )

async def _call_api(path: str) -> tuple[bool, str]:
    # HARDCODED TOKEN
    headers = {"x-shield-auth": "secure_shield_key_2026"}
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, headers=headers) as client:
            resp = await client.post(f"{API_BASE}{path}")
        if resp.is_success:
            return True, ""
        return False, f"HTTP {resp.status_code}: {resp.text[:150]}"
    except httpx.HTTPError as exc:
        return False, f"{type(exc).__name__}: {exc}"

def _ticker_from_message(message: Any) -> str:
    match = re.search(r"Ticker:\s*(\S+)", getattr(message, "text", None) or "")
    return match.group(1) if match else "?"

async def send_approval_request(token: str, ticker: str, trade_plan: dict) -> None:
    approve_data, reject_data = f"approve:{token}", f"reject:{token}"
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Approve", callback_data=approve_data),
        InlineKeyboardButton("❌ Reject", callback_data=reject_data),
    ]])
    _pending[token] = ticker
    try:
        async with Bot(_env("TELEGRAM_BOT_TOKEN")) as bot:
            await bot.send_message(
                chat_id=_env("TELEGRAM_CHAT_ID"),
                text=_format_plan(ticker, trade_plan),
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
            )
    except Exception:
        _pending.pop(token, None)
        raise

async def _on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or not query.data or query.message is None:
        return
    if str(query.message.chat.id) != _env("TELEGRAM_CHAT_ID"):
        await query.answer("Not authorised.", show_alert=True)
        return
    action, _, token = query.data.partition(":")
    if token in _inflight:
        await query.answer("Already processing…")
        return
    _inflight.add(token)
    try:
        await query.answer()
        ticker = _pending.get(token) or _ticker_from_message(query.message)
        if action == "approve":
            ok, detail = await _call_api(f"/confirm/{token}")
            if ok:
                text = f"✅ Order Executed: {ticker}"
            else:
                log.error("confirm failed for %s: %s", ticker, detail)
                text = f"⚠️ Execution failed: {ticker}\n{detail}"
        else:
            ok, detail = await _call_api(f"/reject/{token}")
            text = "❌ Trade Rejected by User" if ok else f"⚠️ Reject failed: {ticker}"
        try:
            await query.edit_message_text(text)
        except Exception as _e:
            if "Message is not modified" not in str(_e): raise
        _pending.pop(token, None)
    except Exception:
        log.exception("callback handling failed for token %s", token)
    finally:
        _inflight.discard(token)

async def _poll_forever() -> None:
    app = Application.builder().token(_env("TELEGRAM_BOT_TOKEN")).build()
    app.add_handler(CallbackQueryHandler(_on_callback, pattern=r"^(approve|reject):"))
    async with app:
        await app.start()
        await app.updater.start_polling(allowed_updates=[Update.CALLBACK_QUERY])
        log.info("Telegram listener started")
        await asyncio.Event().wait()

def _thread_main() -> None:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(_poll_forever())
    except Exception:
        log.exception("Telegram listener crashed")
    finally:
        loop.close()

def start_telegram_listener() -> None:
    global _listener_thread
    if _listener_thread and _listener_thread.is_alive():
        return
    _env("TELEGRAM_BOT_TOKEN")
    _env("TELEGRAM_CHAT_ID")
    _listener_thread = threading.Thread(target=_thread_main, name="telegram-listener", daemon=True)
    _listener_thread.start()
