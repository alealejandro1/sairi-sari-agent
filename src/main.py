"""Telegram bot entrypoint for the Telegram intake track."""

from __future__ import annotations

import os
import logging
import re
import argparse
import json
from datetime import datetime, timezone
from dataclasses import dataclass
from typing import Dict, List, TYPE_CHECKING

from dotenv import load_dotenv

if TYPE_CHECKING:
    from telegram import Update
    from telegram.ext import ContextTypes


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def _conversation_log_path() -> str:
    return os.getenv("TELEGRAM_CONVERSATION_LOG", "logs/telegram_conversations.jsonl").strip()


def _append_conversation_log(record: Dict[str, object]) -> None:
    path = _conversation_log_path()
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        record["ts_utc"] = datetime.now(timezone.utc).isoformat()
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as exc:
        logger.warning("Conversation log write failed: %s", exc)


def _safe_preview(text: str | None, *, limit: int = 1000) -> str:
    if text is None:
        return ""
    compact = text.replace("\n", "\\n")
    if len(compact) <= limit:
        return compact
    return compact[:limit] + "…"


def _record_inbound(update: "Update", source: str, payload: str | None) -> None:
    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    message_type = message.__class__.__name__ if message else None

    _append_conversation_log(
        {
            "direction": "inbound",
            "source": source,
            "update_id": update.update_id,
            "chat_id": chat.id if chat else None,
            "user_id": user.id if user else None,
            "message_type": message_type,
            "payload": _safe_preview(payload),
        }
    )


async def _record_outbound(update: "Update", *, source: str, payload: str) -> None:
    chat = update.effective_chat
    user = update.effective_user
    message = update.effective_message
    _append_conversation_log(
        {
            "direction": "outbound",
            "source": source,
            "chat_id": chat.id if chat else None,
            "user_id": user.id if user else None,
            "message_id": message.message_id if message else None,
            "message_type": "reply_text",
            "payload": _safe_preview(payload),
        }
    )


async def _reply_with_log(update: "Update", text: str, *, source: str, **kwargs) -> None:
    await update.effective_message.reply_text(text, **kwargs)
    await _record_outbound(update, source=source, payload=text)


async def _edit_with_log(update: "Update", *, source: str, text: str) -> None:
    query = update.callback_query
    await query.edit_message_text(text)
    message = query.message
    _append_conversation_log(
        {
            "direction": "outbound",
            "source": source,
            "chat_id": message.chat_id if message else None,
            "user_id": query.from_user.id if query.from_user else None,
            "message_id": message.message_id if message else None,
            "message_type": "edit_message",
            "payload": _safe_preview(text),
        }
    )


@dataclass
class ParsedLine:
    raw: str
    item: str
    qty: int = 1
    price: float = 0.0


def _allowed_chat_ids() -> set[int]:
    raw = os.getenv("TELEGRAM_ALLOWED_CHAT_IDS", "").strip()
    ids = set()
    for token in raw.split(","):
        token = token.strip()
        if token and re.fullmatch(r"-?\d+", token):
            ids.add(int(token))
    return ids


def _is_allowed(update: Update) -> bool:
    allowed = _allowed_chat_ids()
    if not allowed:
        return True
    chat_id = update.effective_chat.id if update.effective_chat else None
    return chat_id in allowed


def _parse_text_to_lines(text: str) -> List[ParsedLine]:
    lines: List[ParsedLine] = []
    for raw_line in text.splitlines():
        raw = raw_line.strip()
        if not raw:
            continue

        # Very small first-pass parser for patterns like:
        # "chips 2 12" or "soap x2 @ 12"
        at_match = re.match(
            r"^(?P<item>.+?)\s*[xX]?\s*(?P<qty>\d+)?\s*@\s*(?P<price>\d+(?:\.\d{1,2})?)\s*$",
            raw,
        )
        if at_match:
            item = (at_match.group("item") or "").strip() or raw
            qty = int(at_match.group("qty")) if at_match.group("qty") else 1
            price = float(at_match.group("price"))
            lines.append(ParsedLine(raw=raw, item=item, qty=qty, price=price))
            continue

        parts = raw.split()
        if len(parts) >= 3 and parts[-1].replace(".", "", 1).isdigit() and parts[-2].isdigit():
            item = " ".join(parts[:-2]).strip() or raw
            qty = int(parts[-2])
            price = float(parts[-1])
            lines.append(ParsedLine(raw=raw, item=item, qty=qty, price=price))
            continue

        if len(parts) >= 2 and parts[-1].isdigit():
            item = " ".join(parts[:-1]).strip() or raw
            qty = int(parts[-1])
            lines.append(ParsedLine(raw=raw, item=item, qty=qty, price=0.0))
            continue
        lines.append(ParsedLine(raw=raw, item=raw, qty=1, price=0.0))
    return lines[:20]


