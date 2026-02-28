"""Utang-ledger OCR parsing and lightweight JSON persistence."""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Sequence


_MONTH_TO_NUMBER = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}
_MONTH_RE = "(?:" + "|".join(sorted(_MONTH_TO_NUMBER.keys(), key=len, reverse=True)) + ")"
_DATE_RE = re.compile(rf"^\s*({_MONTH_RE})\s*([ivx\d]+)\s*(.*)$", re.IGNORECASE)
_DATE_RE_ALT = re.compile(r"^\s*([ivx\d]{1,4})\s*[\.\:\-\)]?\s*$", re.IGNORECASE)


def _month_to_number(month_token: str) -> int | None:
    token = (month_token or "").strip().lower()
    token = token.replace(".", "")
    return _MONTH_TO_NUMBER.get(token)
_AMOUNT_WITH_PESO_RE = re.compile(
    r"\b(?:PHP|₱|P|Php|php)\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)\b",
    re.IGNORECASE,
)
_PURE_AMOUNT_RE = re.compile(
    r"^\s*(?:P|₱|PHP|php)?\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)\s*$",
    re.IGNORECASE,
)
_HEADER_RE = re.compile(r"^.*utang\s+ledger\s*[-:]\s*(.+?)\s*$", re.IGNORECASE)
_HEADER_NAME_STOP_RE = re.compile(
    r"\b(?:brgy\.?|barangay|blk\.?|block|purok|sitio|zone|city|province|municipality|municipal|b(?:ul)?acan)\b",
    re.IGNORECASE,
)


def _slugify(value: str, max_len: int = 80) -> str:
    base = re.sub(r"[^a-z0-9]+", "_", value.strip().lower())
    return (base.strip("_") or "customer")[:max_len]


def _normalize_header(customer_line: str) -> str:
    m = _HEADER_RE.match(customer_line.strip())
    if not m:
        return "Unknown"

    name = m.group(1).strip()
    name = name.lstrip(":-").strip()
    if not name:
        return "Unknown"

    # Remove parenthesized notes and comma-separated suffixes.
    if "(" in name:
        name = name.split("(", 1)[0].strip()
    if "," in name:
        name = name.split(",", 1)[0].strip()

    # Heuristic fallback for OCR headers that miss a delimiter between
    # customer name and address text.
    stop_match = _HEADER_NAME_STOP_RE.split(name, maxsplit=1)
    if len(stop_match) > 1:
        name = stop_match[0].strip()

    name = re.sub(r"\s+", " ", name).strip()
    return name or "Unknown"


def _cleanup_note(raw_note: str) -> str:
    note = re.sub(r"\s+", " ", (raw_note or "").strip())
    note = note.lstrip(" .,:;)]}")
    return note.strip()


def _split_ocr_day_token(token: str) -> tuple[Optional[int], str, bool]:
    raw = token.strip()
    if not raw:
        return None, "", False

    # Numeric OCR merges like "164" where day is 16 and trailing "4" belongs to note.
    if raw.isdigit():
        if len(raw) <= 2:
            return _roman_to_int(raw), "", False
        for size in (2, 1):
            day_candidate = _roman_to_int(raw[:size])
            if day_candidate is not None:
                return day_candidate, raw[size:], True
        return None, raw, False

    # Roman-like merge fallback, e.g., "I6" or "III1".
    for size in range(len(raw), 0, -1):
        day_candidate = _roman_to_int(raw[:size])
        if day_candidate is not None:
            suffix = raw[size:]
            return day_candidate, suffix, bool(suffix)

    return None, "", False


