# sairi-sari-agent

Digital twin for a rural Philippine sari-sari store.

## Vision
`sairi-sari-agent` is an agentic assistant that communicates via Telegram and helps store owners who are not financially literate maintain day-to-day accounting records without spreadsheets.

## Core goals
- Capture sales, expenses, inventory, and utang (credit) through simple chat.
- Convert conversational inputs into structured bookkeeping entries.
- Provide plain-language daily/weekly financial summaries.
- Enable a digital twin simulation for forecasting and recommendations.

## Planned architecture
- `src/bot`: Telegram interface + command handlers.
- `src/accounting`: Ledger and bookkeeping domain logic.
- `src/api`: Internal API layer for integrations and dashboards.
- `docs`: Product notes, domain glossary, and roadmap.

## Quickstart
1. Install Python 3.11+.
2. Create a virtual environment.
3. Install dependencies (to be added in `requirements.txt`).
4. Copy `.env.example` to `.env` and configure values.
5. Run the bot entrypoint (to be added in `src/main.py`).

## Suggested first milestones
1. Telegram message ingestion and auth.
2. Natural-language transaction parser (Tagalog/English mix).
3. Double-entry ledger model with simple reports.
4. Weekly summary and anomaly alerts.
5. Digital twin simulation for pricing and stock decisions.

## Development with Codex
- Ask Codex for single, testable increments (e.g., "Implement ledger posting with unit tests").
- Ask Codex to explain tradeoffs before major architecture changes.
- Keep prompts anchored to files and outcomes.

## License
MIT
