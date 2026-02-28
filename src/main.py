"""Telegram bot entrypoint for the Telegram intake track."""

from __future__ import annotations

import os
import logging
import re
import argparse
import json
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, TYPE_CHECKING
from html import escape

from business_state import BusinessStateStore
from ledger_ocr import extract_text_from_image, format_ledger_draft, parse_ledger_ocr_text
from telegram.constants import ParseMode

from dotenv import load_dotenv

if TYPE_CHECKING:
    from telegram import Update
    from telegram.ext import ContextTypes


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def _conversation_log_path() -> str:
    return os.getenv("TELEGRAM_CONVERSATION_LOG", "logs/telegram_conversations.jsonl").strip()


def _ledger_ocr_log_path() -> str:
    return os.getenv("LEDGER_OCR_LOG", "logs/ledger_ocr.jsonl").strip()


def _business_state_store_path() -> str:
    return os.getenv("BUSINESS_STATE_STORE_PATH", "data/business_state.json").strip()

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


def _append_ledger_ocr_log(record: Dict[str, object]) -> None:
    path = _ledger_ocr_log_path()
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        record["ts_utc"] = datetime.now(timezone.utc).isoformat()
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as exc:
        logger.debug("Ledger OCR log write failed: %s", exc)


def _safe_preview(text: str | None, *, limit: int = 1000) -> str:
    if text is None:
        return ""
    compact = text.replace("\n", "\\n")
    if len(compact) <= limit:
        return compact
    return compact[:limit] + "…"


def _safe_json_preview(payload: Dict[str, object], *, limit: int = 4000) -> str:
    rendered = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2)
    if len(rendered) <= limit:
        return rendered
    return rendered[: limit - 3] + "..."


def _ledger_photo_info(message: object) -> Dict[str, object]:
    photo = getattr(message, "photo", []) or []
    selected = photo[-1] if photo else None
    if not selected:
        return {}
    return {
        "telegram_file_id": str(getattr(selected, "file_id", "")),
        "telegram_file_unique_id": str(getattr(selected, "file_unique_id", "")),
        "telegram_file_size": getattr(selected, "file_size", None),
        "telegram_width": getattr(selected, "width", None),
        "telegram_height": getattr(selected, "height", None),
    }


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


def _get_stock(context: "ContextTypes.DEFAULT_TYPE") -> Dict[str, float]:
    store = _get_business_state(context)
    return {
        key: float(item.get("on_hand_qty", 0.0))
        for key, item in store.get_inventory().items()
        if isinstance(item, dict)
    }


def _get_business_state(context: "ContextTypes.DEFAULT_TYPE") -> BusinessStateStore:
    store = context.application.bot_data.get("business_state_store")
    if isinstance(store, BusinessStateStore):
        return store
    store = BusinessStateStore(_business_state_store_path())
    context.application.bot_data["business_state_store"] = store
    return store


def _parse_money(amount_text: str) -> Optional[float]:
    if not amount_text:
        return None
    if amount_text.strip().lower() in {"na", "n/a", "-"}:
        return None
    cleaned = amount_text.strip().replace(",", "").replace("₱", "").replace("P", "")
    if not re.fullmatch(r"^-?\d+(?:\.\d{1,2})?$", cleaned):
        # Keep permissive but safe: optional sign, at most two decimals.
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _snapshot_amount_if_any(state: BusinessStateStore, *, since_days: int) -> tuple[float, float]:
    """Returns (current amount, oldest amount within window or first available)."""
    snapshots = state.get_cash_snapshots()
    if not snapshots:
        return 0.0, 0.0

    window_start = datetime.now(timezone.utc) - timedelta(days=since_days)
    oldest_in_window = None
    latest = float(snapshots[-1].get("cash_amount", 0.0))

    for snapshot in snapshots:
        raw_ts = str(snapshot.get("snapshot_utc", ""))
        try:
            parsed_ts = datetime.fromisoformat(raw_ts.replace("Z", "+00:00"))
        except ValueError:
            continue
        if parsed_ts >= window_start:
            oldest_in_window = float(snapshot.get("cash_amount", 0.0))
            break

    if oldest_in_window is None:
        oldest_in_window = float(snapshots[0].get("cash_amount", 0.0))

    return latest, oldest_in_window


def _parse_iso_datetime(value: str | None) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value
    raw = str(value)
    try:
        value_dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        try:
            value_dt = datetime.fromisoformat(raw)
        except ValueError:
            return None
    if value_dt.tzinfo is None:
        return value_dt.replace(tzinfo=timezone.utc)
    return value_dt


