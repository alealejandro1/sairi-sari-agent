#!/usr/bin/env python3
"""Generate a deterministic `business_state.json` fixture for MVP-lite."""

from __future__ import annotations

import argparse
import random
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List

sys.path.append(str(Path(__file__).resolve().parents[1] / "src"))
from business_state import BusinessStateStore

SEED = 20260228
DEFAULT_OUTPUT = Path(__file__).resolve().parents[1] / "data" / "synthetic" / "business_state.json"

CUSTOMERS = ["Maria next door", "Ate Nena", "Kuya Lito", "Mang Tony", "Aling Liza"]
SUPPLIERS = [
    ("Rodel fishpond", "agent"),
    ("North Valley Agent", "agent"),
    ("City Wholesale Mart", "wholesaler"),
    ("Barangay Distributor", "agent"),
]
SKU_TEMPLATES = [
    ("Marbobo", "pack", 75.00, 95.00),
    ("Balao", "pack", 42.00, 58.00),
    ("KopiPawa", "sachet", 5.20, 8.30),
    ("Hydro water", "bottle", 9.00, 13.50),
    ("SeasonBite", "bottle", 11.80, 16.50),
    ("ChocoJoy", "pack", 6.50, 10.20),
    ("Wafer Crisp", "pack", 5.30, 9.40),
    ("Cruncher", "pack", 5.10, 8.90),
    ("Luntuk beer", "bottle", 32.00, 45.00),
    ("Luma laundry", "piece", 16.50, 24.00),
]
LEDGER_NOTES = [
    "2 Marlboro, Mang Tomas, Magic Sarap",
    "1 hotdog, 3 Nova",
    "5 KopiPawa, 2 Hydro",
    "3 Cruncher, 4 Wafer Crisp",
    "10 Glow sachet, 2 SeasonBite",
    "2 Luntuk beer, 10 Balao",
    "1 Marbobo box 20s",
    "6 Hydro water, 1 Bathy soap",
]


def _slug(value: str) -> str:
    normalized = "".join(ch.lower() if ch.isalnum() else "_" for ch in value).strip("_")
    while "__" in normalized:
        normalized = normalized.replace("__", "_")
    return normalized or "item"


def _iso(day: date, hour: int = 9, minute: int = 0) -> str:
    return datetime(day.year, day.month, day.day, hour, minute, 0, tzinfo=timezone.utc).isoformat()


def _money(rng: random.Random, lo: float, hi: float) -> float:
    return round(rng.uniform(lo, hi), 2)


def _build_ledger_rows(rng: random.Random, customer_name: str, start_day: date) -> List[Dict[str, object]]:
    rows: List[Dict[str, object]] = []
    balance = 0.0
    for i in range(rng.randint(6, 10)):
        event_day = start_day + timedelta(days=rng.randint(0, 44))
        is_payment = rng.random() < 0.28 and balance > 12.0
        if is_payment:
            amount = -round(_money(rng, 20.0, min(balance, 145.0)), 2)
            kind = "payment"
            note = "BAYAD"
        else:
            amount = float(rng.choice([47.0, 62.0, 72.0, 81.0, 99.0, 118.0]))
            kind = "credit_sale"
            note = rng.choice(LEDGER_NOTES)
        balance = round(balance + amount, 2)
        rows.append(
            {
                "date": event_day.isoformat(),
                "entry_kind": kind,
                "note": note,
                "amount": amount,
                "running_balance": max(balance, 0.0),
                "raw_lines": [f"{customer_name}: row {i+1}"],
                "confidence": 0.88,
                "warnings": [],
            }
        )
    # keep rows roughly chronological for readability
    return sorted(rows, key=lambda row: row["date"])