def _format_draft(draft_id: str, source: str, lines: List[ParsedLine]) -> str:
    if not lines:
        return (
            f"Draft #{draft_id} from {source}: I couldn’t parse anything yet.\n"
            "Send a clearer message or photo and I’ll draft it again."
        )

    body = [f"=== Draft #{draft_id} ({source}) ===", "Sari-Sari Receipt Preview"]
    total = 0.0
    for index, row in enumerate(lines, start=1):
        if row.price > 0:
            subtotal = row.qty * row.price
            total += subtotal
            body.append(
                f"{index:02d}. {row.item} | qty {row.qty} x PHP {row.price:.2f}"
                f" = PHP {subtotal:.2f}"
            )
        else:
            body.append(f"{index:02d}. {row.item} | qty {row.qty}")

    if total > 0:
        body.append("")
        body.append(f"Subtotal: PHP {total:.2f}")
        body.append(f"Grand Total: PHP {total:.2f}")
        body.append(f"TOTAL DUE: PHP {total:.2f}")

    body.append("")
    body.append("Is this correct?")
    return "\n".join(body)


def _get_transactions(context: "ContextTypes.DEFAULT_TYPE") -> List[Dict]:
    return context.application.bot_data.setdefault("transactions", [])


def _get_debtors(context: "ContextTypes.DEFAULT_TYPE") -> List[Dict]:
    return context.application.bot_data.setdefault("debtors", [])


def _get_stock(context: "ContextTypes.DEFAULT_TYPE") -> Dict[str, int]:
    # Stock is optional in this MVP and can be filled by integrations later.
    return context.application.bot_data.setdefault("stock", {})


def _draft_total(draft: Dict) -> float:
    total = 0.0
    for row in draft.get("lines", []):
        qty = float(row.get("qty", 0) or 0)
        price = float(row.get("price", 0.0) or 0.0)
        total += qty * price
    return total


def _add_transaction(context: "ContextTypes.DEFAULT_TYPE", draft_id: str, draft: Dict) -> None:
    if draft.get("transaction_recorded"):
        return

    transactions = _get_transactions(context)
    transactions.append(
        {
            "draft_id": draft_id,
            "chat_id": draft.get("chat_id"),
            "source": draft.get("source", ""),
            "status": draft.get("status", ""),
            "lines": draft.get("lines", []),
            "total": _draft_total(draft),
            "when_utc": datetime.now(timezone.utc).isoformat(),
            "raw": draft.get("raw"),
        }
    )
    draft["transaction_recorded"] = True


def _log_inbound(update: "Update", source: str) -> None:
    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user

    text_preview = None
    if message:
        if message.text:
            text_preview = message.text
        elif message.caption:
            text_preview = message.caption
        elif message.photo:
            text_preview = "<photo>"
        else:
            text_preview = message.__class__.__name__

    callback = update.callback_query
    if callback and callback.data:
        text_preview = f"callback:{callback.data}"

    _record_inbound(update, source, text_preview)
    logger.info(
        "INBOUND %s | update_id=%s | chat_id=%s | user_id=%s | payload=%r",
        source,
        update.update_id,
        chat.id if chat else None,
        user.id if user else None,
        text_preview,
    )


async def _ensure_ready(update: "Update") -> bool:
    if not _is_allowed(update):
        await _reply_with_log(
            update,
            "Sorry, this chat is not authorized for this bot.",
            source="authorization_rejected",
        )
        return False
    return True