def _safe_float(value: Any, *, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        if isinstance(value, (int, float)):
            return float(value)
        candidate = str(value).replace(",", "").replace("₱", "").replace("P", "").strip()
        return float(candidate) if candidate else default
    except (TypeError, ValueError):
        return default


def _money(value: float) -> str:
    return f"PHP {value:,.2f}"


def _resolve_insight_mode(raw: str) -> str:
    normalized = (raw or "").strip().lower().replace("-", "_")
    aliases = {
        "opening": "opening",
        "open": "opening",
        "start": "opening",
        "morning": "opening",
        "midday": "midday",
        "noon": "midday",
        "mid": "midday",
        "visit": "visit",
        "wholesaler": "visit",
        "vendor": "visit",
        "supplier": "supplier",
        "offer": "supplier",
        "offers": "supplier",
        "price_list": "supplier",
        "close": "close",
        "closing": "close",
        "day_end": "close",
        "due": "due",
        "due_soon": "due",
        "repayment": "due",
        "loan_due": "due",
        "week": "weekly",
        "weekly": "weekly",
        "overview": "overview",
        "all": "overview",
        "default": "overview",
    }
    if normalized in aliases:
        return aliases[normalized]
    return normalized if normalized in {"opening", "midday", "visit", "supplier", "close", "due", "weekly"} else "overview"


def _collect_insight_metrics(state: BusinessStateStore) -> Dict[str, Any]:
    now = datetime.now(timezone.utc)
    week_start = now - timedelta(days=7)
    prev_week_start = now - timedelta(days=14)

    current_cash, old_cash = _snapshot_amount_if_any(state, since_days=7)
    cash_delta = current_cash - old_cash
    cash_delta_pct = 0.0 if old_cash == 0 else (cash_delta / old_cash)
    cash_snapshots = state.get_cash_snapshots()
    latest_snapshot = cash_snapshots[-1] if cash_snapshots else None
    latest_snapshot_ts = _parse_iso_datetime(
        latest_snapshot.get("snapshot_utc") if isinstance(latest_snapshot, dict) else None
    ) if latest_snapshot else None
    cash_age_hours: float | None = None
    if latest_snapshot_ts:
        cash_age_hours = max(0.0, (now - latest_snapshot_ts).total_seconds()) / 3600.0

    week_sales_total = _sum_sales_between(state, week_start, None)
    prev_week_sales_total = _sum_sales_between(state, prev_week_start, week_start)
    total_sales_count = len(state.load().get("sales", []))
    sales_change_pct = 0.0
    if prev_week_sales_total > 0:
        sales_change_pct = (week_sales_total - prev_week_sales_total) / prev_week_sales_total

    inventory = state.get_inventory()
    critical_stock: List[Dict[str, Any]] = []
    for key, product in inventory.items():
        if not isinstance(product, dict):
            continue
        qty = _safe_float(product.get("on_hand_qty"), default=0.0)
        reorder_point = _safe_float(product.get("reorder_point"), default=0.0)
        threshold = reorder_point if reorder_point > 0 else 0.0
        if qty <= threshold:
            critical_stock.append(
                {
                    "sku_id": key,
                    "name": str(product.get("display_name") or key),
                    "qty": qty,
                    "reorder_point": reorder_point,
                    "unit": str(product.get("unit") or "pcs"),
                }
            )
    critical_stock.sort(key=lambda item: item["qty"])

    debtors = state.get_open_debtors()
    debtors_count = len(debtors)
    total_utang = state.total_open_utang()
    stale_debtors: List[Dict[str, Any]] = []
    for row in debtors:
        updated_raw = row.get("last_updated_utc")
        updated_at = _parse_iso_datetime(str(updated_raw) if updated_raw else None)
        if not updated_at:
            stale_debtors.append(
                {
                    "name": str(row.get("customer_name", "Unknown")),
                    "balance": _safe_float(row.get("current_balance")),
                    "days_since_update": None,
                }
            )
            continue
        days = int((now - updated_at).total_seconds() // 86400)
        if days >= 7:
            stale_debtors.append(
                {
                    "name": str(row.get("customer_name", "Unknown")),
                    "balance": _safe_float(row.get("current_balance")),
                    "days_since_update": days,
                }
            )
    top_debt_share = 0.0
    if debtors and total_utang > 0:
        top_debt_share = _safe_float(debtors[0].get("current_balance"), default=0.0) / total_utang

    raw_loans = state.load().get("loans", {})
    total_loans = state.total_open_loans()
    due_soon: List[Dict[str, Any]] = []
    for raw_loan in raw_loans.values():
        if not isinstance(raw_loan, dict):
            continue
        if raw_loan.get("status", "active") != "active":
            continue
        due_raw = raw_loan.get("next_due_date")
        due_dt = _parse_iso_datetime(str(due_raw) if due_raw else None)
        if not due_dt:
            continue
        if now <= due_dt <= (now + timedelta(days=3)):
            due_soon.append(
                {
                    "lender": str(raw_loan.get("lender_name", "Unknown")),
                    "balance": _safe_float(raw_loan.get("current_balance"), default=0.0),
                    "due_in_days": int((due_dt - now).total_seconds() // 86400),
                }
            )

    due_soon.sort(key=lambda item: item["due_in_days"])

    loan_cash_ratio = 0.0 if total_loans == 0 else float("inf")
    if current_cash > 0:
        loan_cash_ratio = total_loans / current_cash

    return {
        "now": now,
        "current_cash": current_cash,
        "cash_delta": cash_delta,
        "cash_delta_pct": cash_delta_pct,
        "cash_age_hours": cash_age_hours,
        "has_cash": current_cash > 0 or bool(cash_snapshots),
        "week_sales": week_sales_total,
        "prev_week_sales": prev_week_sales_total,
        "sales_change_pct": sales_change_pct,
        "sales_count": total_sales_count,
        "critical_stock": critical_stock,
        "debtors": debtors,
        "debtors_count": debtors_count,
        "total_utang": total_utang,
        "top_debt_share": top_debt_share,
        "stale_debtors": stale_debtors,
        "total_loans": total_loans,
        "loan_cash_ratio": loan_cash_ratio,
        "due_soon": due_soon,
    }


def _sum_sales_between(
    state: BusinessStateStore,
    since: datetime,
    until: datetime | None = None,
) -> float:
    total = 0.0
    for sale in state.load().get("sales", []):
        if not isinstance(sale, dict):
            continue
        recorded = _parse_iso_datetime(str(sale.get("recorded_utc", "")))
        if not recorded:
            continue
        if recorded < since:
            continue
        if until and recorded >= until:
            continue
        total += _safe_float(sale.get("total"), default=0.0)
    return round(total, 2)


def _append_warning(lines: List[str], level: str, title: str, detail: str) -> None:
    icon = {
        "red": "🚨",
        "amber": "⚠️",
        "green": "✅",
    }.get(level, "•")
    lines.append(f"{icon} {title}: {detail}")


def _trend_emoji(level: str) -> str:
    return {"up": "📈", "flat": "➡️", "down": "📉"}.get(level, "➡️")


def _build_overview_lines(metrics: Dict[str, Any]) -> List[str]:
    critical_stock = metrics["critical_stock"]
    stock_lines = ["• No tracked stock below threshold yet."] if not critical_stock else [
        f"• {item['name']} ({item['qty']:.0f} {item['unit']})" for item in critical_stock[:3]
    ]

    lines = [
        "📊 Overview Health Check",
        "",
        f"Week sales: {_money(metrics['week_sales'])}",
        f"Prev 7 days sales: {_money(metrics['prev_week_sales'])}",
        f"Sales trend: {_trend_emoji(_trend_for_sales(metrics))} {_trend_for_sales(metrics)}",
        f"Cash now: {_money(metrics['current_cash']) if metrics['has_cash'] else 'not recorded'}",
        f"Cash change (7 days): {_money(metrics['cash_delta'])} ({metrics['cash_delta_pct']:.0%})",
        f"Open utang: {_money(metrics['total_utang'])}",
        f"Outstanding loans: {_money(metrics['total_loans'])}",
        "",
        "Critical stock (low):",
    ]
    lines.extend(stock_lines)
    if metrics["debtors_count"]:
        top_debtor = metrics["debtors"][0]
        lines.extend([
            "",
            "Top debtor:",
            f"• {top_debtor.get('customer_name')} — {_money(_safe_float(top_debtor.get('current_balance')))}",
        ])
    else:
        lines.append("")
        lines.append("Top debtor: none")
    lines.append("")
    lines.append("Did I make money this week?")
    if _trend_for_sales(metrics) == "up":
        lines.append("Likely yes.")
    elif _trend_for_sales(metrics) == "flat":
        lines.append("Not clear yet.")
    else:
        lines.append("Likely not.")
    return lines


def _trend_for_sales(metrics: Dict[str, Any]) -> str:
    prev_sales = _safe_float(metrics.get("prev_week_sales"))
    week_sales = _safe_float(metrics.get("week_sales"))
    if week_sales > 0 and prev_sales == 0:
        return "up"
    if prev_sales > 0 and week_sales < (prev_sales * 0.75):
        return "down"
    if week_sales > prev_sales:
        return "up"
    if week_sales < prev_sales:
        return "down"
    return "flat"


def _build_opening_lines(metrics: Dict[str, Any]) -> List[str]:
    lines = [
        "🕘 Opening Day Insight",
        "",
        f"Cash now: {_money(metrics['current_cash']) if metrics['has_cash'] else 'not recorded'}",
        f"Week cash change: {_money(metrics['cash_delta'])} ({metrics['cash_delta_pct']:.0%})",
    ]
    if metrics["cash_age_hours"] is None:
        _append_warning(lines, "amber", "Cash baseline missing", "No cash snapshot yet.")
    if metrics["cash_delta_pct"] <= -0.2:
        _append_warning(
            lines,
            "red",
            "Cash is dropping",
            "You may be too tight to do a full restock this morning.",
        )
    elif metrics["cash_delta_pct"] <= -0.1:
        _append_warning(
            lines,
            "amber",
            "Cash is weakening",
            "Keep purchases small today until sales recover.",
        )
    else:
        _append_warning(lines, "green", "Liquidity", "Cash trend is acceptable for opening.")

    if metrics["loan_cash_ratio"] == float("inf"):
        _append_warning(lines, "red", "Loan pressure", "You have active loans but no recent cash baseline.")
    elif metrics["loan_cash_ratio"] >= 1.2:
        _append_warning(lines, "red", "Loan pressure", "Loans are heavier than cash.")
    elif metrics["loan_cash_ratio"] >= 0.6:
        _append_warning(
            lines,
            "amber",
            "Loan pressure",
            "Borrowing should be limited before new buying.",
        )

    if metrics["top_debt_share"] >= 0.6:
        _append_warning(lines, "amber", "Debt concentration", "One or two customers dominate open utang.")
    elif not metrics["debtors_count"]:
        _append_warning(lines, "green", "Debt concentration", "No open utang now.")

    critical_count = len(metrics["critical_stock"])
    if critical_count >= 2:
        _append_warning(
            lines,
            "amber",
            "Stock fragility",
            f"{critical_count} tracked item(s) already at or below reorder point.",
        )

    lines.extend([
        "",
        "Best actions now:",
        "1) /cash to capture a fresh snapshot",
        "2) /insight_visit before wholesaler trip",
    ])
    return lines


def _build_midday_lines(metrics: Dict[str, Any]) -> List[str]:
    lines = [
        "☀️ Midday Check-in",
        "",
        f"Week sales pace: {_money(metrics['week_sales'])}",
        f"Critical stock now: {len(metrics['critical_stock'])}",
    ]
    if len(metrics["critical_stock"]) == 0:
        _append_warning(lines, "green", "Stock", "No immediate low-stock signal.")
    else:
        low = metrics["critical_stock"][:3]
        lines.append("Items that may need replenishment:")
        for row in low:
            lines.append(f"• {row['name']} — {_money(row['qty'])} {row['unit']}")
        if len(metrics["critical_stock"]) >= 2:
            _append_warning(
                lines,
                "amber",
                "Stock pressure",
                "Protect these SKUs and delay weak movers.",
            )
    if metrics["cash_delta_pct"] < -0.1:
        _append_warning(lines, "amber", "Liquidity", "Watch discretionary restock while cash is weakening.")
    lines.extend([
        "",
        "Best actions now:",
        "1) Adjust by /stock only for must-have items",
        "2) /insight_close before locking the day",
    ])
    return lines


def _build_visit_lines(metrics: Dict[str, Any]) -> List[str]:
    lines = [
        "🧺 Wholesaler Visit Prep",
        "",
        f"Cash: {_money(metrics['current_cash']) if metrics['has_cash'] else 'not recorded'}",
        f"Loan pressure ratio: {metrics['loan_cash_ratio']:.2f}" if metrics["loan_cash_ratio"] != float("inf") else "Loan pressure ratio: high (cash baseline missing)",
        f"Open utang: {_money(metrics['total_utang'])}",
        f"Critical stock count: {len(metrics['critical_stock'])}",
    ]
    if metrics["loan_cash_ratio"] >= 0.6:
        _append_warning(
            lines,
            "amber",
            "Borrowing risk",
            "Avoid adding high-cost items if cash is tight.",
        )
    if len(metrics["critical_stock"]) >= 1:
        lines.append("Priority restock candidates:")
        for row in metrics["critical_stock"][:3]:
            lines.append(
                f"• {row['name']} — {row['qty']:.0f} {row['unit']} "
                f"(reorder {row['reorder_point']:.0f})"
            )
    if metrics["top_debt_share"] >= 0.6:
        _append_warning(lines, "red", "Cash risk", "Utang concentration is high; prioritize collection before major purchases.")
    lines.extend([
        "",
        "Best actions now:",
        "1) Capture /cash before placing big orders",
        "2) Visit with a small list first, expand only if needed",
    ])
    return lines


def _build_supplier_lines(metrics: Dict[str, Any]) -> List[str]:
    lines = [
        "📄 Supplier Offer Response",
        "",
        f"Cash available: {_money(metrics['current_cash']) if metrics['has_cash'] else 'not recorded'}",
        f"Loan pressure ratio: {metrics['loan_cash_ratio']:.2f}"
        if metrics["loan_cash_ratio"] != float("inf")
        else "Loan pressure ratio: high (cash baseline missing)",
    ]
    if metrics["cash_delta_pct"] <= -0.15:
        _append_warning(lines, "red", "Price response", "Do not add items aggressively; prioritize cash first.")
    else:
        _append_warning(lines, "green", "Price response", "You can compare offer with current stock needs.")
    critical_stock = metrics["critical_stock"][:3]
    if critical_stock:
        lines.append("Items currently critical:")
        for row in critical_stock:
            lines.append(f"• {row['name']} — {row['qty']:.0f} {row['unit']}")
    if metrics["total_loans"] > 0 and metrics["current_cash"] < metrics["total_loans"]:
        _append_warning(lines, "amber", "Cash reserve", "High loan obligations may require tighter buying.")
    lines.extend([
        "",
        "Best actions now:",
        "1) Mark only must-have SKUs from the offer",
        "2) /insight_week for weekly decision context",
    ])
    return lines


def _build_close_lines(metrics: Dict[str, Any]) -> List[str]:
    lines = [
        "🌙 End-of-Day / Ledger Review",
        "",
        f"Cash change today (7 days): {_money(metrics['cash_delta'])}",
        f"Open utang: {_money(metrics['total_utang'])}",
        f"Stale debtor count: {len(metrics['stale_debtors'])}",
    ]
    if not metrics["stale_debtors"]:
        _append_warning(lines, "green", "Ledger", "No stale debtors in last 7 days.")
    else:
        lines.append("Debtors to follow up:")
        for row in metrics["stale_debtors"][:3]:
            lines.append(f"• {row['name']} — {_money(row['balance'])}")
        _append_warning(lines, "amber", "Collections", "Prioritize these people first.")
    if metrics["debtors_count"]:
        top_debtor = metrics["debtors"][0]
        lines.append(f"Top debt exposure: {top_debtor.get('customer_name')} — {_money(_safe_float(top_debtor.get('current_balance')))}")
    lines.extend([
        "",
        "Best actions now:",
        "1) /cash to close the day",
        "2) /debtors and ask for one focused collection follow-up",
    ])
    return lines


def _build_due_lines(metrics: Dict[str, Any]) -> List[str]:
    due_soon = metrics["due_soon"]
    lines = [
        "⏰ Loan/Repayment Risk",
        "",
        f"Open shop loans: {_money(metrics['total_loans'])}",
        f"Due within 3 days: {len(due_soon)}",
    ]
    if not due_soon:
        _append_warning(lines, "green", "Payments", "No loans due in the next 3 days.")
    else:
        for row in due_soon:
            lines.append(f"• {row['lender']} — {_money(row['balance'])} (due in {row['due_in_days']} day(s))")
        _append_warning(
            lines,
            "red",
            "Repayment pressure",
            "Keep non-essential spending low until due dates are handled.",
        )
    if metrics["loan_cash_ratio"] >= 1.0:
        _append_warning(lines, "amber", "Cash pressure", "Loan burden is above current cash.")
    lines.extend([
        "",
        "Best actions now:",
        "1) /insight_week to check weekly pressure",
        "2) /cash to update amount before payment planning",
    ])
    return lines


def _build_weekly_lines(metrics: Dict[str, Any]) -> List[str]:
    trend = _trend_for_sales(metrics)
    lines = [
        "📅 Weekly Pattern",
        "",
        f"Week sales: {_money(metrics['week_sales'])}",
        f"Prev week sales: {_money(metrics['prev_week_sales'])}",
        f"Weekly sales trend: {_trend_emoji(trend)} {trend}",
        f"Week cash change: {_money(metrics['cash_delta'])}",
    ]
    if trend == "up":
        _append_warning(lines, "green", "Direction", "Momentum is positive.")
    elif trend == "down":
        _append_warning(lines, "red", "Direction", "Momentum is down; tighten discretionary cash outflow.")
    else:
        _append_warning(lines, "amber", "Direction", "Sales are mixed. Review supplier + utang decisions.")

    if metrics["top_debt_share"] >= 0.5:
        _append_warning(lines, "amber", "Exposure", "Debt concentration is rising.")
    if metrics["total_utang"] > 0 and metrics["current_cash"] < metrics["total_utang"]:
        _append_warning(lines, "amber", "Liquidity", "Open utang is above cash reserve.")
    lines.extend([
        "",
        "Best actions this week:",
        "1) Use /insight_visit before next wholesaler trip",
        "2) /insight_open again tomorrow after cash update",
    ])
    return lines


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

    lines = draft.get("lines", [])
    if not lines:
        draft["transaction_recorded"] = True
        return

    transactions = _get_transactions(context)
    transactions.append(
        {
            "draft_id": draft_id,
            "chat_id": draft.get("chat_id"),
            "source": draft.get("source", ""),
            "status": draft.get("status", ""),
            "lines": lines,
            "total": _draft_total(draft),
            "when_utc": datetime.now(timezone.utc).isoformat(),
            "raw": draft.get("raw"),
        }
    )
    state = _get_business_state(context)
    state_result = state.add_sale_record(
        draft_id=draft_id,
        source=draft.get("source", "text"),
        raw=str(draft.get("raw", "")),
        lines=draft.get("lines", []),
        paid_cash=True,
    )
    draft["sale_record_result"] = state_result
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
        "Quickly capture:\n"
        "1) text like `item qty price` (e.g. `soap 2 15`) or\n"
        "2) `/cash 1200` for a cash snapshot,\n"
        "3) `/loan lender_name amount` for a shop debt, or\n"
        "4) a photo of a handwritten note.\n"
        "Use one of these insight moments whenever:\n"
        "• /insight_open, /insight_visit, /insight_supplier, /insight_midday,\n"
        "• /insight_close, /insight_due, /insight_week\n"
        "Then I’ll build a draft and ask for confirm/cancel.\n"
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
                KeyboardButton("/cash"),
                KeyboardButton("/loan"),
                KeyboardButton("/stock"),
            ],
            [
                KeyboardButton("/insight_open"),
                KeyboardButton("/insight_visit"),
                KeyboardButton("/insight_supplier"),
            ],
            [
                KeyboardButton("/insight_midday"),
                KeyboardButton("/insight_close"),
                KeyboardButton("/insight_due"),
            ],
            [
                KeyboardButton("/insights"),
                KeyboardButton("/insight_week"),
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


def _build_confirm_markup(draft_id: str) -> "InlineKeyboardMarkup":
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("✅ Confirm", callback_data=f"confirm:{draft_id}"),
            InlineKeyboardButton("❌ Cancel", callback_data=f"cancel:{draft_id}"),
        ]]
    )