def generate(seed: int = SEED, output: Path = DEFAULT_OUTPUT) -> Dict[str, object]:
    rng = random.Random(seed)
    store = BusinessStateStore(str(output))
    start_day = date(2026, 2, 1)

    catalog: Dict[str, Dict[str, object]] = {}
    for idx, (name, unit, cost_min, cost_max) in enumerate(SKU_TEMPLATES, start=1):
        sku_id = f"sku_{idx:03d}"
        catalog[sku_id] = {
            "sku_id": sku_id,
            "aliases": [name, name.split(" ")[0]],
            "unit": unit,
            "default_sale_price": _money(rng, cost_min * 1.35, cost_max * 1.65),
            "created_utc": _iso(start_day, 8, 15),
        }
        opening_stock = _money(rng, 18, 34)
        store.adjust_inventory(
            name,
            qty_delta=opening_stock,
            reason="opening_stock",
            unit=unit,
            source="synthetic_seed",
            source_id=f"seed-open-{sku_id}",
        )
        restock_qty = _money(rng, 8, 20)
        store.adjust_inventory(
            name,
            qty_delta=restock_qty,
            reason="restock",
            unit=unit,
            source="synthetic_seed",
            source_id=f"seed-restock-{sku_id}",
        )

    state = store.load()
    state["catalog"] = catalog
    state["business_metadata"]["notes"] = "synthetic fixture for MVP-lite"
    store.save(state)

    # customers + utang ledger rows
    for customer in CUSTOMERS:
        rows = _build_ledger_rows(rng, customer, start_day)
        store.upsert_customer_ledger(
            customer_name=customer,
            rows=rows,
            source="synthetic_seed",
            source_id=f"seed-utang-{_slug(customer)}",
        )

    # cash snapshots
    cash = 1200.0
    for i in range(10):
        day = start_day + timedelta(days=i * 4)
        cash = max(200.0, cash + _money(rng, -90.0, 210.0))
        store.add_cash_snapshot(
            cash_amount=cash,
            source="synthetic_seed",
            source_id=f"seed-cash-{i+1:03d}",
            note="synthetic daily cash check",
        )

    # loans
    for lender, _kind in [
        ("Ate Letty", "short_term"),
        ("Mang Bert", "rolling"),
        ("Sister Finance", "weekly"),
    ]:
        store.add_loan(
            lender,
            principal=_money(rng, 1000, 2200),
            interest_rate=_money(rng, 2.5, 6.5),
            installment_amount=_money(rng, 80, 240),
            next_due_date=(start_day + timedelta(days=rng.randint(8, 25))).isoformat(),
            source="synthetic_seed",
            source_id=f"seed-loan-{_slug(lender)}",
        )

    # supplier offers
    for idx, (supplier_name, supplier_type) in enumerate(SUPPLIERS, start=1):
        sku_name, unit, _, _ = SKU_TEMPLATES[idx % len(SKU_TEMPLATES)]
        store.add_offer(
            supplier_name=supplier_name,
            sku_name=sku_name,
            unit_price=_money(rng, 5.0, 12.0),
            supplier_type=supplier_type,
            unit=unit,
            source="synthetic_seed",
            source_id=f"seed-offer-{idx:03d}",
            effective_date=(start_day + timedelta(days=idx)).isoformat(),
        )

    # sales
    for sale_idx in range(60):
        lines: List[Dict[str, object]] = []
        for _ in range(rng.randint(1, 3)):
            sku_name, _unit, _lo, _hi = rng.choice(SKU_TEMPLATES)
            qty = float(rng.randint(1, 5))
            price = _money(rng, 4.0, 18.0)
            lines.append(
                {
                    "sku_name": sku_name,
                    "qty": qty,
                    "price": price,
                    "raw": f"synthetic sale #{sale_idx + 1}",
                }
            )
        store.add_sale_record(
            draft_id=f"seed-sale-{sale_idx + 1:03d}",
            source="text" if rng.random() < 0.65 else "photo",
            raw=f"synthetic-sale-{sale_idx + 1}",
            lines=lines,
            paid_cash=(rng.random() < 0.85),
            customer_name=rng.choice([None] + CUSTOMERS),
        )

    # derived insights
    state = store.load()
    cash_latest = state.get("cash", {}).get("latest_snapshot") or {}
    open_cash = float(cash_latest.get("cash_amount", 0.0))
    utang_open = sum(
        float(cust.get("current_balance", 0.0)) for cust in state.get("customers", {}).values() if isinstance(cust, dict)
    )
    loan_outstanding = sum(
        float(loan.get("current_balance", 0.0)) for loan in state.get("loans", {}).values() if isinstance(loan, dict)
    )
    weekly_sales = sum(float(sale.get("total", 0.0) or 0.0) for sale in state.get("sales", []))
    state["insights"].append(
        {
            "period_key": "week_2026_02",
            "cash_on_hand": open_cash,
            "inventory_cost_estimate": 0.0,
            "utang_open": round(utang_open, 2),
            "loan_outstanding": round(loan_outstanding, 2),
            "gross_sales": round(weekly_sales, 2),
            "gross_cost": round(weekly_sales * 0.62, 2),
            "net_position": round(open_cash - loan_outstanding + utang_open * 0.15, 2),
            "trend_signal": "up" if weekly_sales > 2500 else "flat",
            "explain": "Synthetic benchmark snapshot for lightweight insights.",
            "recorded_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        }
    )
    store.save(state)
    return state


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate synthetic business_state.json fixture.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="Output file path.")
    parser.add_argument("--seed", type=int, default=SEED, help="Deterministic seed.")
    args = parser.parse_args()

    output = args.output
    if output.suffix != ".json":
        output = output / "business_state.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    generate(seed=args.seed, output=output)
    print(f"Generated synthetic data -> {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
