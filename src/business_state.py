"""JSON-backed lightweight business state used by the MVP-lite bot."""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence


_FLOAT_RE = re.compile(r"^-?\d+(?:\.\d{1,2})?$")


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _slugify(value: str, max_len: int = 80) -> str:
    base = re.sub(r"[^a-z0-9]+", "_", (value or "").strip().lower())
    return (base.strip("_") or "item")[:max_len]


def _coerce_float(value: Any, *, default: float = 0.0) -> float:
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        candidate = value.strip().replace(",", "").replace("₱", "").replace("P", "")
        if not candidate:
            return default
        try:
            return float(candidate)
        except ValueError:
            return default
    return default


def _to_float(value: Any, *, default: Optional[float] = None) -> Optional[float]:
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        cleaned = value.strip().replace(",", "").replace("₱", "").replace("P", "")
        if not cleaned:
            return default
        try:
            return float(cleaned)
        except ValueError:
            return default
    return default


def _normalize_ts(ts: str | None) -> str:
    if ts:
        return ts
    return _now_utc()


class BusinessStateStore:
    """A minimal single-tenant JSON document for all required domains."""

    def __init__(self, path: str = "data/business_state.json") -> None:
        self.path = path
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)

    def load(self) -> Dict[str, Any]:
        if not os.path.exists(self.path):
            return self._new_state()
        with open(self.path, "r", encoding="utf-8") as handle:
            loaded = json.load(handle)
        if not isinstance(loaded, dict):
            return self._new_state()
        if "version" not in loaded:
            loaded["version"] = 1
        return self._normalize_state(loaded)

    def save(self, payload: Dict[str, Any]) -> None:
        payload["updated_utc"] = _now_utc()
        with open(self.path, "w", encoding="utf-8") as handle:
            json.dump(self._normalize_state(payload), handle, ensure_ascii=False, indent=2, sort_keys=True)

    def _new_state(self) -> Dict[str, Any]:
        return {
            "version": 1,
            "updated_utc": _now_utc(),
            "business_metadata": {
                "owner_name": "owner",
                "notes": "lightweight single-shop snapshot",
            },
            "customers": {},
            "inventory": {},
            "cash": {
                "snapshots": [],
                "latest_snapshot": None,
            },
            "loans": {},
            "offers": [],
            "catalog": {},
            "sales": [],
            "insights": [],
            "ingestion_log": [],
        }

    def _normalize_state(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        baseline = self._new_state()
        for key, default_value in baseline.items():
            payload.setdefault(key, default_value)
        if not isinstance(payload.get("customers"), dict):
            payload["customers"] = {}
        if not isinstance(payload.get("inventory"), dict):
            payload["inventory"] = {}
        if not isinstance(payload.get("loans"), dict):
            payload["loans"] = {}
        if not isinstance(payload.get("offers"), list):
            payload["offers"] = []
        if not isinstance(payload.get("catalog"), dict):
            payload["catalog"] = {}
        if not isinstance(payload.get("sales"), list):
            payload["sales"] = []
        if not isinstance(payload.get("insights"), list):
            payload["insights"] = []
        if not isinstance(payload.get("ingestion_log"), list):
            payload["ingestion_log"] = []
        if not isinstance(payload.get("cash"), dict):
            payload["cash"] = baseline["cash"]
        if not isinstance(payload["cash"].get("snapshots"), list):
            payload["cash"]["snapshots"] = []
        return payload

    def _next_id(self, section: str, prefix: str) -> str:
        payload = self.load()
        bucket = payload.get(section)
        counter = 0
        if isinstance(bucket, dict):
            counter = len(bucket)
        elif isinstance(bucket, list):
            counter = len(bucket)
        base = f"{prefix}-{len(str(_now_utc()))}-{datetime.now(timezone.utc).microsecond:06d}"
        payload["updated_utc"] = _now_utc()
        payload_hash = hashlib.md5(base.encode("utf-8")).hexdigest()[:10]
        return f"{prefix}-{counter + 1:03d}-{payload_hash}"

    def _append_ingestion(self, payload: Dict[str, Any], event: Dict[str, Any]) -> None:
        event.setdefault("event_id", self._next_id("ingestion_log", "ing"))
        event.setdefault("recorded_at", _now_utc())
        payload["ingestion_log"].append(event)

    def upsert_customer_ledger(
        self,
        customer_name: str,
        rows: Sequence[Dict[str, Any]],
        *,
        source: str,
        source_id: str | None = None,
        draft_payload: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        payload = self.load()
        key = _slugify(customer_name)
        customers = payload["customers"]
        customer = customers.setdefault(
            key,
            {
                "customer_key": key,
                "customer_name": customer_name or "Unknown",
                "entries": [],
                "current_balance": 0.0,
                "first_seen_utc": _now_utc(),
                "last_updated_utc": None,
                "is_irrecoverable": False,
            },
        )

        added = 0
        running = _coerce_float(customer.get("current_balance"), default=0.0)
        for idx, row in enumerate(rows):
            if not isinstance(row, dict):
                continue
            payload_row = dict(row)
            amount = _coerce_float(payload_row.get("amount"), default=0.0)
            if amount == 0.0 and payload_row.get("entry_kind") != "payment":
                # keep zeros only if parser explicitly included them
                continue
            running += amount
            payload_row.setdefault("entry_id", self._next_id("customers", f"led-{key}-{idx}"))
            payload_row.setdefault("source", source)
            payload_row.setdefault("source_id", source_id)
            if payload_row.get("running_balance") is None:
                payload_row["running_balance"] = round(running, 2)
            customer["entries"].append(payload_row)
            added += 1

        customer["current_balance"] = round(_coerce_float(customer.get("current_balance"), default=0.0) + sum(_coerce_float(r.get("amount"), default=0.0) for r in customer["entries"][-added:]) , 2)
        customer["last_updated_utc"] = _now_utc()
        if customer["first_seen_utc"] is None:
            customer["first_seen_utc"] = _now_utc()

        self._append_ingestion(
            payload,
            {
                "kind": "ledger_posted",
                "source": source,
                "source_id": source_id,
                "customer_key": key,
                "entries_added": added,
                "payload_json": draft_payload,
            },
        )
        self.save(payload)
        return {
            "customer_key": key,
            "entries_added": added,
            "entries_total": len(customer.get("entries", [])),
            "current_balance": customer.get("current_balance", 0.0),
        }

    def add_cash_snapshot(
        self,
        cash_amount: float,
        *,
        source: str,
        source_id: str | None = None,
        note: str | None = None,
    ) -> Dict[str, Any]:
        payload = self.load()
        normalized = round(_coerce_float(cash_amount, default=0.0), 2)
        snapshot = {
            "cash_snapshot_id": self._next_id("cash", "cash"),
            "snapshot_utc": _now_utc(),
            "cash_amount": normalized,
            "source": source,
            "source_id": source_id,
            "note": note,
        }
        payload["cash"]["snapshots"].append(snapshot)
        payload["cash"]["latest_snapshot"] = snapshot
        self._append_ingestion(
            payload,
            {
                "kind": "cash_snapshot",
                "source": source,
                "source_id": source_id,
                "amount": normalized,
            },
        )
        self.save(payload)
        return snapshot

    def add_loan(
        self,
        lender_name: str,
        *,
        principal: float,
        interest_rate: float | None = None,
        installment_amount: float | None = None,
        next_due_date: str | None = None,
        source: str = "manual",
        source_id: str | None = None,
    ) -> Dict[str, Any]:
        payload = self.load()
        key = _slugify(f"{lender_name}-{principal}-{_now_utc()}")
        loan = {
            "loan_id": self._next_id("loans", f"loan-{key}"),
            "lender_name": lender_name or "Unknown",
            "principal": round(_coerce_float(principal, default=0.0), 2),
            "current_balance": round(_coerce_float(principal, default=0.0), 2),
            "interest_rate": None if interest_rate is None else round(float(interest_rate), 2),
            "installment_amount": None
            if installment_amount is None else round(float(installment_amount), 2),
            "next_due_date": _normalize_ts(next_due_date),
            "status": "active",
            "created_utc": _now_utc(),
            "payments": [],
            "source": source,
            "source_id": source_id,
        }
        payload["loans"][key] = loan
        self._append_ingestion(
            payload,
            {
                "kind": "loan_created",
                "source": source,
                "source_id": source_id,
                "loan_id": loan["loan_id"],
                "lender_name": lender_name,
            },
        )
        self.save(payload)
        return loan

    def adjust_inventory(
        self,
        sku_name: str,
        *,
        qty_delta: float,
        reason: str = "manual",
        unit: str = "pcs",
        source: str = "manual",
        source_id: str | None = None,
        unit_cost: float | None = None,
    ) -> Dict[str, Any]:
        payload = self.load()
        key = _slugify(sku_name)
        inventory = payload["inventory"]
        product = inventory.setdefault(
            key,
            {
                "sku_id": key,
                "display_name": sku_name or "Unknown",
                "unit": unit,
                "on_hand_qty": 0.0,
                "reorder_point": 0.0,
                "cost_estimate": None,
                "events": [],
                "first_seen_utc": _now_utc(),
                "last_updated_utc": None,
            },
        )
        product["display_name"] = sku_name or product.get("display_name", "Unknown")
        product["unit"] = unit or product.get("unit", "pcs")
        if unit_cost is not None:
            product["cost_estimate"] = round(float(unit_cost), 2)

        event = {
            "inventory_event_id": self._next_id("inventory", f"inv-{key}"),
            "qty_delta": round(_coerce_float(qty_delta, default=0.0), 3),
            "reason": reason,
            "source": source,
            "source_id": source_id,
            "recorded_utc": _now_utc(),
            "unit": product.get("unit", "pcs"),
        }
        product["events"].append(event)
        product["on_hand_qty"] = round(_coerce_float(product.get("on_hand_qty", 0.0), default=0.0) + event["qty_delta"], 3)
        product["last_updated_utc"] = _now_utc()
        self._append_ingestion(
            payload,
            {
                "kind": "inventory_adjusted",
                "source": source,
                "source_id": source_id,
                "sku_id": key,
                "qty_delta": event["qty_delta"],
            },
        )
        self.save(payload)
        return {"sku_id": key, "product": product, "event": event}

    def add_offer(
        self,
        supplier_name: str,
        sku_name: str,
        unit_price: float,
        *,
        supplier_type: str = "agent",
        unit: str = "pcs",
        source: str = "manual",
        source_id: str | None = None,
        effective_date: str | None = None,
    ) -> Dict[str, Any]:
        payload = self.load()
        sku_id = _slugify(sku_name)
        offer = {
            "offer_id": self._next_id("offers", f"offer-{sku_id}"),
            "supplier_name": supplier_name or "Unknown",
            "supplier_type": supplier_type,
            "sku_id": sku_id,
            "sku_name": sku_name,
            "unit": unit,
            "unit_price": round(_coerce_float(unit_price, default=0.0), 2),
            "effective_date": _normalize_ts(effective_date),
            "source": source,
            "source_id": source_id,
            "recorded_utc": _now_utc(),
        }
        payload["offers"].append(offer)
        self._append_ingestion(
            payload,
            {
                "kind": "offer_recorded",
                "source": source,
                "source_id": source_id,
                "supplier_name": supplier_name,
                "sku_id": sku_id,
            },
        )
        self.save(payload)
        return offer

    def add_sale_record(
        self,
        *,
        draft_id: str,
        source: str,
        raw: str,
        lines: Sequence[Dict[str, Any]],
        paid_cash: bool = True,
        customer_name: str | None = None,
    ) -> Dict[str, Any]:
        payload = self.load()
        line_items: List[Dict[str, Any]] = []
        for row in lines:
            if not isinstance(row, dict):
                continue
            qty = _to_float(row.get("qty"), default=1.0) or 0.0
            unit_price = _to_float(row.get("price"), default=0.0) or 0.0
            if qty <= 0 or unit_price < 0:
                continue
            line_items.append(
                {
                    "sku_name": str(row.get("item", row.get("name", "")) or ""),
                    "qty": qty,
                    "unit_sale_price": round(unit_price, 2),
                    "line_total": round(qty * unit_price, 2),
                    "raw": str(row.get("raw", "")),
                }
            )

        total = round(sum(r.get("line_total", 0.0) for r in line_items), 2)
        sale_record = {
            "sale_id": self._next_id("sales", "sale"),
            "draft_id": draft_id,
            "recorded_utc": _now_utc(),
            "source": source,
            "raw": raw,
            "customer_name": customer_name,
            "paid_cash": bool(paid_cash),
            "lines": line_items,
            "total": total,
        }
        payload["sales"].append(sale_record)
        self._append_ingestion(
            payload,
            {
                "kind": "sale_posted",
                "source": source,
                "source_id": draft_id,
                "sale_id": sale_record["sale_id"],
                "total": total,
            },
        )
        self.save(payload)
        return {"sale_id": sale_record["sale_id"], "total": total, "lines": len(line_items)}

    def get_open_debtors(self, *, min_amount: float = 0.01) -> List[Dict[str, Any]]:
        payload = self.load()
        debtors: List[Dict[str, Any]] = []
        for customer in payload.get("customers", {}).values():
            if not isinstance(customer, dict):
                continue
            balance = _coerce_float(customer.get("current_balance"), default=0.0)
            if balance > min_amount:
                debtors.append(
                    {
                        "customer_key": customer.get("customer_key"),
                        "customer_name": customer.get("customer_name", "Unknown"),
                        "current_balance": round(balance, 2),
                        "last_updated_utc": customer.get("last_updated_utc"),
                    }
                )
        debtors.sort(key=lambda item: _coerce_float(item.get("current_balance"), default=0.0), reverse=True)
        return debtors

    def get_cash_latest(self) -> Dict[str, Any] | None:
        payload = self.load()
        latest = payload.get("cash", {}).get("latest_snapshot")
        return latest if isinstance(latest, dict) else None

    def get_cash_snapshots(self) -> List[Dict[str, Any]]:
        payload = self.load()
        snapshots = payload.get("cash", {}).get("snapshots", [])
        return snapshots if isinstance(snapshots, list) else []

    def total_open_loans(self) -> float:
        payload = self.load()
        total = 0.0
        for loan in payload.get("loans", {}).values():
            if isinstance(loan, dict):
                total += _coerce_float(loan.get("current_balance"), default=0.0)
        return round(total, 2)

    def total_open_utang(self) -> float:
        payload = self.load()
        total = 0.0
        for customer in payload.get("customers", {}).values():
            if isinstance(customer, dict):
                total += max(0.0, _coerce_float(customer.get("current_balance"), default=0.0))
        return round(total, 2)

    def get_inventory(self) -> Dict[str, Any]:
        payload = self.load()
        return payload.get("inventory", {})

    def get_sales_since(self, since_iso: str) -> List[Dict[str, Any]]:
        payload = self.load()
        try:
            since_dt = datetime.fromisoformat(since_iso.replace("Z", "+00:00"))
        except ValueError:
            since_dt = datetime.min.replace(tzinfo=timezone.utc)
        sales = []
        for sale in payload.get("sales", []):
            if not isinstance(sale, dict):
                continue
            recorded_raw = str(sale.get("recorded_utc", ""))
            try:
                recorded = datetime.fromisoformat(recorded_raw.replace("Z", "+00:00"))
            except ValueError:
                continue
            if recorded >= since_dt:
                sales.append(sale)
        return sales