def _format_cash_draft(draft_id: str, amount: float) -> str:
    return (
        f"=== Draft #{draft_id} (cash) ===\n"
        f"Cash snapshot: PHP {amount:.2f}\n"
        "Is this correct?"
    )


def _format_loan_draft(draft_id: str, lender: str, principal: float, interest: float | None = None, due: str | None = None) -> str:
    msg = [f"=== Draft #{draft_id} (loan) ==="]
    msg.append(f"Lender: {lender}")
    msg.append(f"Principal: PHP {principal:.2f}")
    if interest is not None:
        msg.append(f"Interest: {interest:.2f}")
    if due:
        msg.append(f"Due: {due}")
    msg.append("Is this correct?")
    return "\n".join(msg)


def _format_stock_draft(draft_id: str, sku: str, qty_delta: float, reason: str) -> str:
    sign = "add" if qty_delta >= 0 else "remove"
    return (
        f"=== Draft #{draft_id} (inventory) ===\n"
        f"Item: {sku}\n"
        f"{sign.title()} {abs(qty_delta):.3f} unit(s)\n"
        f"Reason: {reason}\n"
        "Is this correct?"
    )


def _build_ledger_payload(
    draft_id: str,
    *,
    customer_name: str,
    parsed: Dict,
    ocr_text: str,
    chat_id: int,
) -> Dict[str, object]:
    return {
        "schema": "sairi_ledger_draft_v1",
        "draft_id": draft_id,
        "source": "telegram_photo_ledger",
        "chat_id": chat_id,
        "customer_name": customer_name or "Unknown",
        "received_at_utc": datetime.now(timezone.utc).isoformat(),
        "raw_ocr_text": ocr_text,
        "warnings": list(parsed.get("warnings", [])),
        "rows": list(parsed.get("entries", [])),
    }