def _roman_to_int(token: str) -> Optional[int]:
    normalized = token.strip().upper().replace("O", "0").replace("|", "I")
    if not normalized:
        return None
    if normalized.isdigit():
        value = int(normalized)
        return value if 1 <= value <= 31 else None

    # OCR often merges digits and Roman tokens (e.g., I1 or 1I). Try numeric fallback.
    numeric_fallback = (
        normalized.replace("I", "1")
        .replace("L", "1")
        .replace("l", "1")
        .replace("O", "0")
    )
    if numeric_fallback.isdigit():
        value = int(numeric_fallback)
        return value if 1 <= value <= 31 else None

    if normalized in {"I", "II"}:
        # Common OCR corruption for 11 on this notebook style.
        return 11

    values = {"I": 1, "V": 5, "X": 10}
    total = 0
    prev = 0
    for ch in normalized:
        if ch not in values:
            return None
        cur = values[ch]
        if cur > prev:
            total += cur - 2 * prev
        else:
            total += cur
        prev = cur
    if total < 1 or total > 31:
        return None
    return total


def _parse_date_line(
    line: str,
    default_month: int | None = None,
) -> Optional[tuple[str, str, bool]]:
    m = _DATE_RE.match(line.strip())
    if m:
        month_token = m.group(1)
        day_token = m.group(2)
        rest = (m.group(3) or "").strip()
        month = _month_to_number(month_token)
        if month is None:
            return None
        day, suffix, merged = _split_ocr_day_token(day_token)
        if day is None:
            return None
        if suffix:
            if rest:
                rest = f"{suffix} {rest}"
            else:
                rest = suffix

        rest = rest.strip()
        if rest.startswith(".") or rest.startswith(":") or rest.startswith(")"):
            rest = rest[1:].strip()
        if day:
            year = datetime.utcnow().year
            date_iso = f"{year}-{month:02d}-{day:02d}"
            return date_iso, rest, merged

    m = _DATE_RE_ALT.match(line.strip())
    if not m:
        return None
    if default_month is None:
        return None
    year = datetime.utcnow().year
    day = _roman_to_int(m.group(1))
    if not day:
        return None
    return f"{year}-{default_month:02d}-{day:02d}", "", False


def _pick_row_amount_balance(
    amounts: Sequence[float],
    previous_balance: Optional[float],
    entry_kind: str,
) -> tuple[Optional[float], Optional[float], float]:
    values = [float(a) for a in amounts]
    if not values:
        return None, None, 0.35

    if len(values) == 1:
        return values[0], None, 0.7

    if len(values) == 2:
        return values[0], values[1], 0.95

    if previous_balance is not None:
        expected_sign = -1.0 if entry_kind == "payment" else 1.0
        best_error = float("inf")
        selected: tuple[Optional[float], Optional[float]] = (None, None)
        for i, amount in enumerate(values):
            for j in range(i + 1, len(values)):
                balance = values[j]
                candidate_amount = amount
                expected_balance = previous_balance + expected_sign * candidate_amount
                err = abs(expected_balance - balance)
                if err < best_error:
                    best_error = err
                    selected = (candidate_amount, balance)

        if selected[0] is not None and best_error <= 2.0:
            confidence = 0.75 if best_error <= 0.5 else 0.7
            return selected[0], selected[1], confidence

    return values[-2], values[-1], 0.7


def _looks_like_footer(line: str) -> bool:
    lowered = line.lower().strip()
    return (
        lowered.startswith("total")
        or lowered.startswith("hg")
        or lowered.startswith("math")
    )


def _normalize_bayad_token(value: str) -> str:
    token = re.sub(r"[^a-z0-9]", "", value.lower())
    token = token.replace("0", "o").replace("1", "i").replace("4", "a").replace("5", "s")
    token = token.replace("6", "b").replace("7", "t").replace("8", "b")
    token = token.replace("l", "i")
    token = token.replace("m", "b")
    token = token.replace("r", "b")
    return token


def _levenshtein_distance(left: str, right: str) -> int:
    if not left:
        return len(right)
    if not right:
        return len(left)

    rows = list(range(len(right) + 1))
    for i, left_char in enumerate(left, start=1):
        previous_row = rows
        rows = [i] + [0] * len(right)
        for j, right_char in enumerate(right, start=1):
            insert_cost = rows[j - 1] + 1
            delete_cost = previous_row[j] + 1
            replace_cost = previous_row[j - 1] + (left_char != right_char)
            rows[j] = min(insert_cost, delete_cost, replace_cost)
    return rows[-1]


