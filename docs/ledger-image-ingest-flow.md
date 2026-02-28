# Utang Ledger Image Intake Flow (Confirm-first)

This flow documents what happens today when a shopkeeper sends a ledger photo.

## Step-by-step runtime path

1. User sends `/ledger`
   - Handler: `on_ledger`
   - Action: sets `context.user_data["next_photo_mode"] = "ledger"`
   - No write yet.

2. User uploads the photo
   - Handler: `on_photo`
   - If mode is `ledger`, flow goes to ledger OCR path.

3. OCR extraction
   - `_extract_ledger_text(message)` downloads the photo to a temp file.
   - `extract_text_from_image(temp_path)` is called in `ledger_ocr`.
   - On failure: a pending OCR draft is created and user is prompted to retry.

4. Parse ledger text
   - `parse_ledger_ocr_text(ocr_text)` runs.
   - It returns:
     - normalized `customer_name`
     - parsed `entries[]` (`date`, `entry_kind`, `note`, `amount`, `running_balance`, `raw_lines`, confidence)
     - parser `warnings`

5. Draft creation
   - Bot stores a draft:
     - `source: "ledger"`
     - `status: "pending"`
     - `ocr_mode: "ledger"`
     - `customer_name`, `parsed`, `lines` (entries)
   - The draft id is shown like `ledger-0001`.
   - User sees formatted preview from `format_ledger_draft`.

6. User confirmation required (important)
   - Inline buttons:
     - `✅ Confirm`
     - `📝 Edit`
     - `❌ Cancel`
   - **Nothing is persisted until Confirm.**

7. Confirm callback
   - `on_callback` with `action == confirm` calls `_persist_ledger_draft(context, draft_id, draft)`.
   - `_persist_ledger_draft` validates:
     - draft source is `ledger`
     - parsed rows exist
   - Writer called: `BusinessStateStore.upsert_customer_ledger(...)`

8. JSON write
   - `upsert_customer_ledger` appends/merges rows under:
     - `business_state["customers"][customer_key]["entries"]`
   - Updates:
     - customer running/balances
     - `current_balance`
     - `last_updated_utc`
   - Adds an `ingestion_log` event with:
     - `kind: "ledger_posted"`
     - `source`, `source_id`, `customer_key`, `entries_added`
   - Writes file atomically via `BusinessStateStore.save()`.

## Result in JSON

After confirm, the store contains (example shape):

```json
{
  "customers": {
    "maria_next_door": {
      "entries": [
        {
          "date": "2026-03-03",
          "entry_kind": "credit_sale",
          "note": "2 Marlboro, Mang Tomas",
          "amount": 47,
          "running_balance": 47,
          "source": "telegram_photo_ledger",
          "source_id": "ledger-0001"
        }
      ],
      "current_balance": 47
    }
  },
  "ingestion_log": [
    {
      "event_id": "ing-...",
      "recorded_at": "2026-03-03T00:00:00Z",
      "kind": "ledger_posted",
      "source_id": "ledger-0001"
    }
  ]
}
```

## Why this meets your request

- Lightweight: still one JSON file.
- No automatic posting: all writes are manual-confirmed.
- Recoverable: all raw OCR text is kept in draft before confirm (and can be re-sent/corrected later).
- Auditable: every confirmed write gets an event in `ingestion_log`.