def _json_snippet(payload: Dict[str, object], *, max_chars: int = 3000) -> str:
    raw = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    if len(raw) <= max_chars:
        return raw
    head = raw[: max_chars - 12]
    return f"{head}\n..."


def _parse_loan_text(text: str) -> Optional[Dict[str, object]]:
    tokens = text.strip().split()
    if len(tokens) < 2:
        return None

    principal_index: int | None = None
    principal: float | None = None
    for i in range(len(tokens) - 1, -1, -1):
        parsed = _parse_money(tokens[i])
        if parsed is not None:
            principal = parsed
            principal_index = i
            break

    if principal is None or principal_index is None or principal_index == 0:
        return None

    lender = " ".join(tokens[:principal_index]).strip() or None
    if not lender:
        return None

    remaining = tokens[principal_index + 1 :]
    interest = None
    due = None
    for token in remaining:
        token_lower = token.lower().replace("%", "")
        if re.fullmatch(r"\d+(?:\.\d{1,2})?", token_lower):
            if interest is None:
                interest = float(token_lower)
        elif re.fullmatch(r"\d{4}-\d{2}-\d{2}", token):
            due = token

    return {
        "lender_name": lender,
        "principal": principal,
        "interest_rate": interest,
        "next_due_date": due,
    }


def _parse_stock_text(text: str) -> Optional[Dict[str, object]]:
    tokens = text.strip().split()
    if len(tokens) < 2:
        return None
    sku = tokens[0].strip()
    qty = _parse_money(tokens[1])
    if qty is None:
        return None
    reason = " ".join(tokens[2:]).strip() or "manual"
    return {"sku_name": sku, "qty_delta": qty, "reason": reason}


