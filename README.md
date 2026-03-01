# sairi-sari-agent

Telegram-first digital assistant for small, Filipino sari-sari shop operators who still rely on manual credit books.

## Why this project matters

Sari-sari operators often track credit in paper ledgers that are hard to audit, update, and act on during busy hours. The business risks are real:

- Missed collections and delayed follow-up because debt history is scattered across notebook pages.
- No quick visibility into who owes the most or who has carried debt the longest.
- Slow manual reconciliation during stock and cash planning, even when operations are already under pressure.

This project turns a physical **UTANG LEDGER** workflow into a structured, fast Telegram workflow so owners can:

- Record credit entries in seconds from a ledger photo.
- See risk-priority summaries before heading to the store or making calls.
- Prioritize collection actions with debtor-level reminders and carried-debt context.

## What is a UTANG LEDGER?

In this repo, an **UTANG LEDGER** is the household credit tracking record a shop owner keeps for items sold on credit:

- Each row typically has a date, running balance, itemized notes, and payment activity.
- Entries are often difficult to aggregate manually because they are paper-based and repeat across multiple photos over time.
- The bot extracts these rows from photos and keeps a clean, queryable state so reporting is immediate.

The outcome is not a full accounting system; it is a focused debt-tracking assistant for small retail owners operating in a high-friction, low-time environment.

## Current scope (Utang-focused MVP)

This repository is now focused on Utang ledger extraction and reporting:

- Capture household ledger pages from Telegram photos (`/ledger`).
- Review parsed rows and confirm before saving.
- Produce two Utang reports:
  - `/insights` for quick risk and concentration snapshot.
  - `/debtors` for person-level outstanding, consumed, and payment staleness.
- Keep a single JSON business state file.

English-only for this version.

## Demo

Watch a walkthrough of the full flow:

- [YouTube Demo: Sairi Sari Agent](https://youtu.be/bU-kSxXDwD4)

## Built in the OpenAI Codex Hackathon context

This project was developed during:

- **OpenAI Codex Hackathon - Singapore**
- **Date:** Saturday, February 28, 2026
- **Time:** 9:30 AM – 8:30 PM
- **Location:** Lorong AI @ One-North, Singapore

Event context highlights:

- Co-hosted with Lorong AI and 65Labs.
- Designed for builders shipping production-grade code with AI assistants.
- Focused tracks included:
  - Agentic coding workflows
  - UX for agentic applications
  - Multimodal intelligence
  - Domain agents
  - Building evals
- Prizes:
  - 1st: $30,000 USD in API credits
  - 2nd: $15,000 USD in API credits
  - 3rd: $5,000 USD in API credits
  - Top 5 teams: one year of ChatGPT Pro each

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

## Data

Current default persistence is a single file:
- `data/business_state.json` via `BUSINESS_STATE_STORE_PATH`.

If you still need the old dedicated utang file during transition:
- `data/utang_ledger_store.json` via `UTANG_LEDGER_STORE_PATH`.

## Key docs

- `docs/product-requirements.md`: source of truth for MVP scope.
- `docs/domain-model.md`: data boundaries and invariants.
- `docs/thread-charter.md`: how we split work across Codex threads.
- `docs/synthetic-data.md`: synthetic dataset schema and usage.
- `docs/ocr-analysis-utang-ledger.md`: OCR parsing notes from sample utang ledger photos.
- `docs/ledger-storage-guidance.md`: JSON-first storage recommendation for one-user scale.
- `docs/feature-data-requirements.md`: required fields for lightweight but complete insights.
- `docs/ledger-image-ingest-flow.md`: confirm-first runtime flow for utang ledger photos.
- `docs/deployment-requirements.md`: local and cloud hardware requirements for MVP-lite.
- `docs/telegram-board-dev.md`: Telegram board run and reload instructions.

## Synthetic data

Generate deterministic test data:

```bash
python3 scripts/generate_synthetic_data.py
```

Generated files are written to `data/synthetic/`.

## Development with Codex

- Keep each thread focused on one track.
- Map each implementation task to a requirement in the PRD.
- Prefer small, testable increments.

## License

MIT
