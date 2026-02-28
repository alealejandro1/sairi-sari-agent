# sairi-sari-agent

Telegram-first digital assistant for sari-sari shop operations.

## Current scope (MVP-lite)
This repository is currently focused on a strict minimum feature set:
- Capture operations from Telegram text and photos.
- Confirm parsed records before posting.
- Track inventory updates from shelf photos.
- Track supplier offers (agent vs wholesaler) and sale prices.
- Generate a simple wholesaler trip-prep list.

English-only for this version.

## Why this scope
The target users operate with pen-and-paper workflows and limited time. The MVP-lite is intentionally narrow to ship quickly and provide immediate value without spreadsheet behavior.

## Key docs
- `docs/product-requirements.md`: MVP-lite source of truth.
- `docs/domain-model.md`: data boundaries and invariants.
- `docs/thread-charter.md`: how to split work across Codex threads.
- `docs/synthetic-data.md`: synthetic dataset schema and usage.

## Telegram bot buttons and features

The bot exposes a persistent keyboard with these commands:

- `/start`
  - Starts/returns to normal intake mode.
  - Accepts text like `item qty price` (example: `soap 2 15`) and also photos.
  - Builds a draft for confirmation before posting.
  - If started by mistake, use `/cancel`.
- `/ledger`
  - Marks the next uploaded photo as a ledger page.
  - Replies with a ledger placeholder draft and marks it as OCR-ledger flow.
  - If started by mistake, use `/cancel`.
- `/cancel`
  - Clears the active in-progress action (`next_photo_mode` or latest active draft).
  - Use this anytime after accidental taps or wrong starts.
- `/insights`
  - Returns a quick summary of:
    - tracked stock summary
    - total sales count
    - total sales value
- `/debtors`
  - Shows the list of people who owe money, including:
    - person name
    - amount owed
    - since date
- `/recent10`
  - Shows the latest 10 confirmed transaction log entries.
  - Useful for quick review of recent intake posts.

## Synthetic data
Generate deterministic test data:

```bash
python3 scripts/generate_synthetic_data.py
```

Generated files are written to `data/synthetic/`.

## Development with Codex
- Keep each thread focused on one track.
- Map every implementation PR to a requirement in the PRD.
- Prefer small, testable increments.

## License
MIT