async def on_cash(update: "Update", context: "ContextTypes.DEFAULT_TYPE") -> None:
    _log_inbound(update, "COMMAND /cash")
    if not await _ensure_ready(update):
        return

    raw_text = (update.effective_message.text or "").strip()
    parts = raw_text.split(maxsplit=1)
    if len(parts) < 2:
        context.user_data["next_text_mode"] = "cash"
        await _reply_with_log(
            update,
            "Send current cash amount next, e.g. `1450.00`.\n"
            "I’ll create a draft and wait for your confirmation before writing it.",
            source="cash_wait_amount",
            reply_markup=_command_keyboard(),
        )
        return

    amount = _parse_money(parts[1])
    if amount is None:
        await _reply_with_log(
            update,
            "I couldn’t read that cash amount. Send just a number, e.g. `/cash 1450`.",
            source="cash_invalid_amount",
            reply_markup=_command_keyboard(),
        )
        return

    draft_id = f"cash-{len(_get_drafts(context)) + 1:04d}"
    context.user_data["active_draft_id"] = draft_id
    _get_drafts(context)[draft_id] = {
        "chat_id": str(update.effective_chat.id),
        "source": "cash",
        "status": "pending",
        "cash_amount": amount,
        "raw": str(amount),
    }
    await _reply_with_log(
        update,
        _format_cash_draft(draft_id, amount),
        source="cash_draft_created",
        reply_markup=_build_confirm_markup(draft_id),
    )
    await _send_shortcuts(update)


async def on_loan(update: "Update", context: "ContextTypes.DEFAULT_TYPE") -> None:
    _log_inbound(update, "COMMAND /loan")
    if not await _ensure_ready(update):
        return

    raw_text = (update.effective_message.text or "").strip()
    parts = raw_text.split(maxsplit=1)
    if len(parts) < 2:
        context.user_data["next_text_mode"] = "loan"
        await _reply_with_log(
            update,
            "Send: lender name then amount.\n"
            "Example: `Ate Nena 1500` or `/loan Ato 1500`.\n"
            "I’ll ask for confirm before saving.",
            source="loan_wait_input",
            reply_markup=_command_keyboard(),
        )
        return

    parsed = _parse_loan_text(parts[1])
    if parsed is None:
        context.user_data["next_text_mode"] = "loan"
        await _reply_with_log(
            update,
            "I couldn’t parse that loan entry. Try `Ate Nena 1500`.",
            source="loan_parse_failed",
            reply_markup=_command_keyboard(),
        )
        return

    draft_id = f"loan-{len(_get_drafts(context)) + 1:04d}"
    context.user_data["active_draft_id"] = draft_id
    _get_drafts(context)[draft_id] = {
        "chat_id": str(update.effective_chat.id),
        "source": "loan",
        "status": "pending",
        "loan": parsed,
        "raw": str(parsed),
    }
    await _reply_with_log(
        update,
        _format_loan_draft(
            draft_id,
            parsed["lender_name"],
            float(parsed["principal"]),
            parsed.get("interest_rate"),  # type: ignore[index]
            parsed.get("next_due_date"),  # type: ignore[index]
        ),
        source="loan_draft_created",
        reply_markup=_build_confirm_markup(draft_id),
    )
    await _send_shortcuts(update)


async def on_stock(update: "Update", context: "ContextTypes.DEFAULT_TYPE") -> None:
    _log_inbound(update, "COMMAND /stock")
    if not await _ensure_ready(update):
        return

    raw_text = (update.effective_message.text or "").strip()
    parts = raw_text.split(maxsplit=1)
    if len(parts) < 2:
        context.user_data["next_text_mode"] = "stock"
        await _reply_with_log(
            update,
            "Send: item quantity.\n"
            "Use a positive number to add / a negative number to remove.\n"
            "Example: `/stock margarine 20`.",
            source="stock_wait_input",
            reply_markup=_command_keyboard(),
        )
        return

    parsed = _parse_stock_text(parts[1])
    if parsed is None:
        context.user_data["next_text_mode"] = "stock"
        await _reply_with_log(
            update,
            "I couldn’t parse that stock input. Try `/stock margarine 20` or `/stock margarine -5`.",
            source="stock_parse_failed",
            reply_markup=_command_keyboard(),
        )
        return

    draft_id = f"stock-{len(_get_drafts(context)) + 1:04d}"
    context.user_data["active_draft_id"] = draft_id
    _get_drafts(context)[draft_id] = {
        "chat_id": str(update.effective_chat.id),
        "source": "stock",
        "status": "pending",
        "stock": parsed,
        "raw": str(parsed),
    }
    await _reply_with_log(
        update,
        _format_stock_draft(
            draft_id,
            str(parsed["sku_name"]),
            float(parsed["qty_delta"]),
            str(parsed["reason"]),
        ),
        source="stock_draft_created",
        reply_markup=_build_confirm_markup(draft_id),
    )
    await _send_shortcuts(update)