def _is_bayad(value: str) -> bool:
    token = _normalize_bayad_token(value)
    if not token:
        return False

    exact = {
        "bayad",
        "bayat",
        "b4yad",
        "mayad",
        "bayadf",
        "bayada",
        "bayad0",
        "bayad5",
        "payad",
        "pbayad",
    }
    if token in exact:
        return True

    # Common OCR variants: mayad, byad, p?ayad, leading/trailing extra marks.
    if token.startswith(("bay", "may", "pay", "baya", "byad")) and token.endswith(("ad", "at", "aq", "aqf", "afd", "afd0", "f")):
        return True

    if token.startswith("b") and "aya" in token:
        return True
    if token.startswith("p") and len(token) >= 4 and token[1:4] == "aya":
        return True
    if token.startswith("m") and token.endswith("ad"):
        return True

    # Allow one- to two-character OCR distortions for the critical payment marker.
    if abs(len(token) - len("bayad")) <= 2 and _levenshtein_distance(token, "bayad") <= 2:
        return True

    return False


def _extract_amounts(line: str) -> List[float]:
    vals: List[float] = []
    for token in _AMOUNT_WITH_PESO_RE.findall(line):
        if token is None:
            continue
        clean = token.replace(",", "")
        if clean.count(".") > 1:
            continue
        try:
            vals.append(round(float(clean), 2))
        except ValueError:
            continue

    # Handle OCR-only rows like "56.01" with no peso symbol.
    if not vals and _PURE_AMOUNT_RE.match(line):
        try:
            vals.append(round(float(line.strip().replace("P", "").replace(",", "")), 2))
        except ValueError:
            pass
    return vals


def _classify_row_kind(note: str, amounts: Sequence[float], payment_flag: bool) -> str:
    if payment_flag or _is_bayad(note):
        return "payment"
    if not amounts and not note.strip():
        return "unknown"
    return "credit_sale"


@dataclass
class LedgerParseLine:
    date: str
    entry_kind: str
    note: str
    amount: Optional[float]
    running_balance: Optional[float]
    raw_lines: List[str]
    confidence: float = 0.8
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, object]:
        return {
            "date": self.date,
            "entry_kind": self.entry_kind,
            "note": self.note,
            "amount": self.amount,
            "running_balance": self.running_balance,
            "raw_lines": self.raw_lines,
            "confidence": self.confidence,
            "warnings": self.warnings or [],
        }


