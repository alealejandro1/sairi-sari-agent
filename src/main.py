"""Telegram bot entrypoint for the Telegram intake track."""

from __future__ import annotations

import os
import logging
import re
import argparse
import json
from datetime import datetime, timedelta, timezone
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


def _effective_now() -> datetime:
    raw = os.getenv("SARI_AS_OF_UTC", "").strip()
    if not raw:
        return datetime.now(timezone.utc)

    normalized = raw.strip().lower().replace("-", "").replace("_", "").replace(" ", "")
    if normalized in {"aprilfirst", "april1", "4/1", "401", "april01", "april1st"}:
        today = datetime.now(timezone.utc)
        return datetime(today.year, 4, 1, tzinfo=timezone.utc)

    for parser in ("%Y-%m-%d", "%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S"):
        try:
            parsed = datetime.strptime(raw, parser)
            return parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed
    except ValueError:
        logger.debug("Invalid SARI_AS_OF_UTC value %r, falling back to system UTC.", raw)
        return datetime.now(timezone.utc)

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


def _is_unknown_customer_name(customer_name: str | None) -> bool:
    normalized = str(customer_name or "").strip().lower()
    return normalized in {"", "unknown"}


LEDGER_RESET_CONFIRMATION = "WIPE ALL LEDGER DATA"

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


async def _edit_with_log(update: "Update", *, source: str, text: str, **kwargs) -> None:
    query = update.callback_query
    await query.edit_message_text(text, **kwargs)
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


def _get_business_state(context: "ContextTypes.DEFAULT_TYPE") -> BusinessStateStore:
    store = context.application.bot_data.get("business_state_store")
    if isinstance(store, BusinessStateStore):
        return store
    store = BusinessStateStore(_business_state_store_path())
    context.application.bot_data["business_state_store"] = store
    return store


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


def _parse_cash_amount(value: Any) -> float | None:
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    sanitized = (
        raw.replace(",", "")
        .replace("₱", "")
        .replace("PHP", "")
        .replace("php", "")
        .replace("P", "")
    )
    sanitized = re.sub(r"\s+", "", sanitized)
    match = re.search(r"-?\d+(?:\.\d{1,2})?", sanitized)
    if not match:
        return None
    return _safe_float(match.group(0), default=0.0)


def _money(value: float) -> str:
    return f"PHP {value:,.2f}"


def _format_projection_date(value: datetime | None) -> str:
    if value is None:
        return "n/a"
    return value.strftime("%b %d")