async def on_text(update: "Update", context: "ContextTypes.DEFAULT_TYPE") -> None:
    _log_inbound(update, "TEXT")
    if not await _ensure_ready(update):
        return

    mode = context.user_data.pop("next_text_mode", None)
    text = (update.effective_message.text or "").strip()
    if mode == "cash":
        amount = _parse_money(text)
        if amount is None:
            context.user_data["next_text_mode"] = "cash"
            await _reply_with_log(
                update,
                "I couldn’t parse that cash amount. Send a number like `1450`.",
                source="cash_invalid_followup",
                reply_markup=_command_keyboard(),
            )
            return
        draft_id = f"cash-{len(_get_drafts(context)) + 1:04d}"
        context.user_data["active_draft_id"] = draft_id
        _get_drafts(context)[draft_id] = {
            "chat_id": str(update.effective_chat.id),
            "source": "cash",
            "status": "pending",
            "cash_amount": amount,
            "raw": text,
        }
        await _reply_with_log(
            update,
            _format_cash_draft(draft_id, amount),
            source="cash_draft_created",
            reply_markup=_build_confirm_markup(draft_id),
        )
        await _send_shortcuts(update)
        return

    if mode == "loan":
        parsed = _parse_loan_text(text)
        if parsed is None:
            context.user_data["next_text_mode"] = "loan"
            await _reply_with_log(
                update,
                "I couldn’t parse that loan input. Example: `Ate Nena 1500`.",
                source="loan_parse_failed_followup",
                reply_markup=_command_keyboard(),
            )
            return
        draft_id = f"loan-{len(_get_drafts(context)) + 1:04d}"
        context.user_data["active_draft_id"] = draft_id
        _get_drafts(context)[draft_id] = {
            "chat_id": str(update.effective_chat.id),
            "source": "loan",
            "status": "pending",
            "loan": parsed,
            "raw": text,
        }
        await _reply_with_log(
            update,
            _format_loan_draft(
                draft_id,
                str(parsed["lender_name"]),
                float(parsed["principal"]),
                parsed.get("interest_rate"),  # type: ignore[index]
                parsed.get("next_due_date"),  # type: ignore[index]
            ),
            source="loan_draft_created",
            reply_markup=_build_confirm_markup(draft_id),
        )
        await _send_shortcuts(update)
        return

    if mode == "stock":
        parsed = _parse_stock_text(text)
        if parsed is None:
            context.user_data["next_text_mode"] = "stock"
            await _reply_with_log(
                update,
                "I couldn’t parse stock input. Try `/stock item 10`.",
                source="stock_parse_failed_followup",
                reply_markup=_command_keyboard(),
            )
            return
        draft_id = f"stock-{len(_get_drafts(context)) + 1:04d}"
        context.user_data["active_draft_id"] = draft_id
        _get_drafts(context)[draft_id] = {
            "chat_id": str(update.effective_chat.id),
            "source": "stock",
            "status": "pending",
            "stock": parsed,
            "raw": text,
        }
        await _reply_with_log(
            update,
            _format_stock_draft(
                draft_id,
                str(parsed["sku_name"]),
                float(parsed["qty_delta"]),
                str(parsed["reason"]),
            ),
            source="stock_draft_created",
            reply_markup=_build_confirm_markup(draft_id),
        )
        await _send_shortcuts(update)
        return

    parsed = _parse_text_to_lines(text)
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
        draft_id = f"ledger-{len(_get_drafts(context)) + 1:04d}"
        context.user_data["active_draft_id"] = draft_id
        photo_info = _ledger_photo_info(update.effective_message)
        _append_ledger_ocr_log(
            {
                "event": "ledger_photo_received",
                "draft_id": draft_id,
                "chat_id": update.effective_chat.id if update.effective_chat else None,
                "user_id": (update.effective_user.id if update.effective_user else None),
                "photo": photo_info,
            }
        )

        try:
            ocr_text, ocr_error = await _extract_ledger_text(update.effective_message)
            _append_ledger_ocr_log(
                {
                    "event": "ledger_ocr_extract",
                    "draft_id": draft_id,
                    "chat_id": update.effective_chat.id if update.effective_chat else None,
                    "user_id": (update.effective_user.id if update.effective_user else None),
                    "text_length": len(ocr_text or ""),
                }
            )
            if not ocr_text:
                _append_ledger_ocr_log(
                    {
                        "event": "ledger_ocr_failed",
                        "draft_id": draft_id,
                        "chat_id": update.effective_chat.id if update.effective_chat else None,
                        "user_id": (update.effective_user.id if update.effective_user else None),
                        "reason": "no_ocr_text",
                        "error": ocr_error,
                        "raw_ocr_text": None,
                    }
                )
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
                    "I couldn’t run OCR on this photo yet. Send a clearer picture if this repeats.",
                    source="ledger_photo_received",
                )
                await _send_shortcuts(update)
                return

            parsed = parse_ledger_ocr_text(ocr_text)
            payload_for_log = _build_ledger_payload(
                draft_id,
                customer_name=str(parsed.get("customer_name") or "Unknown"),
                parsed=parsed,
                ocr_text=ocr_text,
                chat_id=update.effective_chat.id,
            )
            _append_ledger_ocr_log(
                {
                    "event": "ledger_ocr_parsed",
                    "draft_id": draft_id,
                    "chat_id": update.effective_chat.id if update.effective_chat else None,
                    "user_id": (update.effective_user.id if update.effective_user else None),
                    "rows": len(parsed.get("entries") or []),
                    "warnings": list(parsed.get("warnings", [])),
                    "raw_ocr_text": ocr_text,
                    "payload_json": payload_for_log,
                }
            )
            entries = list(parsed.get("entries", []))
            customer_name = str(parsed.get("customer_name") or "Unknown")
            _get_drafts(context)[draft_id] = {
                "chat_id": str(update.effective_chat.id),
                "source": "ledger",
                "raw": ocr_text,
                "status": "pending",
                "ocr_mode": "ledger",
                "customer_name": customer_name,
                "parsed": parsed,
                "lines": entries,
                "payload_json": payload_for_log,
            }
            await _reply_with_log(
                update,
                (
                    "📄 Parsed ledger payload (JSON):\n"
                    f"<pre>{escape(_json_snippet(payload_for_log))}</pre>\n"
                    "If this looks correct, press Confirm. If not, send /cancel and submit a new photo."
                ),
                source="ledger_json_review",
                parse_mode=ParseMode.HTML,
            )
            await _reply_with_log(
                update,
                format_ledger_draft(entries, draft_id=draft_id),
                source="ledger_photo_received",
                reply_markup=InlineKeyboardMarkup(
                    [[
                        InlineKeyboardButton("✅ Confirm", callback_data=f"confirm:{draft_id}"),
                        InlineKeyboardButton("❌ Cancel", callback_data=f"cancel:{draft_id}"),
                    ]]
                ),
            )
            await _send_shortcuts(update)
        except Exception as exc:  # pragma: no cover - runtime guard
            logger.exception("Ledger OCR workflow failed for %s", draft_id)
            _append_ledger_ocr_log(
                {
                    "event": "ledger_ocr_handler_error",
                    "draft_id": draft_id,
                    "chat_id": update.effective_chat.id if update.effective_chat else None,
                    "user_id": (update.effective_user.id if update.effective_user else None),
                    "error": str(exc),
                }
            )
            await _reply_with_log(
                update,
                "⚠️ I hit a parsing/network error while processing this ledger photo.\n"
                "Please send the photo again or try a clearer image after /ledger.\n"
                "If this persists, send /cancel and try again in a few seconds.",
                source="ledger_photo_error",
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
        "For now, please use this draft as a placeholder and send a corrected message to create a new one.\n"
        "Is this what you meant?",
        source="photo_draft",
        reply_markup=InlineKeyboardMarkup(
            [[
                InlineKeyboardButton("✅ Confirm", callback_data=f"confirm:{draft_id}"),
                InlineKeyboardButton("❌ Cancel", callback_data=f"cancel:{draft_id}"),
            ]]
        ),
    )
    await _send_shortcuts(update)


