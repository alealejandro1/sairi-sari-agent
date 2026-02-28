# sairi-sari-agent

Telegram-first digital assistant for sari-sari shop operations.

## Current scope (Utang-focused MVP)
This repository is now focused on Utang ledger extraction and reporting:
- Capture household ledger pages from Telegram photos (`/ledger`).
- Review parsed rows and confirm before saving.
- Produce two Utang reports:
  - `/insights` for quick risk and concentration snapshot.
  - `/debtors` for person-level outstanding, consumed, and payment staleness.
- Keep a single JSON business state file.

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
- `docs/telegram-board-dev.md`: Telegram board run and reload instructions.

Current default persistence is a single file:
- `data/business_state.json` via `BUSINESS_STATE_STORE_PATH`.

If you still need the old dedicated utang file during transition:
- `data/utang_ledger_store.json` via `UTANG_LEDGER_STORE_PATH`.

## Telegram bot buttons and features

The bot exposes a persistent keyboard "board" with these commands:

- `/start`
  - Shows the active Utang workflow and available commands.
  - If started by mistake, use `/cancel`.
- `/ledger`
  - Marks the next uploaded photo as a household ledger page.
  - Supports the Utang Ledger format (date, shorthand notes, amount, running balance).
  - Replies with a ledger placeholder draft and marks it as OCR-ledger flow.
  - If started by mistake, use `/cancel`.
- `/cancel`
  - Clears the active in-progress action (`next_photo_mode` or latest active draft).
  - Use this anytime after accidental taps or wrong starts.
- `/insights`
  - Returns a fast Utang risk summary (open balances, concentration, and watchlist).
- `/debtors`
  - Shows the list of people who owe money, including:
    - person name
    - amount owed
    - since date

The keyboard is the primary control board for Utang-only flow and reports.

## Telegram board

Ledger photo OCR remains on the existing PaddleOCR path in `ledger_ocr.py`.

### One-time run

```bash
python3 -m pip install python-telegram-bot python-dotenv
# Install PaddleOCR if you run on systems that support it.
python3 -m pip install paddleocr
cp .env.example .env
python3 src/main.py bot
```

Use `/start` in Telegram to open the board keyboard.

### Live-reload while editing

- Use `scripts/run_telegram_board.py` to auto-restart on source changes in `src/` and `scripts/`.
- The launcher watches Python file mtimes and restarts the bot whenever code changes.

```bash
python3 scripts/run_telegram_board.py
```

Keep this running in its own terminal while you make edits.

If you encounter certificate errors from your environment (`self-signed certificate in certificate chain`), run:

```bash
TELEGRAM_INSECURE_TLS=1 python3 scripts/run_telegram_board.py
```

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