async def start(update: "Update", _: "ContextTypes.DEFAULT_TYPE") -> None:
    _log_inbound(update, "COMMAND /start")
    if not await _ensure_ready(update):
        return
    await _reply_with_log(
        update,
        "Hi! I’m the Sari-Sari agent.\n"
        "I’m ready now.\n"
        "Please send either:\n"
        "1) text like `item qty price` (e.g. `soap 2 15`) or\n"
        "2) a photo of a handwritten note.\n"
        "Then I’ll build a draft and ask for confirm/edit/cancel.\n"
        "If you accidentally started this, send /cancel.",
        source="start_ack",
        reply_markup=_command_keyboard(),
    )


def _command_keyboard():
    return ReplyKeyboardMarkup(
        [
            [
                KeyboardButton("/start"),
                KeyboardButton("/ledger"),
                KeyboardButton("/cancel"),
            ],
            [
                KeyboardButton("/insights"),
                KeyboardButton("/debtors"),
                KeyboardButton("/recent10"),
            ],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


async def on_ledger(update: "Update", context: "ContextTypes.DEFAULT_TYPE") -> None:
    _log_inbound(update, "COMMAND /ledger")
    if not await _ensure_ready(update):
        return
    context.user_data["next_photo_mode"] = "ledger"
    await _reply_with_log(
        update,
        "📒 Ledger mode started.\n"
        "Please send the ledger photo next. "
        "I’ll treat it as a ledger page and extract sales from the image.\n"
        "If this is not what you want, send /cancel.",
        source="ledger_mode_started",
        reply_markup=_command_keyboard(),
    )


def _get_drafts(context: "ContextTypes.DEFAULT_TYPE") -> Dict[str, Dict]:
    drafts = context.application.bot_data.setdefault("drafts", {})
    return drafts


async def _send_shortcuts(update: "Update") -> None:
    await _reply_with_log(
        update,
        "Quick actions:\n"
        "Press /cancel if you want to stop this step and reset.",
        source="quick_actions",
        reply_markup=_command_keyboard(),
    )


async def on_text(update: "Update", context: "ContextTypes.DEFAULT_TYPE") -> None:
    _log_inbound(update, "TEXT")
    if not await _ensure_ready(update):
        return

    parsed = _parse_text_to_lines((update.effective_message.text or "").strip())
    draft_id = f"txt-{len(_get_drafts(context)) + 1:04d}"
    context.user_data["active_draft_id"] = draft_id
    _get_drafts(context)[draft_id] = {
        "chat_id": str(update.effective_chat.id),
        "source": "text",
        "raw": update.effective_message.text,
        "lines": [line.__dict__ for line in parsed],
        "status": "pending",
    }

    await _reply_with_log(
        update,
        _format_draft(draft_id, "text", parsed),
        source="text_draft",
        reply_markup=InlineKeyboardMarkup(
            [[
                InlineKeyboardButton("✅ Confirm", callback_data=f"confirm:{draft_id}"),
                InlineKeyboardButton("📝 Edit", callback_data=f"edit:{draft_id}"),
                InlineKeyboardButton("❌ Cancel", callback_data=f"cancel:{draft_id}"),
            ]]
        ),
    )
    await _send_shortcuts(update)


async def on_photo(update: "Update", context: "ContextTypes.DEFAULT_TYPE") -> None:
    _log_inbound(update, "PHOTO")
    if not await _ensure_ready(update):
        return

    mode = context.user_data.pop("next_photo_mode", None)
    if mode == "ledger":
    # Ledger OCR path placeholder (to be wired to OCR engine in a next step).
        draft_id = f"ledger-{len(_get_drafts(context)) + 1:04d}"
        context.user_data["active_draft_id"] = draft_id
        _get_drafts(context)[draft_id] = {
            "chat_id": str(update.effective_chat.id),
            "source": "ledger",
            "raw": "ledger_photo",
            "status": "pending_ocr",
            "ocr_mode": "ledger",
        }
        await _reply_with_log(
            update,
            f"📒 Draft #{draft_id} marked as ledger page.\n"
            "I’ll parse this with OCR and extract all sales. (OCR extraction step is not wired yet)."
            ,
            source="ledger_photo_received",
        )
        await _send_shortcuts(update)
        return

    # Photo parsing is intentionally placeholder in MVP-lite.
    draft_id = f"photo-{len(_get_drafts(context)) + 1:04d}"
    context.user_data["active_draft_id"] = draft_id
    _get_drafts(context)[draft_id] = {
        "chat_id": str(update.effective_chat.id),
        "source": "photo",
        "raw": "photo",
        "lines": [],
        "status": "pending",
    }

    await _reply_with_log(
        update,
        f"Draft #{draft_id} from photo: OCR/vision parsing will be added in the next step.\n"
        "For now, please use this draft as a placeholder and edit by reply text.\n"
        "Is this what you meant?",
        source="photo_draft",
        reply_markup=InlineKeyboardMarkup(
            [[
                InlineKeyboardButton("✅ Confirm", callback_data=f"confirm:{draft_id}"),
                InlineKeyboardButton("📝 Edit", callback_data=f"edit:{draft_id}"),
                InlineKeyboardButton("❌ Cancel", callback_data=f"cancel:{draft_id}"),
            ]]
        ),
    )
    await _send_shortcuts(update)


async def on_callback(update: "Update", context: "ContextTypes.DEFAULT_TYPE") -> None:
    _log_inbound(update, "CALLBACK")
    query = update.callback_query
    await query.answer()

    data = query.data or ""
    action, _, draft_id = data.partition(":")
    drafts = _get_drafts(context)
    draft = drafts.get(draft_id)
    if not draft:
        await _edit_with_log(update, source="callback_draft_not_found", text="Draft not found.")
        return

    if action == "confirm":
        draft["status"] = "confirmed"
        _add_transaction(context, draft_id, draft)
        await _edit_with_log(
            update,
            source="callback_confirm",
            text=f"✅ Draft #{draft_id} confirmed.",
        )
    elif action == "edit":
        draft["status"] = "edit_requested"
        await _edit_with_log(
            update,
            source="callback_edit",
            text=f"✏️ Edit mode for #{draft_id}.\n"
            "Send a corrected text message next. I’ll treat it as a replacement.",
        )
    elif action == "cancel":
        draft["status"] = "cancelled"
        await _edit_with_log(
            update,
            source="callback_cancel",
            text=f"❌ Draft #{draft_id} cancelled.",
        )

    if update.effective_chat and update.effective_message:
        await _reply_with_log(
            update,
            "Quick actions:",
            source="quick_actions",
            reply_markup=_command_keyboard(),
        )


async def on_other_message(update: "Update", _: "ContextTypes.DEFAULT_TYPE") -> None:
    _log_inbound(update, "UNSUPPORTED_MESSAGE")
    if not await _ensure_ready(update):
        return
    await _reply_with_log(
        update,
        "I can only process text and photos right now. Send text like `item qty price`, send a photo,\n"
        "or run `/ledger` first to mark a photo as a ledger page for OCR parsing.\n"
        "If you need to stop, send /cancel.",
        source="unsupported_input",
        reply_markup=_command_keyboard(),
    )


async def on_cancel(update: "Update", context: "ContextTypes.DEFAULT_TYPE") -> None:
    _log_inbound(update, "COMMAND /cancel")
    if not await _ensure_ready(update):
        return

    mode = context.user_data.pop("next_photo_mode", None)
    draft_id = context.user_data.pop("active_draft_id", None)
    cancelled_items: List[str] = []
    if mode:
        cancelled_items.append(f"mode:{mode}")

    drafts = _get_drafts(context)
    if draft_id and draft_id in drafts:
        draft_status = drafts[draft_id].get("status")
        if draft_status in {"pending", "pending_ocr", "edit_requested"}:
            drafts[draft_id]["status"] = "cancelled"
            cancelled_items.append(draft_id)

    if cancelled_items:
        msg = f"🛑 Cancelled ({', '.join(cancelled_items)})."
    else:
        msg = "🛑 Nothing active to cancel."

    await _reply_with_log(
        update,
        f"{msg}\n"
        "Start over with /start for normal mode, or /ledger for ledger-photo mode.",
        source="command_cancelled",
        reply_markup=_command_keyboard(),
    )


async def on_insights(update: "Update", context: "ContextTypes.DEFAULT_TYPE") -> None:
    _log_inbound(update, "COMMAND /insights")
    if not await _ensure_ready(update):
        return

    transactions = _get_transactions(context)
    total_sales = sum(float(t.get("total", 0.0) or 0.0) for t in transactions)
    sales_count = len(transactions)

    stock = _get_stock(context)
    if stock:
        stock_lines = [f"• {name}: {int(qty)} unit(s)" for name, qty in sorted(stock.items())]
        stock_block = "\n".join(["Stock (tracked):"] + stock_lines)
    else:
        stock_block = "Stock (tracked): not configured yet."

    await _reply_with_log(
        update,
        "📊 Sales & Stock Insight\n\n"
        f"{stock_block}\n\n"
        f"Sales logged: {sales_count} transaction(s)\n"
        f"Total sales value: PHP {total_sales:.2f}\n\n"
        "Use /debtors for people who owe and /recent10 for latest transactions.",
        source="insights_report",
        reply_markup=_command_keyboard(),
    )


async def on_debtors(update: "Update", context: "ContextTypes.DEFAULT_TYPE") -> None:
    _log_inbound(update, "COMMAND /debtors")
    if not await _ensure_ready(update):
        return

    debtors = _get_debtors(context)
    if not debtors:
        await _reply_with_log(
            update,
            "📒 People who owe: none currently.\n"
            "Use /insights for sales/stock summary and /recent10 for last transactions.",
            source="debtors_empty",
            reply_markup=_command_keyboard(),
        )
        return

    lines = ["📒 People who owe"]
    for idx, row in enumerate(debtors, start=1):
        name = str(row.get("name", "Unknown"))
        amount = float(row.get("amount", 0.0) or 0.0)
        since = row.get("since", "unknown date")
        lines.append(f"{idx}) {name} — PHP {amount:.2f} since {since}")

    await _reply_with_log(
        update,
        "\n".join(lines),
        source="debtors_report",
        reply_markup=_command_keyboard(),
    )


async def on_recent_transactions(update: "Update", context: "ContextTypes.DEFAULT_TYPE") -> None:
    _log_inbound(update, "COMMAND /recent10")
    if not await _ensure_ready(update):
        return

    transactions = list(_get_transactions(context))
    if not transactions:
        await _reply_with_log(
            update,
            "No transactions logged yet.",
            source="recent_transactions_empty",
            reply_markup=_command_keyboard(),
        )
        return

    lines = ["🧾 Last 10 transactions"]
    for idx, tx in enumerate(reversed(transactions[-10:]), start=1):
        draft_id = tx.get("draft_id", "unknown")
        total = float(tx.get("total", 0.0) or 0.0)
        when = (tx.get("when_utc") or "unknown time").replace("T", " ")[:19]
        src = tx.get("source", "unknown")
        lines.append(
            f"{idx}) #{draft_id} | {src} | PHP {total:.2f} | {when}"
        )

    await _reply_with_log(
        update,
        "\n".join(lines),
        source="recent_transactions_report",
        reply_markup=_command_keyboard(),
    )


async def on_unknown_command(update: "Update", _: "ContextTypes.DEFAULT_TYPE") -> None:
    _log_inbound(update, "UNHANDLED_COMMAND")
    if not await _ensure_ready(update):
        return
    message_text = (update.effective_message.text or "").strip()
    command = message_text.split(maxsplit=1)[0] if message_text else "/"
    await _reply_with_log(
        update,
        f"I received {command}.\n"
        "Currently supported commands are:\n"
        "• /start — begin normal intake flow\n"
        "• /ledger — start ledger OCR flow (send ledger photo next)\n"
        "• /cancel — stop any in-progress action\n"
        "• /insights — sales and stock summary\n"
        "• /debtors — list who owes since when and how much\n"
        "• /recent10 — latest 10 transactions\n"
        "If you meant to process a ledger photo, send `/ledger` and then send the photo.",
        source="unknown_command",
        reply_markup=_command_keyboard(),
    )


async def on_error(update: "Update", context) -> None:
    logger.exception(
        "Unhandled error. update_id=%s | error=%s",
        getattr(update, "update_id", None),
        context.error,
    )


def main() -> None:
    # Smoke-safe entrypoint for tests and lightweight invocation.
    # Use `python3 src/main.py` (below) to run the live bot.
    print("sairi-sari-agent bootstrap ready")


def run_bot() -> None:
    try:
        from telegram import (
            InlineKeyboardButton,
            InlineKeyboardMarkup,
            ReplyKeyboardMarkup,
            KeyboardButton,
        )

        # Make button classes available to handlers that are defined at module level.
        globals()["InlineKeyboardButton"] = InlineKeyboardButton
        globals()["InlineKeyboardMarkup"] = InlineKeyboardMarkup
        globals()["ReplyKeyboardMarkup"] = ReplyKeyboardMarkup
        globals()["KeyboardButton"] = KeyboardButton
    except ModuleNotFoundError as exc:
        if exc.name == "telegram":
            raise SystemExit(
                "Missing dependency: telegram. Install with "
                "`python3 -m pip install python-telegram-bot python-dotenv`"
            ) from exc
        raise

    from telegram.ext import (
        ApplicationBuilder,
        CallbackQueryHandler,
        CommandHandler,
        ContextTypes,
        MessageHandler,
        filters,
    )
    from telegram.request import HTTPXRequest

    load_dotenv()
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN is required. Put it in a local .env file."
        )

    insecure_tls = os.getenv("TELEGRAM_INSECURE_TLS", "").strip().lower() in {
        "1",
        "true",
        "yes",
    }

    if insecure_tls:
        logger.warning(
            "TELEGRAM_INSECURE_TLS is enabled. Certificate verification is disabled for Telegram requests."
        )

    ca_bundle = (
        os.getenv("TELEGRAM_CA_BUNDLE", "").strip()
        or os.getenv("REQUESTS_CA_BUNDLE", "").strip()
        or os.getenv("CURL_CA_BUNDLE", "").strip()
    )

    request_kwargs: Dict[str, object] = {}
    if insecure_tls:
        request_kwargs["verify"] = False
    elif ca_bundle:
        if not os.path.exists(ca_bundle):
            raise RuntimeError(
                "TELEGRAM_CA_BUNDLE is set but file does not exist: " + ca_bundle
            )
        logger.info("Using custom CA bundle for Telegram requests: %s", ca_bundle)
        request_kwargs["verify"] = ca_bundle
    else:
        try:
            import certifi
        except ModuleNotFoundError:
            logger.debug("certifi is not installed; using httpx defaults for certificate verification.")
        else:
            request_kwargs["verify"] = certifi.where()
            logger.info("Using bundled CA certificates: %s", certifi.where())

    if request_kwargs:
        request = HTTPXRequest(httpx_kwargs=request_kwargs)
        application = (
            ApplicationBuilder()
            .token(token)
            .request(request=request)
            .get_updates_request(request)
            .build()
        )
    else:
        application = ApplicationBuilder().token(token).build()
    logger.info("Registering bot handlers: /start, /ledger, /help, fallback command handler")
    logger.info("Conversation logs: %s", _conversation_log_path())
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("ledger", on_ledger))
    application.add_handler(CommandHandler("cancel", on_cancel))
    application.add_handler(CommandHandler("insights", on_insights))
    application.add_handler(CommandHandler("debtors", on_debtors))
    application.add_handler(CommandHandler("recent10", on_recent_transactions))
    application.add_handler(CommandHandler("help", on_unknown_command))
    application.add_handler(
        MessageHandler(filters.COMMAND, on_unknown_command)
    )
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    application.add_handler(MessageHandler(filters.PHOTO, on_photo))
    application.add_handler(CallbackQueryHandler(on_callback))
    application.add_handler(
        MessageHandler(filters.ALL & ~filters.TEXT & ~filters.PHOTO, on_other_message)
    )
    application.add_error_handler(on_error)
    application.run_polling(close_loop=False)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="sairi-sari-agent")
    parser.add_argument(
        "mode",
        nargs="?",
        default="smoke",
        choices=["smoke", "bot"],
        help="smoke: run startup check, bot: connect to Telegram",
    )
    args = parser.parse_args()
    if args.mode == "bot":
        offline = os.getenv("SAIRI_OFFLINE", "").strip().lower() in {"1", "true", "yes"}
        if offline:
            raise SystemExit("Offline mode enabled via SAIRI_OFFLINE=1. Skipping Telegram connection.")
        run_bot()
    else:
        main()
