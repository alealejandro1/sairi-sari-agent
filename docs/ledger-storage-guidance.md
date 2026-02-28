# Utang Ledger Storage Guidance (MVP)

## Recommendation for one user and hundreds–thousands of rows

For your current scale, the smallest reliable option is a single JSON file:
- Very low setup cost.
- No schema migration burden.
- Human-readable, easy to inspect and hand-fix.
- Fits well with one-shop / one-user assumption.

Store shape in this implementation:
- `data/business_state.json` (configurable via `BUSINESS_STATE_STORE_PATH`)
- Sections: `customers`, `inventory`, `cash`, `loans`, `offers`, `catalog`, `sales`, `insights`, `ingestion_log`

## JSON schema used by this repo (v1)

```json
{
  "version": 1,
  "updated_utc": "2026-02-28T00:00:00Z",
  "business_metadata": {
    "owner_name": "owner",
    "notes": "lightweight single-shop snapshot"
  },
  "customers": {
    "ate_nena": {
      "customer_name": "Ate Nena",
      "entries": [
        {
          "date": "2026-03-03",
          "entry_kind": "credit_sale",
          "note": "2 Marbobo box 20s,5 KopiPawa",
          "amount": 241.85,
          "running_balance": 241.85,
          "raw_lines": ["Mar 3 ...", "2 Marbobo box..."],
          "confidence": 0.95,
          "source": "telegram_photo_ledger",
          "source_id": "ledger-0001"
        }
      ],
      "current_balance": 241.85,
      "first_seen_utc": "2026-03-03T00:00:00Z",
      "last_updated_utc": "2026-03-03T00:00:00Z",
      "is_irrecoverable": false
    }
  },
  "inventory": {},
  "cash": {
    "snapshots": [],
    "latest_snapshot": null
  },
  "loans": {},
  "offers": [],
  "catalog": {},
  "sales": [],
  "insights": [],
  "ingestion_log": []
}
```

## Practical migration path later

1. Keep parser and store interface unchanged (`parse_ledger_ocr_text`, `BusinessStateStore` methods).
2. Replace JSON persistence with SQLite by implementing the same methods against `sqlite3`.
3. Add uniqueness checks on `(customer_key, date, amount, running_balance, source_id)` to avoid duplicate uploads.
4. Add query indexes for weekly insights (`SUM`, `GROUP BY customer_key`).

## Write lifecycle in current bot

- Ledger photo text is parsed into rows first, then shown as a draft.
- Data is written to JSON only when the user presses **Confirm**.
- If the user cancels or abandons the draft, no ledger rows are persisted.

## Why not start with Postgres right now

- Adds deployment cost and operational overhead.
- Too much ceremony for a single operator.
- Not needed for projected load (< a few thousand rows total).