async def _extract_ledger_text(message) -> tuple[Optional[str], Optional[str]]:
    if message is None:
        return None, "missing_message"

    photo = message.photo[-1] if message.photo else None
    if not photo:
        return None, "missing_photo"

    temp_path: Optional[str] = None
    try:
        file_obj = await photo.get_file()
        suffix = Path(photo.file_unique_id).suffix or ".jpg"
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as handle:
            temp_path = handle.name
        await file_obj.download_to_drive(custom_path=temp_path)
        return extract_text_from_image(temp_path), None
    except Exception as exc:
        logger.warning("Ledger OCR extraction failed: %s", exc)
        return None, str(exc)
    finally:
        if temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except Exception:
                logger.debug("Failed to clean temporary OCR file %s", temp_path, exc_info=True)


def _persist_ledger_draft(context: "ContextTypes.DEFAULT_TYPE", draft_id: str, draft: Dict) -> Dict[str, object]:
    if draft.get("ledger_recorded"):
        return {"status": "already_saved", "entries_added": 0}

    if draft.get("source") != "ledger":
        return {"status": "not_ledger", "entries_added": 0}

    parsed = draft.get("parsed")
    if not isinstance(parsed, dict):
        return {"status": "nothing_to_save", "entries_added": 0}

    entries = parsed.get("entries") or []
    if not entries:
        return {"status": "nothing_to_save", "entries_added": 0}

    customer_name = str(parsed.get("customer_name") or draft.get("customer_name") or "Unknown")
    store = _get_business_state(context)
    result = store.upsert_customer_ledger(
        customer_name=customer_name,
        rows=entries,
        source="telegram_photo_ledger",
        source_id=draft_id,
        draft_payload=draft.get("payload_json"),
    )
    draft["ledger_recorded"] = True
    draft["save_result"] = result
    return {
        "status": "saved",
        "entries_added": result.get("entries_added", 0),
        "entries_total": result.get("entries_total", 0),
    }


def _persist_cash_draft(context: "ContextTypes.DEFAULT_TYPE", draft_id: str, draft: Dict) -> Dict[str, object]:
    if draft.get("cash_recorded"):
        return {"status": "already_saved"}
    if draft.get("source") != "cash":
        return {"status": "not_cash"}

    amount = draft.get("cash_amount")
    if amount is None:
        return {"status": "nothing_to_save"}

    try:
        snapshot = _get_business_state(context).add_cash_snapshot(
            float(amount),
            source="telegram_command_cash",
            source_id=draft_id,
            note="Manual cash check-in",
        )
    except Exception as exc:
        return {"status": "error", "error": str(exc)}

    draft["cash_recorded"] = True
    draft["cash_snapshot"] = snapshot
    return {"status": "saved", "cash_amount": snapshot.get("cash_amount", 0.0)}


def _persist_loan_draft(context: "ContextTypes.DEFAULT_TYPE", draft_id: str, draft: Dict) -> Dict[str, object]:
    if draft.get("loan_recorded"):
        return {"status": "already_saved"}
    if draft.get("source") != "loan":
        return {"status": "not_loan"}

    payload = draft.get("loan")
    if not isinstance(payload, dict):
        return {"status": "nothing_to_save"}

    lender = str(payload.get("lender_name") or "").strip()
    principal = payload.get("principal")
    if not lender or principal is None:
        return {"status": "nothing_to_save"}

    try:
        loan = _get_business_state(context).add_loan(
            lender,
            principal=float(principal),
            interest_rate=payload.get("interest_rate"),
            next_due_date=payload.get("next_due_date"),
            source="telegram_command_loan",
            source_id=draft_id,
        )
    except Exception as exc:
        return {"status": "error", "error": str(exc)}

    draft["loan_recorded"] = True
    draft["loan_record"] = loan
    return {"status": "saved", "loan_id": loan.get("loan_id")}