def parse_ledger_ocr_text(text: str, customer_name_hint: Optional[str] = None) -> Dict[str, object]:
    lines = [ln.strip() for ln in text.splitlines()]
    if not lines:
        return {
            "customer_name": customer_name_hint or "Unknown",
            "entries": [],
            "warnings": ["No OCR text found."],
        }

    customer_name = None
    for ln in lines[:8]:
        found = _normalize_header(ln)
        if found != "Unknown":
            customer_name = found
            break
    if customer_name is None:
        customer_name = customer_name_hint or "Unknown"

    entries: List[LedgerParseLine] = []
    parse_warnings: List[str] = []
    previous_balance: Optional[float] = None
    current_month: Optional[int] = None
    current: Optional[Dict[str, object]] = None

    def flush_current() -> None:
        nonlocal current, entries, parse_warnings, previous_balance
        if not current:
            return

        date = current.get("date")
        raw_amounts = current.get("amounts", [])
        amounts = [float(value) for value in raw_amounts] if isinstance(raw_amounts, list) else []
        note = _cleanup_note(current.get("note") or "")
        payment_flag = bool(current.get("payment_flag"))

        if not date:
            parse_warnings.append("Dropped row without date.")
            current = None
            return

        if len(amounts) == 0:
            # Keep ambiguous rows in the review draft instead of silently dropping them.
            raw_lines = list(current.get("raw_lines", []))
            if (
                not note
                and not payment_flag
                and len(raw_lines) <= 1
            ):
                # Footer/scan boundaries can produce trailing date-only lines (e.g. date before TOTAL).
                parse_warnings.append(
                    f"Dropped date-only row on {date} because no readable amounts were found."
                )
                current = None
                return

            warnings = [f"Row on {date} has note but no readable amounts."]
            if note:
                warnings.append("Please verify manually; OCR could not parse amount.")
            entries.append(
                LedgerParseLine(
                    date=str(date),
                    entry_kind=_classify_row_kind(note=note, amounts=amounts, payment_flag=payment_flag),
                    note=note,
                    amount=None,
                    running_balance=None,
                    raw_lines=list(current.get("raw_lines", [])),
                    confidence=0.35,
                    warnings=warnings,
                )
            )
            current = None
            return

        entry_kind = _classify_row_kind(note=note, amounts=amounts, payment_flag=payment_flag)
        amount, running_balance, confidence = _pick_row_amount_balance(
            amounts=amounts,
            previous_balance=previous_balance,
            entry_kind=entry_kind,
        )
        if entry_kind == "payment" and amount is not None:
            amount = -abs(amount)

        entries.append(
            LedgerParseLine(
                date=str(date),
                entry_kind=entry_kind,
                note=note or "",
                amount=amount,
                running_balance=running_balance,
                raw_lines=list(current.get("raw_lines", [])),
                confidence=confidence,
                warnings=[],
            )
        )
        if running_balance is not None:
            previous_balance = running_balance
        current = None

    for idx, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        if _looks_like_footer(line):
            flush_current()
            break

        parsed_date = _parse_date_line(line, default_month=current_month)
        if parsed_date:
            date, trailing, merged_day_token = parsed_date
            if date and "-" in date:
                current_month = int(date.split("-")[1])
            if (
                current is not None
                and merged_day_token
                and not current.get("amounts")
                and current.get("note")
            ):
                current["raw_lines"].append(f"{idx}: {line}")
                if trailing:
                    trailing = trailing.strip()
                    if trailing:
                        current["raw_lines"].append(f"{idx}: [tail]{trailing}")
                        if _extract_amounts(trailing):
                            current["amounts"].extend(_extract_amounts(trailing))
                        if _is_bayad(trailing):
                            current["payment_flag"] = True
                        elif trailing:
                            current["note"] = _cleanup_note(f"{current.get('note', '').strip()} {trailing}")
                continue

            flush_current()
            current = {
                "date": date,
                "amounts": [],
                "raw_lines": [f"{idx}: {line}"],
                "note": "",
                "payment_flag": False,
            }
            if trailing:
                # Example OCR shape: "Mar 14BAYAD"
                trailing = trailing.strip()
                if trailing:
                    current["raw_lines"].append(f"{idx}: [tail]{trailing}")
                    if _extract_amounts(trailing):
                        current["amounts"].extend(_extract_amounts(trailing))
                    if _is_bayad(trailing):
                        current["payment_flag"] = True
                    elif trailing:
                        current["note"] += f"{trailing} "
            continue

        if current is None:
            # Allow header-only lines to keep scanning.
            if customer_name_hint is None and _normalize_header(line) != "Unknown":
                customer_name = _normalize_header(line)
            continue

        current["raw_lines"].append(f"{idx}: {line}")
        if _is_bayad(line):
            current["payment_flag"] = True
            if line.lower().strip() != "bayad" and _extract_amounts(line):
                current["amounts"].extend(_extract_amounts(line))
            continue

        parsed_amounts = _extract_amounts(line)
        if parsed_amounts:
            current["amounts"].extend(parsed_amounts)
            continue

        if line:
            current["note"] = _cleanup_note(
                f"{current.get('note', '').strip()} {line}"
            )

    flush_current()

    if not entries:
        parse_warnings.append("No valid ledger rows parsed from OCR text.")

    if not customer_name or customer_name == "Unknown":
        # Entries without a detected customer name should never be recorded.
        if entries:
            parse_warnings.append(
                f"Customer name not detected. Refusing to record {len(entries)} parsed row(s) for unknown customer."
            )
        entries = []
        parse_warnings.append("Customer name not detected; cannot record this ledger until OCR clearly shows customer name.")

    warnings = list(dict.fromkeys(parse_warnings))
    return {
        "customer_name": customer_name or "Unknown",
        "entries": [entry.to_dict() for entry in entries],
        "warnings": warnings,
    }


