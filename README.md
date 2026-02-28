# sairi-sari-agent

Telegram-first digital assistant for sari-sari shop operations.

## Current scope (MVP-lite)
This repository is currently focused on a strict minimum feature set:
- Capture operations from Telegram text and photos.
- Confirm parsed records before posting.
- Track inventory updates from shelf photos.
- Track supplier offers (agent vs wholesaler) and sale prices.
- Generate a simple wholesaler trip-prep list.
- Capture one Utang Ledger per household (not a full transaction ledger).
- Keep a single JSON business state file for customers, cash, loans, inventory, offers, and sales.

English-only for this version.

## Why this scope
The target users operate with pen-and-paper workflows and limited time. The MVP-lite is intentionally narrow to ship quickly and provide immediate value without spreadsheet behavior.

## Key docs
- `docs/product-requirements.md`: MVP-lite source of truth.
- `docs/domain-model.md`: data boundaries and invariants.
- `docs/thread-charter.md`: how to split work across Codex threads.
- `docs/synthetic-data.md`: synthetic dataset schema and usage.
- `docs/ocr-analysis-utang-ledger.md`: OCR parsing notes from sample utang ledger photos.
- `docs/ledger-storage-guidance.md`: JSON-first storage recommendation for one-user scale.
- `docs/feature-data-requirements.md`: what we need to store for lightweight but complete insights.
- `docs/ledger-image-ingest-flow.md`: exact confirm-first runtime flow for utang ledger photos.
- `docs/deployment-requirements.md`: local and cloud hardware requirements for MVP-lite.

Current default persistence is a single file:
- `data/business_state.json` via `BUSINESS_STATE_STORE_PATH`.

If you still need the old dedicated utang file during transition:
- `data/utang_ledger_store.json` via `UTANG_LEDGER_STORE_PATH`.

## Telegram bot buttons and features

The bot exposes a persistent keyboard "board" with these commands:

- `/start`
  - Starts/returns to normal intake mode.
  - Accepts text like `item qty price` (example: `soap 2 15`) and also photos.
  - Builds a draft for confirmation before posting.
  - If started by mistake, use `/cancel`.
- `/cash <amount>`
  - Record shop cash on hand snapshot as a confirmed draft.
  - Example: `/cash 1450`.
- `/loan <lender> <amount>`
  - Record shop borrowings with confirmation before write.
  - Example: `/loan "Ate Nena" 1500 3 2026-03-28`.
- `/stock <item> <qty_delta>`
  - Record manual stock adjustment with confirmation before write.
  - Positive qty for add, negative for remove.
  - Example: `/stock margarine 20`.
- `/ledger`
  - Marks the next uploaded photo as a household ledger page.
  - Supports the Utang Ledger format (date, shorthand notes, amount, running balance).
  - Replies with a ledger placeholder draft and marks it as OCR-ledger flow.
  - If started by mistake, use `/cancel`.
- `/cancel`
  - Clears the active in-progress action (`next_photo_mode` or latest active draft).
  - Use this anytime after accidental taps or wrong starts.
- `/insights`
  - Returns an easy-to-read business snapshot.
  - Answers “Did I make money this week?”
  - Shows cash movement, ledger exposure, and sales/stock health direction.
- `/insight_open`
  - Opening / start-of-day health card for liquidity and readiness.
- `/insight_midday`
  - Midday stock pressure and pacing card.
- `/insight_visit`
  - Pre-wholesaler visit prep card.
- `/insight_supplier`
  - Supplier-offer response card.
- `/insight_close`
  - End-of-day close and collections follow-up card.
- `/insight_due`
  - Loan repayment pressure card.
- `/insight_week`
  - Weekly trend card.
- `/debtors`
  - Shows the list of people who owe money, including:
    - person name
    - amount owed
    - since date
- `/recent10`
  - Shows the latest 10 confirmed log entries (sales + utang ledger updates).
  - Useful for quick review of recent intake posts.

The keyboard is the primary control board for insights: these moment checks are pinned as board buttons so operators can trigger them anytime, regardless of day period.

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