def _persist_stock_draft(context: "ContextTypes.DEFAULT_TYPE", draft_id: str, draft: Dict) -> Dict[str, object]:
    if draft.get("stock_recorded"):
        return {"status": "already_saved"}
    if draft.get("source") != "stock":
        return {"status": "not_stock"}

    payload = draft.get("stock")
    if not isinstance(payload, dict):
        return {"status": "nothing_to_save"}

    sku = str(payload.get("sku_name") or "").strip()
    qty_delta = payload.get("qty_delta")
    reason = str(payload.get("reason") or "manual").strip()
    if not sku or qty_delta is None:
        return {"status": "nothing_to_save"}

    try:
        result = _get_business_state(context).adjust_inventory(
            sku,
            qty_delta=float(qty_delta),
            reason=reason,
            source="telegram_command_stock",
            source_id=draft_id,
        )
    except Exception as exc:
        return {"status": "error", "error": str(exc)}

    draft["stock_recorded"] = True
    draft["stock_record"] = result
    return {"status": "saved", "product": result.get("sku_id")}

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
        if draft.get("source") == "ledger":
            result = _persist_ledger_draft(context, draft_id, draft)
            _append_ledger_ocr_log(
                {
                    "event": "ledger_draft_confirm",
                    "draft_id": draft_id,
                    "chat_id": update.effective_chat.id if update.effective_chat else None,
                    "user_id": query.from_user.id if query and query.from_user else None,
                    "status": result.get("status"),
                    "entries_added": result.get("entries_added", 0),
                    "entries_total": result.get("entries_total", 0),
                    "payload_json": draft.get("payload_json"),
                }
            )
            if result["status"] == "saved":
                msg = (
                    f"✅ Draft #{draft_id} confirmed and saved to ledger."
                    f" {result.get('entries_added')} row(s) added."
                )
            elif result["status"] == "already_saved":
                msg = (
                    f"✅ Draft #{draft_id} was already confirmed."
                )
            elif result["status"] == "nothing_to_save":
                msg = f"⚠️ Draft #{draft_id} has no parsed rows to save."
            else:
                msg = f"✅ Draft #{draft_id} confirmed."
            await _edit_with_log(
                update,
                source="callback_confirm",
                text=msg,
            )
        elif draft.get("source") == "cash":
            result = _persist_cash_draft(context, draft_id, draft)
            if result["status"] == "saved":
                msg = (
                    f"✅ Draft #{draft_id} confirmed. Cash snapshot saved: "
                    f"PHP {result.get('cash_amount'):.2f}"
                )
            elif result["status"] == "already_saved":
                msg = f"✅ Draft #{draft_id} was already confirmed."
            elif result["status"] == "nothing_to_save":
                msg = f"⚠️ Draft #{draft_id} is missing a cash amount."
            else:
                msg = f"⚠️ Draft #{draft_id} could not be saved."
            await _edit_with_log(
                update,
                source="callback_confirm",
                text=msg,
            )
        elif draft.get("source") == "loan":
            result = _persist_loan_draft(context, draft_id, draft)
            if result["status"] == "saved":
                msg = (
                    f"✅ Draft #{draft_id} confirmed. Loan saved: "
                    f"{result.get('loan_id')}"
                )
            elif result["status"] == "already_saved":
                msg = f"✅ Draft #{draft_id} was already confirmed."
            else:
                msg = f"⚠️ Draft #{draft_id} could not be saved."
            await _edit_with_log(
                update,
                source="callback_confirm",
                text=msg,
            )
        elif draft.get("source") == "stock":
            result = _persist_stock_draft(context, draft_id, draft)
            if result["status"] == "saved":
                msg = f"✅ Draft #{draft_id} confirmed. Stock updated."
            elif result["status"] == "already_saved":
                msg = f"✅ Draft #{draft_id} was already confirmed."
            else:
                msg = f"⚠️ Draft #{draft_id} could not be saved."
            await _edit_with_log(
                update,
                source="callback_confirm",
                text=msg,
            )
        else:
            _add_transaction(context, draft_id, draft)
            await _edit_with_log(
                update,
                source="callback_confirm",
                text=f"✅ Draft #{draft_id} confirmed.",
            )
    elif action == "cancel":
        draft["status"] = "cancelled"
        if draft.get("source") == "ledger":
            _append_ledger_ocr_log(
                {
                    "event": "ledger_draft_cancelled",
                    "draft_id": draft_id,
                    "chat_id": update.effective_chat.id if update.effective_chat else None,
                    "user_id": query.from_user.id if query and query.from_user else None,
                    "payload_json": draft.get("payload_json"),
                    "reason": "user_cancelled",
                }
            )
        await _edit_with_log(
            update,
            source="callback_cancel",
            text=f"❌ Draft #{draft_id} cancelled.",
        )
    else:
        await _edit_with_log(
            update,
            source="callback_unsupported_action",
            text=(
                "This action is not available anymore. "
                "Use Confirm or Cancel for the current draft."
            ),
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
        "Use `/cash` or `/loan` for quick balance/borrowed cash checks.\n"
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

    text_mode = context.user_data.pop("next_text_mode", None)
    if text_mode:
        cancelled_items.append(f"mode_text:{text_mode}")

    drafts = _get_drafts(context)
    if draft_id and draft_id in drafts:
        draft_status = drafts[draft_id].get("status")
        if draft_status in {"pending", "pending_ocr"}:
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

    state = _get_business_state(context)
    command = (update.effective_message.text or "").strip().split(maxsplit=1)[0].lower()
    mode = "overview"
    if command.startswith("/insight_"):
        mode = _resolve_insight_mode(command[len("/insight_") :])
    elif command == "/insights" and context.args:
        mode = _resolve_insight_mode(context.args[0])

    metrics = _collect_insight_metrics(state)
    builders = {
        "overview": _build_overview_lines,
        "opening": _build_opening_lines,
        "midday": _build_midday_lines,
        "visit": _build_visit_lines,
        "supplier": _build_supplier_lines,
        "close": _build_close_lines,
        "due": _build_due_lines,
        "weekly": _build_weekly_lines,
    }
    builder = builders.get(mode, _build_overview_lines)
    lines = builder(metrics)

    if mode == "overview":
        lines.append("")
        lines.append("Need a specific moment check?")
        lines.append("Use one of: /insight_open, /insight_midday, /insight_visit, /insight_supplier, /insight_close, /insight_due, /insight_week")

    await _reply_with_log(
        update,
        "\n".join(lines),
        source=f"insights_report:{mode}",
        reply_markup=_command_keyboard(),
    )


async def on_debtors(update: "Update", context: "ContextTypes.DEFAULT_TYPE") -> None:
    _log_inbound(update, "COMMAND /debtors")
    if not await _ensure_ready(update):
        return

    state = _get_business_state(context)
    debtors = state.get_open_debtors()
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
        name = str(row.get("customer_name", "Unknown"))
        amount = float(row.get("current_balance", 0.0) or 0.0)
        since = row.get("last_updated_utc", "unknown date")
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
    for sale in _get_business_state(context).load().get("sales", []):
        if not isinstance(sale, dict):
            continue
        transactions.append(
            {
                "draft_id": sale.get("sale_id", "unknown"),
                "source": sale.get("source", "sales_state"),
                "status": "confirmed",
                "total": sale.get("total", 0.0),
                "when_utc": sale.get("recorded_utc", ""),
            }
        )

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
        "• /cash — record cash snapshot (confirm before save)\n"
        "• /loan — record shop debt from lender (confirm before save)\n"
        "• /stock — adjust stock with delta (confirm before save)\n"
        "• /cancel — stop any in-progress action\n"
        "• /insights — sales and stock summary\n"
        "• /insight_open — start-day health and readiness\n"
        "• /insight_midday — midday stock and sales checkpoint\n"
        "• /insight_visit — wholesaler-trip prep signal\n"
        "• /insight_supplier — supplier offer response signal\n"
        "• /insight_close — end-of-day ledger and collections signal\n"
        "• /insight_due — loan repayment pressure signal\n"
        "• /insight_week — weekly trend signal\n"
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
    if update and getattr(update, "effective_chat", None):
        try:
            await update.effective_chat.send_message(
                "⚠️ I hit an unexpected issue. Please type /start to reset and retry."
            )
        except Exception:
            logger.debug("Failed to send error message to chat.", exc_info=True)


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
    logger.info("Registering bot handlers: /start, /ledger, /cash, /loan, /stock, /help, fallback command handler")
    logger.info("Conversation logs: %s", _conversation_log_path())
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("ledger", on_ledger))
    application.add_handler(CommandHandler("cash", on_cash))
    application.add_handler(CommandHandler("loan", on_loan))
    application.add_handler(CommandHandler("stock", on_stock))
    application.add_handler(CommandHandler("cancel", on_cancel))
    application.add_handler(CommandHandler("insights", on_insights))
    application.add_handler(CommandHandler("insight_open", on_insights))
    application.add_handler(CommandHandler("insight_midday", on_insights))
    application.add_handler(CommandHandler("insight_visit", on_insights))
    application.add_handler(CommandHandler("insight_supplier", on_insights))
    application.add_handler(CommandHandler("insight_close", on_insights))
    application.add_handler(CommandHandler("insight_due", on_insights))
    application.add_handler(CommandHandler("insight_week", on_insights))
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