def format_ledger_draft(
    entries: Sequence[Dict[str, object]],
    *,
    draft_id: str,
    customer_name: str | None = None,
) -> str:
    if not entries:
        name = str(customer_name or "Unknown").strip() or "Unknown"
        return (
            f"📒 Draft #{draft_id} (ledger): I couldn’t parse any utang rows.\n"
            f"Customer: {name}\n"
            "Try a clearer image or send the same photo again."
        )

    name = str(customer_name or "Unknown").strip() or "Unknown"
    body: List[str] = [
        f"=== Draft #{draft_id} (ledger) ===",
        f"Customer: {name}",
        "Utang Entries Preview",
    ]
    for index, row in enumerate(entries, start=1):
        date = str(row.get("date", ""))
        kind = str(row.get("entry_kind", "credit_sale"))
        note = str(row.get("note", ""))
        balance = row.get("running_balance")
        amount_value = row.get("amount")
        if amount_value is None:
            amount_text = "unreadable"
        else:
            amount_text = f"{float(amount_value):.2f}"
        body.append(
            f"{index:02d}. {date} | {kind} | PHP {amount_text}"
            + (f" | bal {balance:.2f}" if balance is not None else "")
            + (f" | {note}" if note else "")
        )

    body.append("")
    body.append(f"Rows: {len(entries)}")
    body.append("Is this correct?")
    return "\n".join(body)


class UtangLedgerStore:
    """Minimal JSON-backed persistence for one-tenant MVP."""

    def __init__(self, path: str = "data/utang_ledger_store.json") -> None:
        self.path = path
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)

    def load(self) -> Dict[str, object]:
        if not os.path.exists(self.path):
            return {"version": 1, "ledgers": {}}
        with open(self.path, "r", encoding="utf-8") as handle:
            return json.load(handle)

    def save(self, payload: Dict[str, object]) -> None:
        with open(self.path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)

    def upsert_ledger(
        self,
        customer_name: str,
        entries: Sequence[Dict[str, object]],
        source: str,
        source_id: Optional[str] = None,
    ) -> Dict[str, object]:
        payload = self.load()
        ledgers = payload.setdefault("ledgers", {})
        key = _slugify(customer_name)
        customer = ledgers.setdefault(
            key,
            {
                "customer_name": customer_name,
                "entries": [],
                "last_updated_utc": None,
            },
        )

        now = datetime.utcnow().isoformat() + "Z"
        source_hash = None
        if source:
            source_hash = hashlib.sha1((source + (source_id or "")).encode()).hexdigest()

        appended = 0
        for row in entries:
            payload_row = dict(row)
            payload_row.setdefault("entry_id", hashlib.sha1(
                (customer_name + (row.get("date") or "") + str(row.get("amount", "" ) ) + now + str(appended)).encode()
            ).hexdigest())
            payload_row.setdefault("source", source)
            payload_row.setdefault("source_id", source_id)
            payload_row.setdefault("source_hash", source_hash)
            customer["entries"].append(payload_row)
            appended += 1

        customer["last_updated_utc"] = now
        payload["version"] = 1
        self.save(payload)
        return {"customer_key": key, "entries_added": appended, "entries_total": len(customer["entries"])}

    def get_customer_ledger(self, customer_name: str) -> List[Dict[str, object]]:
        payload = self.load()
        ledgers = payload.get("ledgers", {})
        key = _slugify(customer_name)
        entry_bucket = ledgers.get(key, {})
        return list(entry_bucket.get("entries", []))


def extract_text_from_image(path: str) -> str:
    """Run OCR on a local image path, if OCR engine is available."""
    try:
        from paddleocr import PaddleOCR
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "PaddleOCR not installed. Install with `pip install paddleocr` for image OCR."
        ) from exc

    ocr = PaddleOCR(use_angle_cls=True, lang="en", show_log=False)
    result = ocr.ocr(path)
    lines: List[str] = []
    for block in result:
        for row in block:
            text = row[1][0]
            if text:
                lines.append(str(text).strip())
    return "\n".join(lines)
