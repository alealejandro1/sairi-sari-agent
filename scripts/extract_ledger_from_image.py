#!/usr/bin/env python3
"""CLI helper for extracting a utang ledger image into structured rows."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.append(os.path.join(os.path.dirname(__file__), "..", "src"))

from ledger_ocr import UtangLedgerStore, extract_text_from_image, parse_ledger_ocr_text


def main() -> int:
    parser = argparse.ArgumentParser(description="Extract utang ledger rows from an image")
    parser.add_argument("image", type=Path, help="Path to a ledger photo")
    parser.add_argument(
        "--customer",
        help="Optional customer name override if header text is unreadable",
    )
    parser.add_argument(
        "--store",
        default="data/utang_ledger_store.json",
        help="Ledger store JSON path",
    )
    parser.add_argument(
        "--no-store",
        action="store_true",
        help="Parse only, do not persist.",
    )
    args = parser.parse_args()

    if not args.image.exists():
        print(f"Image not found: {args.image}", file=sys.stderr)
        return 1

    text = extract_text_from_image(str(args.image))
    parsed = parse_ledger_ocr_text(text, customer_name_hint=args.customer)

    print(json.dumps(parsed, ensure_ascii=False, indent=2))

    if args.no_store:
        return 0

    store = UtangLedgerStore(args.store)
    result = store.upsert_ledger(
        customer_name=str(parsed.get("customer_name", "Unknown")),
        entries=parsed.get("entries", []),
        source=f"image:{args.image.name}",
        source_id=str(args.image),
    )
    print(f"Stored: {result}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