def _collect_insight_metrics(state: BusinessStateStore) -> Dict[str, Any]:
    state.purge_unknown_customers()

    def _debt_age_signal(days_carried: int | None) -> Dict[str, Any]:
        if days_carried is None:
            return {
                "emoji": "🧭",
                "bucket": "no-activity",
                "label": "No recent activity",
                "age_score": 2,
            }
        if days_carried <= 3:
            return {
                "emoji": "✅",
                "bucket": "0-3",
                "label": "Fresh",
                "age_score": 0,
            }
        if days_carried <= 8:
            return {
                "emoji": "🟢",
                "bucket": "4-8",
                "label": "Stable",
                "age_score": 1,
            }
        if days_carried <= 10:
            return {
                "emoji": "⚠️",
                "bucket": "9-10",
                "label": "Aging",
                "age_score": 2,
            }
        return {
            "emoji": "🚨",
            "bucket": "11+",
            "label": "Long carried",
            "age_score": 3,
        }

    def _debt_amount_signal(current_balance: float) -> Dict[str, Any]:
        if current_balance <= 400:
            return {
                "emoji": "✅",
                "bucket": "Low",
                "label": "Low debt",
                "debt_score": 0,
            }
        if current_balance <= 1000:
            return {
                "emoji": "🟢",
                "bucket": "Medium",
                "label": "Watch debt",
                "debt_score": 1,
            }
        if current_balance <= 2000:
            return {
                "emoji": "⚠️",
                "bucket": "High",
                "label": "High debt",
                "debt_score": 2,
            }
        return {
            "emoji": "🚨",
            "bucket": "Critical",
            "label": "Critical debt",
            "debt_score": 3,
        }

    def _resolve_risk_profile(current_balance: float, days_carried: int | None) -> Dict[str, Any]:
        debt_signal = _debt_amount_signal(current_balance)
        age_signal = _debt_age_signal(days_carried)
        risk_score = debt_signal["debt_score"] + age_signal["age_score"]

        if risk_score <= 1:
            return {
                "emoji": "✅",
                "tier": "safe",
                "label": "Good",
                "risk_score": risk_score,
                "debt_signal": debt_signal,
                "age_signal": age_signal,
            }
        if risk_score <= 3:
            return {
                "emoji": "🟢",
                "tier": "watch",
                "label": "Monitor",
                "risk_score": risk_score,
                "debt_signal": debt_signal,
                "age_signal": age_signal,
            }
        if risk_score <= 5:
            return {
                "emoji": "🚨",
                "tier": "risk",
                "label": "Risk",
                "risk_score": risk_score,
                "debt_signal": debt_signal,
                "age_signal": age_signal,
            }
        return {
            "emoji": "🆘",
            "tier": "critical",
            "label": "Critical",
            "risk_score": risk_score,
            "debt_signal": debt_signal,
            "age_signal": age_signal,
        }

    def _collect_customer_hutang_profiles() -> List[Dict[str, Any]]:
        profiles: List[Dict[str, Any]] = []
        customers = state.load().get("customers", {})
        for key, raw_customer in customers.items():
            if not isinstance(raw_customer, dict):
                continue

            name = str(raw_customer.get("customer_name", "Unknown"))
            if _is_unknown_customer_name(name):
                continue
            current_balance = _safe_float(raw_customer.get("current_balance"), default=0.0)
            entries = raw_customer.get("entries", [])
            if not isinstance(entries, list):
                entries = []

            total_consumed = 0.0
            total_paid = 0.0
            last_activity: datetime | None = None
            last_payment: datetime | None = None
            last_note = ""
            payment_dates: List[datetime] = []

            for raw_entry in entries:
                if not isinstance(raw_entry, dict):
                    continue
                amount = _safe_float(raw_entry.get("amount"), default=0.0)
                if amount > 0:
                    total_consumed += amount
                elif amount < 0:
                    total_paid += abs(amount)

                entry_date = _parse_iso_datetime(str(raw_entry.get("date")))
                if entry_date and (last_activity is None or entry_date > last_activity):
                    last_activity = entry_date
                    last_note = str(raw_entry.get("note", "") or "")

                is_payment = (
                    str(raw_entry.get("entry_kind", "")).lower() == "payment"
                    or amount < 0
                )
                if is_payment and entry_date:
                    payment_dates.append(entry_date)
                    if last_payment is None or entry_date > last_payment:
                        last_payment = entry_date

            days_since_activity = _days_since(last_activity, now)
            days_since_payment = _days_since(last_payment, now)
            days_carried = days_since_payment if last_payment is not None else days_since_activity
            payment_dates.sort()
            if len(payment_dates) >= 2:
                gaps = []
                for current, previous in zip(payment_dates[1:], payment_dates[:-1]):
                    gap = max(0, int((current - previous).total_seconds() // 86400))
                    if gap > 0:
                        gaps.append(gap)
                avg_repayment_days: Optional[int] = int(sum(gaps) / len(gaps)) if gaps else None
            else:
                avg_repayment_days = None
            last_payment_text = last_payment.isoformat().replace("T", " ")[:16] if last_payment else "n/a"
            risk_profile = _resolve_risk_profile(current_balance, days_carried)

            profiles.append(
                {
                    "customer_key": key,
                    "customer_name": name,
                    "outstanding": round(current_balance, 2),
                    "total_consumed": round(total_consumed, 2),
                    "total_paid": round(total_paid, 2),
                    "last_activity": last_activity.isoformat() if last_activity else None,
                    "days_since_activity": days_since_activity,
                    "days_since_payment": days_since_payment,
                    "days_carried": days_carried,
                    "days_bucket": risk_profile["age_signal"]["bucket"],
                    "risk_tier": risk_profile["tier"],
                    "risk_label": risk_profile["label"],
                    "risk_emoji": risk_profile["emoji"],
                    "risk_score": risk_profile["risk_score"],
                    "debt_bucket": risk_profile["debt_signal"]["bucket"],
                    "debt_emoji": risk_profile["debt_signal"]["emoji"],
                    "debt_label": risk_profile["debt_signal"]["label"],
                    "age_emoji": risk_profile["age_signal"]["emoji"],
                    "age_label": risk_profile["age_signal"]["label"],
                    "avg_repayment_days": avg_repayment_days,
                    "total_spent": round(total_consumed, 2),
                    "payment_count": len(payment_dates),
                    "last_payment_text": last_payment_text,
                    "last_note": last_note[:80],
                }
            )
        profiles.sort(
            key=lambda item: (
                item.get("days_carried") or -1,
                item.get("risk_score", 0),
                item.get("outstanding", 0.0),
            ),
            reverse=True,
        )
        return profiles

    def _days_since(date_value: datetime | None, now: datetime) -> int | None:
        if date_value is None:
            return None
        return max(0, int((now - date_value).total_seconds() // 86400))

    now = _effective_now()
    customer_profiles = _collect_customer_hutang_profiles()
    total_utang = state.total_open_utang()
    open_profiles = [row for row in customer_profiles if row["outstanding"] > 0.009]

    risk_buckets = {"critical": 0, "risk": 0, "watch": 0, "safe": 0}
    total_consumed = 0.0
    total_paid = 0.0
    top_share = 0.0
    total_concentration = 0.0

    for row in customer_profiles:
        total_consumed += _safe_float(row.get("total_consumed"))
        total_paid += _safe_float(row.get("total_paid"))
        row["open_share"] = 0.0

        if total_utang > 0 and row["outstanding"] > 0.009:
            share = _safe_float(row.get("outstanding"), default=0.0) / total_utang
            row["open_share"] = share
            total_concentration += share
            top_share = max(top_share, share)

    for row in open_profiles:
        risk_buckets[row["risk_tier"]] += 1

    return {
        "now": now,
        "customer_profiles": customer_profiles,
        "open_profiles": open_profiles,
        "open_count": len(open_profiles),
        "profile_count": len(customer_profiles),
        "total_utang": round(total_utang, 2),
        "total_consumed": round(total_consumed, 2),
        "total_paid": round(total_paid, 2),
        "risk_buckets": risk_buckets,
        "top_outstanding": open_profiles[:5],
        "top_share": round(top_share, 4),
        "total_concentration": round(total_concentration, 4),
    }


def _days_since_text(days: int | None, *, suffix: str = "day(s)") -> str:
    if days is None:
        return f"unknown {suffix}"
    if days == 1:
        return "1 day"
    return f"{days} {suffix}"


def _build_repayment_request_message(profile: Dict[str, Any]) -> str:
    name = str(profile.get("customer_name", "Customer"))
    outstanding = _safe_float(profile.get("outstanding"), default=0.0)
    carried_days = profile.get("days_carried")
    if carried_days is None:
        carried_days = profile.get("days_since_activity")
    carried_text = _days_since_text(carried_days)
    avg_repayment_days = profile.get("avg_repayment_days")
    cadence_text = (
        f" Your usual repayment pace has been every {avg_repayment_days} day(s)."
        if isinstance(avg_repayment_days, int) and avg_repayment_days > 0
        else ""
    )
    last_payment_days = profile.get("days_since_payment")
    payment_gap_text = ""
    if isinstance(last_payment_days, int) and last_payment_days > 0:
        payment_gap_text = f" Last payment was {last_payment_days} day(s) ago."
    return (
        f"Hi {name},\n"
        "Friendly reminder for your utang settlement.\n\n"
        f"Outstanding balance: {_money(outstanding)}\n"
        f"This debt has been carried for {carried_text}.{payment_gap_text}{cadence_text}\n\n"
        "Please reply and let me know when you will be able to pay. Thank you."
    )


def _build_debtor_chase_markup(
    profiles: List[Dict[str, Any]],
) -> "InlineKeyboardMarkup":
    buttons: List[List[Any]] = []
    for row in profiles:
        if str(row.get("customer_name", "")).strip() == "":
            continue
        name = str(row.get("customer_name"))
        key = str(row.get("customer_key", ""))
        if not key:
            continue
        buttons.append(
            [InlineKeyboardButton(f"📩 {name}", callback_data=f"chase:{key}")]
        )
    if not buttons:
        return InlineKeyboardMarkup([])
    return InlineKeyboardMarkup(buttons)


def _build_overview_lines(metrics: Dict[str, Any]) -> List[str]:
    profiles = metrics["customer_profiles"]
    open_profiles = metrics["open_profiles"]
    open_count = metrics["open_count"]
    profile_count = metrics["profile_count"]
    total_utang = metrics["total_utang"]
    avg_outstanding = total_utang / open_count if open_count else 0.0

    lines = [
        "📒 Utang Ledger Snapshot",
        "",
        f"─────────────────────────────",
        "",
        f"👥 People recorded: {profile_count}",
        f"🧾 Open debtors: {open_count}",
        f"💳 Outstanding debt: {_money(total_utang)}",
        f"📈 Concentration: {metrics['top_share']*100:.0f}% held by largest debtor",
        f"📊 Avg open amount per debtor: {_money(avg_outstanding)}",
        "",
        "🟩 Risk buckets by debt carried",
    ]
    bucket_lines = [
        ("✅ Safe", "safe"),
        ("🟢 Watch", "watch"),
        ("🚨 Risk", "risk"),
        ("🆘 Critical", "critical"),
    ]
    for label, key in bucket_lines:
        count = metrics["risk_buckets"].get(key, 0)
        lines.append(f"• {label}: {count} people")

    if open_count:
        lines.append("")
        lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        lines.append("🏆 TOP DEBTORS (PERSON-LEVEL)")
        for idx, row in enumerate(open_profiles[:5], start=1):
            days = row.get("days_carried")
            days_text = f"{days}d" if days is not None else "n/a"
            lines.append(
                f"{idx}) {row['risk_emoji']} {row['customer_name']} "
                f"({row['debt_emoji']} {row['debt_bucket']})"
            )
            lines.append(
                f"   • Outstanding: {_money(_safe_float(row['outstanding']))} · "
                f"carried {days_text}"
            )
            lines.append(
                f"   • Last payment: {row.get('last_payment_text', 'n/a')} · "
                f"activity {row['age_emoji']} {row['age_label']}"
            )
    else:
        lines.append("")
        lines.append("No open utang currently.")

    top_share = metrics["top_share"]
    if top_share >= 0.4:
        lines.append("")
        lines.append("⚠️ Concentration risk: one customer has >40% of open utang.")

    risky_rows = [row for row in open_profiles if row["risk_tier"] in {"risk", "critical"}]
    lines.append("")
    lines.append("⚠️  ACTION PRIORITIES")
    if not risky_rows:
        lines.append("• No critical/high-risk customers currently.")
    else:
        for row in risky_rows[:5]:
            days = row.get("days_carried")
            days_text = f"{days}d" if days is not None else "n/a"
            lines.append(
                f"• {row['risk_emoji']} {row['customer_name']} · "
                f"{_money(_safe_float(row['outstanding']))} · carried {days_text}"
            )

    lines.append("")
    lines.append("🧩 QUICK ACTIONS")
    lines.append("1) /debtors for per-person detail + collections priority")
    lines.append("2) /cash to register cash-on-hand and see expected debt collections")
    lines.append("3) /ledger for an updated photo if balances changed")

    return lines


def _build_cash_outlook_lines(
    current_cash: float, metrics: Dict[str, Any], *, snapshot_time: str | None = None
) -> List[str]:
    open_profiles = [row for row in metrics["customer_profiles"] if row["outstanding"] > 0.009]
    total_utang = metrics["total_utang"]
    now = metrics["now"]

    lines: List[str] = ["💰 Cash vs Utang Focus"]
    lines.append("")
    lines.append(f"Cash now: {_money(current_cash)}")
    lines.append(f"Open utang: {_money(total_utang)}")
    if snapshot_time:
        lines.append(f"Snapshot time: {snapshot_time}")

    if not open_profiles:
        lines.append("")
        lines.append("No open utang currently. You can spend from cash-on-hand without an expected debt inflow delay.")
        return lines

    if total_utang <= 0:
        total_utang = 0.0
    coverage_ratio = current_cash / total_utang if total_utang else None

    projected_rows = []
    total_reasonable = 0.0
    weighted_days = 0.0
    weighted_amount = 0.0
    in_7d = 0.0
    in_14d = 0.0
    in_30d = 0.0

    for row in open_profiles:
        outstanding = _safe_float(row.get("outstanding"))
        customer = str(row.get("customer_name") or "Unknown")
        avg_days = row.get("avg_repayment_days")
        days_since_payment = row.get("days_since_payment")
        days_since_activity = row.get("days_since_activity")

        if isinstance(avg_days, int) and avg_days > 0:
            expected_days = max(1, avg_days)
            confidence = 0.7 if avg_days <= 7 else 0.55 if avg_days <= 14 else 0.35
            cadence_note = f"avg {avg_days}d"
        elif isinstance(days_since_payment, int) and days_since_payment > 0:
            expected_days = max(7, int(days_since_payment * 0.6))
            confidence = 0.35
            cadence_note = f"payment gap {days_since_payment}d"
        elif isinstance(days_since_activity, int) and days_since_activity > 0:
            expected_days = max(10, min(30, days_since_activity * 2))
            confidence = 0.25
            cadence_note = f"last activity {days_since_activity}d ago"
        else:
            expected_days = 14
            confidence = 0.2
            cadence_note = "insufficient history"

        expected_by = now + timedelta(days=expected_days)
        expected_amount = round(outstanding * confidence, 2)
        projected_rows.append(
            (expected_by, expected_days, expected_amount, customer, outstanding, cadence_note)
        )

        total_reasonable += expected_amount
        weighted_days += expected_days * expected_amount
        weighted_amount += expected_amount

        if expected_days <= 7:
            in_7d += expected_amount
        if expected_days <= 14:
            in_14d += expected_amount
        if expected_days <= 30:
            in_30d += expected_amount

    if weighted_amount > 0:
        expected_by_days = max(1, int(round(weighted_days / weighted_amount)))
    else:
        expected_by_days = 14
    expected_by = now + timedelta(days=expected_by_days)

    lines.append("")
    lines.append("Expected collections (reasonable confidence):")
    lines.append(f"- next 7d: {_money(round(in_7d, 2))}")
    lines.append(f"- next 14d: {_money(round(in_14d, 2))}")
    lines.append(f"- next 30d: {_money(round(in_30d, 2))}")
    lines.append("")
    lines.append(
        f"Reasonable expected collection: {_money(round(total_reasonable, 2))}"
        f" by around {_format_projection_date(expected_by)} ({expected_by_days}d)"
    )
    lines.append(
        f"If realized, projected cash by then: {_money(round(current_cash + total_reasonable, 2))}"
    )

    if coverage_ratio is not None:
        lines.append(f"Debt coverage ratio: {coverage_ratio*100:.0f}%")
    lines.append("")
    lines.append("Top expected collections:")
    projected_rows.sort(key=lambda item: item[1])
    for expected_by_dt, expected_days, expected_amount, customer, outstanding, cadence_note in projected_rows[:8]:
        lines.append(
            f"• {customer}: {_money(expected_amount)} likely by {expected_days}d "
            f"(expected by {_format_projection_date(expected_by_dt)}, {cadence_note}, out {_money(outstanding)})"
        )

    lines.append("")
    lines.append(
        "You’re currently stock-data blind, so I can’t recommend what to buy."
    )
    lines.append("Use this to choose a spend cap now vs. by the projected collection date.")

    return lines


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
        "Utang-ledger mode is enabled.\n"
        "Use this flow:\n"
        "1) `/ledger` then send the page photo.\n"
        "2) `/cash` to register your cash-on-hand for spending context.\n"
        "3) `/insights` for quick risk + person-level summary.\n"
        "4) `/debtors` for detailed per-person details.\n"
        "5) `/reset_ledger` to wipe all stored debt records (confirmation required).\n"
        "If you accidentally started this, send /cancel.",
        source="start_ack",
        reply_markup=_command_keyboard(),
    )


async def on_reset_ledger(update: "Update", context: "ContextTypes.DEFAULT_TYPE") -> None:
    _log_inbound(update, "COMMAND /reset_ledger")
    if not await _ensure_ready(update):
        return

    context.user_data["awaiting_ledger_reset_confirmation"] = True
    await _reply_with_log(
        update,
        "⚠️ Ledger reset is a destructive action.\n"
        "This will remove all stored debtor profiles and all historical ledger rows.\n"
        "To confirm, type exactly:\n"
        f"`{LEDGER_RESET_CONFIRMATION}`\n"
        "If you want to stop, send /cancel.",
        source="ledger_reset_prompt",
        reply_markup=_command_keyboard(),
    )


def _command_keyboard():
    return ReplyKeyboardMarkup(
        [
            [
                KeyboardButton("/start"),
                KeyboardButton("/ledger"),
                KeyboardButton("/cash"),
            ],
            [
                KeyboardButton("/insights"),
                KeyboardButton("/debtors"),
                KeyboardButton("/cancel"),
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
        "I’ll extract utang entries from the image.\n"
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


async def on_text(update: "Update", context: "ContextTypes.DEFAULT_TYPE") -> None:
    _log_inbound(update, "TEXT")
    if not await _ensure_ready(update):
        return

    if context.user_data.get("awaiting_ledger_reset_confirmation"):
        text = str((update.effective_message.text if update.effective_message else "") or "").strip()
        context.user_data.pop("awaiting_ledger_reset_confirmation", None)

        if text == LEDGER_RESET_CONFIRMATION:
            state = _get_business_state(context)
            result = state.purge_ledger_records()
            context.user_data.pop("next_photo_mode", None)
            context.user_data.pop("active_draft_id", None)
            context.user_data.pop("awaiting_cash_input", None)
            draft_bucket = _get_drafts(context)
            draft_bucket.clear()
            await _reply_with_log(
                update,
                "🧹 Ledger records wiped.\n"
                f"Removed {result.get('removed_customers', 0)} customer profiles, "
                f"{result.get('removed_entries', 0)} ledger rows, "
                f"and {result.get('removed_ingestion_events', 0)} ledger events.\n"
                "You can /ledger a fresh photo set anytime now.",
                source="ledger_wipe_completed",
                reply_markup=_command_keyboard(),
            )
        else:
            await _reply_with_log(
                update,
                "❗ Confirmation mismatch.\n"
                f"To wipe ledgers, send: `{LEDGER_RESET_CONFIRMATION}`\n"
                "If this was by mistake, send /cancel.",
                source="ledger_wipe_wrong_confirmation",
                reply_markup=_command_keyboard(),
            )
        return

    if context.user_data.get("awaiting_cash_input"):
        amount = _parse_cash_amount(update.effective_message.text if update.effective_message else "")
        if amount is None:
            await _reply_with_log(
                update,
                "I couldn’t parse that as a cash amount. Reply with just the number (example: 1450).",
                source="cash_parse_error",
                reply_markup=_command_keyboard(),
            )
            return
        await _store_cash_snapshot(update, context, amount)
        return

    await _reply_with_log(
        update,
        "I currently only support ledger extraction flow:\n"
        "1) `/ledger`\n"
        "2) send a clear photo of the ledger page.\n"
        "Then use `/cash`, `/insights`, or `/debtors`.\n"
        "Tip: use `/cash` first if you want a cash-vs-utang spending outlook.\n"
        "If this is stuck, send `/cancel`.",
        source="text_unsupported_ledger_only",
        reply_markup=_command_keyboard(),
    )


async def on_photo(update: "Update", context: "ContextTypes.DEFAULT_TYPE") -> None:
    _log_inbound(update, "PHOTO")
    if not await _ensure_ready(update):
        return

    mode = context.user_data.pop("next_photo_mode", None)
    if mode != "ledger":
        await _reply_with_log(
            update,
            "I’m in ledger-only mode. Send `/ledger` first, then send the ledger photo.",
            source="photo_not_in_ledger_mode",
            reply_markup=_command_keyboard(),
        )
        await _send_shortcuts(update)
        return

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
        customer_name = str(parsed.get("customer_name") or "Unknown").strip() or "Unknown"
        if customer_name.lower() == "unknown":
            _append_ledger_ocr_log(
                {
                    "event": "ledger_ocr_customer_missing",
                    "draft_id": draft_id,
                    "chat_id": update.effective_chat.id if update.effective_chat else None,
                    "user_id": (update.effective_user.id if update.effective_user else None),
                    "warnings": list(parsed.get("warnings", [])),
                    "raw_ocr_text": ocr_text,
                }
            )
            await _reply_with_log(
                update,
                "⚠️ Customer name was not detected in this ledger image.\n"
                "I cannot record this ledger yet. Please send a clearer photo of the ledger header and try again.",
                source="ledger_customer_missing",
            )
            await _send_shortcuts(update)
            return

        payload_for_log = _build_ledger_payload(
            draft_id,
            customer_name=customer_name,
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
            format_ledger_draft(
                entries,
                draft_id=draft_id,
                customer_name=customer_name,
            ),
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
    if str(customer_name).strip().lower() == "unknown":
        return {"status": "unknown_customer", "entries_added": 0}

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

async def on_callback(update: "Update", context: "ContextTypes.DEFAULT_TYPE") -> None:
    _log_inbound(update, "CALLBACK")
    query = update.callback_query
    await query.answer()

    data = query.data or ""
    action, _, callback_id = data.partition(":")

    if action == "chase":
        key = callback_id
        open_profiles = _collect_insight_metrics(_get_business_state(context)).get("open_profiles", [])
        selected = next(
            (row for row in open_profiles if str(row.get("customer_key")) == key),
            None,
        )
        if not selected:
            await _edit_with_log(
                update,
                source="callback_debtor_not_found",
                text="⚠️ Debtor not found or no longer has open debt.",
            )
            return

        message = _build_repayment_request_message(selected)
        await _edit_with_log(
            update,
            source="callback_repayment_message",
            text=(
                "💬 Copy-ready repayment reminder:\n\n"
                f"{message}\n\n"
                "Pick another debtor below if needed:"
            ),
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("↩️ Select another debtor", callback_data="chase_menu")]]
            ),
        )
        return

    if action == "chase_menu":
        open_profiles = _collect_insight_metrics(_get_business_state(context)).get("open_profiles", [])
        if not open_profiles:
            await _edit_with_log(
                update,
                source="callback_chase_menu_empty",
                text="No open debtors right now. Nothing to chase yet.",
            )
            return
        await _edit_with_log(
            update,
            source="callback_chase_menu",
            text="📬 Select debtor for repayment reminder.\nTap one name to generate a ready-to-send message.",
            reply_markup=_build_debtor_chase_markup(open_profiles),
        )
        return

    draft_id = callback_id
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
                added = int(result.get("entries_added", 0))
                if added:
                    msg = (
                        f"✅ Draft #{draft_id} confirmed and saved to ledger."
                        f" {added} new row(s) added."
                    )
                else:
                    msg = (
                        f"ℹ️ Draft #{draft_id} had no new rows to add."
                        " Entries may already exist for this customer."
                    )
            elif result["status"] == "already_saved":
                msg = (
                    f"✅ Draft #{draft_id} was already confirmed."
                )
            elif result["status"] == "unknown_customer":
                msg = (
                    f"⚠️ Draft #{draft_id} could not be saved: customer name not detected."
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
        else:
            await _edit_with_log(
                update,
                source="callback_confirm_unsupported",
                text=(
                    f"⚠️ Draft #{draft_id} is not a ledger draft."
                    " Use /ledger to create a ledger draft."
                ),
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
        "I’m in utang-ledger-only mode.\n"
        "Use `/ledger` and send a clear photo of the ledger page, then confirm.\n"
        "If you want a cash outlook first, send `/cash`.",
        source="unsupported_input",
        reply_markup=_command_keyboard(),
    )


async def on_cancel(update: "Update", context: "ContextTypes.DEFAULT_TYPE") -> None:
    _log_inbound(update, "COMMAND /cancel")
    if not await _ensure_ready(update):
        return

    context.user_data.pop("awaiting_cash_input", None)
    context.user_data.pop("awaiting_ledger_reset_confirmation", None)
    mode = context.user_data.pop("next_photo_mode", None)
    draft_id = context.user_data.pop("active_draft_id", None)
    cancelled_items: List[str] = []
    if mode:
        cancelled_items.append(f"mode:{mode}")

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


async def _store_cash_snapshot(
    update: "Update", context: "ContextTypes.DEFAULT_TYPE", amount: float
) -> None:
    state = _get_business_state(context)
    snapshot = state.add_cash_snapshot(
        amount,
        source="telegram_text",
        source_id=None,
        note="Manual cash check-in",
    )
    snapshot_time = _parse_iso_datetime(snapshot.get("snapshot_utc"))

    context.user_data.pop("awaiting_cash_input", None)
    metrics = _collect_insight_metrics(state)
    lines = [
        "💰 Cash snapshot saved.",
        f"Recorded: {_money(round(amount, 2))} @ {_format_projection_date(snapshot_time)}",
        "",
    ]
    lines.extend(_build_cash_outlook_lines(round(amount, 2), metrics, snapshot_time=(snapshot.get("snapshot_utc") or "")))

    await _reply_with_log(
        update,
        "\n".join(lines),
        source="cash_snapshot_saved",
        reply_markup=_command_keyboard(),
    )


async def on_cash(update: "Update", context: "ContextTypes.DEFAULT_TYPE") -> None:
    _log_inbound(update, "COMMAND /cash")
    if not await _ensure_ready(update):
        return
    args = getattr(context, "args", [])
    if args:
        raw_amount = " ".join(args)
        amount = _parse_cash_amount(raw_amount)
        if amount is None:
            await _reply_with_log(
                update,
                "I couldn’t parse that cash value. Reply with a number like `1450` or `PHP 1450.00`.",
                source="cash_parse_error",
                reply_markup=_command_keyboard(),
            )
            return
        await _store_cash_snapshot(update, context, amount)
        return

    context.user_data["awaiting_cash_input"] = True
    await _reply_with_log(
        update,
        "How much cash do you have right now?\n"
        "Reply with just the amount (examples: `1450`, `PHP 1450`, `₱1450.00`).",
        source="cash_prompt",
        reply_markup=_command_keyboard(),
    )


async def on_insights(update: "Update", context: "ContextTypes.DEFAULT_TYPE") -> None:
    _log_inbound(update, "COMMAND /insights")
    if not await _ensure_ready(update):
        return

    state = _get_business_state(context)
    metrics = _collect_insight_metrics(state)
    builder = _build_overview_lines
    lines = builder(metrics)

    await _reply_with_log(
        update,
        "\n".join(lines),
        source="insights_report",
        reply_markup=_command_keyboard(),
    )


async def on_debtors(update: "Update", context: "ContextTypes.DEFAULT_TYPE") -> None:
    _log_inbound(update, "COMMAND /debtors")
    if not await _ensure_ready(update):
        return

    metrics = _collect_insight_metrics(_get_business_state(context))
    profiles = metrics["customer_profiles"]
    if not profiles:
        await _reply_with_log(
            update,
            "📒 No ledger entries found yet.\n"
            "Use /ledger and send an updated photo to start tracking people.",
            source="debtors_empty",
            reply_markup=_command_keyboard(),
        )
        return

    ranked_profiles = [row for row in profiles if row["outstanding"] > 0.009]
    if not ranked_profiles:
        await _reply_with_log(
            update,
            "✅ No open utang now. Great job on collections.",
            source="debtors_no_open_debt",
            reply_markup=_command_keyboard(),
        )
        return

    ranked_profiles.sort(
        key=lambda row: (
            row.get("days_carried") or -1,
            row.get("risk_score", 0),
            row.get("outstanding", 0.0),
        ),
        reverse=True,
    )

    lines = [
        "📒 Utang Debtor Insights",
        "─────────────────────────────",
        "",
        "📌 Sorted by: debt carried (oldest) → risk signal → highest balance.",
        "📉 Debt bands: ✅ <=400, 🟢 <=1000, ⚠️ <=2000, 🚨 >3000",
        "🕒 Activity bands: ✅ 0-3d, 🟢 4-8d, ⚠️ 9-10d, 🚨 11d+",
        "",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        "👤 PERSON PROFILE",
        "",
        "💬 Use the buttons below to generate a repayment reminder.",
    ]
    for idx, row in enumerate(ranked_profiles, start=1):
        days = row.get("days_carried")
        if days is None:
            days_text = "n/a"
        else:
            days_text = f"{days} day(s)"
        avg_repayment_days = row.get("avg_repayment_days")
        if avg_repayment_days is None:
            avg_repayment_text = "avg repayment: need more payments to determine cadence"
        else:
            avg_repayment_text = f"avg repayment: every {avg_repayment_days}d"

        lines.append("")
        lines.append(f"{idx}) {row['risk_emoji']} {row['customer_name']}")
        lines.append(
            f"   • Outstanding: {_money(_safe_float(row['outstanding']))} · "
            f"debt: {row['debt_emoji']} {row['debt_label']}"
        )
        lines.append(
            f"   • Spent: {_money(_safe_float(row['total_spent']))} · "
            f"carried: {days_text}"
        )
        lines.append(
            f"   • Activity: {row['age_emoji']} {row['age_label']} · "
            f"last payment: {row.get('last_payment_text', 'n/a')}"
        )
        lines.append(f"   • {avg_repayment_text}")
        if row.get("days_since_payment") is not None and row["days_since_payment"] >= 15:
            lines.append(
                f"   • ⚠️ No payment for {row['days_since_payment']} day(s) "
                f"(last note: {row.get('last_note', '')})"
            )
            if row.get("risk_tier") in {"risk", "critical"}:
                lines.append("   • 🚨 Urgent follow-up recommended")

    reply_markup = _build_debtor_chase_markup(ranked_profiles)

    await _reply_with_log(
        update,
        "\n".join(lines),
        source="debtors_report",
        reply_markup=reply_markup,
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
        "• /start — reset and show focused workflow\n"
        "• /ledger — start ledger OCR flow\n"
        "• /cash — record cash-on-hand for utang recovery outlook\n"
        "• /insights — quick utang risk summary + concentration flags\n"
        "• /debtors — per-person utang details\n"
        "• /reset_ledger — delete all stored ledger records (confirmation word required)\n"
        "• /cancel — stop any in-progress action\n"
        "If you meant to process a ledger photo, send `/ledger` then send the photo.",
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
    logger.info(
        "Registering bot handlers: /start, /ledger, /cash, /insights, /debtors, /reset_ledger, /cancel, fallback command handler"
    )
    logger.info("Conversation logs: %s", _conversation_log_path())
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("ledger", on_ledger))
    application.add_handler(CommandHandler("cash", on_cash))
    application.add_handler(CommandHandler("reset_ledger", on_reset_ledger))
    application.add_handler(CommandHandler("cancel", on_cancel))
    application.add_handler(CommandHandler("insights", on_insights))
    application.add_handler(CommandHandler("debtors", on_debtors))
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
