# Product Requirements Document (MVP-Lite)

## Product name
`sairi-sari-agent`

## Goal for this build
Ship the smallest useful Telegram assistant that helps a sari-sari shop digitize daily operations from chat and photos.

## Delivery constraints
- Very limited founder time.
- English-only interface for this version.
- No full cash-reconciliation module.
- Owner cash check-ins are treated as authoritative where available.

## Core problem to solve
Shopkeepers record operations on paper and in memory. Credit, sales, and cash still live in separate books, so the shopkeeper cannot answer simple business questions such as “Did I make money this week?”. Visibility into stock and profit drivers stays fragmented.

## MVP-lite feature set (must-have)

### M1: Multimodal intake with confirmation
- Input types: Telegram text and Telegram photo.
- Supported photo intents:
  - Notebook page photo for operations ingestion.
- System must reply with a confirmation prompt before posting:
  - "Is this what you meant?"
  - list of interpreted items + quantities + prices.
  - quick actions: confirm, edit item, delete item, cancel.

### M2: Inventory snapshot updates
- Track on-hand quantity for a focused SKU list (initially top 30 SKUs).
- Allow add/remove/adjust updates from:
  - confirmed sales posting
  - confirmed restocks
  - manual correction entries
- Keep event history of every stock change.

### M3: Price book and consistency tracking
- Store and track recent purchase estimates by SKU.
- Store and track recent selling prices by SKU from logged sales.
- Surface two simple checks:
  - margin warning when sale price <= recent purchase estimate.
  - consistency warning when sale price deviates from usual range.

### M4: Sales record capture
- Record sales from text input and notebook-page photo parsing.
- Minimum sale record fields:
  - datetime
  - sku
  - quantity
  - unit sale price
  - source (chat or notebook photo)
- Journal-style sales photos may cover 1-4 dates per photo.
- Maximum 20 sale lines per photo.
- If only date, item name, and price are readable, quantity defaults to 1 and user can adjust.

### M5: Utang ledger (primary credit source of truth)
- Keep a customer/household registry and one utang ledger page per household.
- Parse ledger lines in the shopkeeper's existing format:
  - date
  - shorthand note / item memory cue
  - signed amount (`BAYAD` or equivalent payment lines reduce balance)
  - running balance
- This is the primary way to capture credit activity in MVP-lite and replaces the per-transaction credit ledger ambition.
- Use OCR analysis examples in `docs/ocr-analysis-utang-ledger.md` as the parsing baseline.
- Allow marking a household/account as `irrecoverable` when collection is unlikely.
- If one purchase has mixed cash and credit lines, the system can capture credit lines in the utang ledger and keep cash lines in standard sales records.

### M6: Net worth health
- Track business health and weekly money-making direction from:
  - reported cash
  - gross sales versus estimated cost of goods sold
  - inventory trend at estimated cost
  - open/closed utang exposure
- Flag a sustained decline and provide a weekly signal suitable for answering: “Did I make money this week?”

### M6a: Moment-based insight prompts
- Provide insight cards that can be run at specific moments of the operator day:
  - opening / start-of-day
  - midday check
- prep before stock purchase trip
  - after restock planning update
  - end-of-day close
  - repayment deadline check
  - weekly trend review
- Each moment should output:
  - a short risk signal (red/amber/green),
  - one to three actionable next steps,
  - direct button path back to trigger other moments or quick helpers (`/cash`, `/debtors`, `/insight_*`).
- These should be triggerable at any time from the Telegram quick keyboard board (moment buttons are always visible, not tied to automatic time-of-day checks).

## Explicitly out of scope for MVP-lite
- Full accounting stack and formal financial statements.
- Cash count/reconciliation workflow.
- Advanced debt-collection workflow.
- Automatic debt write-off workflow.
- Advanced autonomous recommendations.
- Multi-language support.
- Multi-store support.

## Primary user flows

### Flow A: Cash check update
1. User sends current cash on hand.
2. Bot records a cash snapshot with timestamp.
3. Bot uses the reported cash value when computing recommendations and warnings.

### Flow B: Notebook page to sales records
1. User sends notebook page photo.
2. System extracts line items as draft sales.
3. User confirms or edits.
4. Sales records are posted and linked to source photo.

## Data requirements (minimum)
- Product catalog (SKU id, name, unit, reorder point).
- Inventory events.
- Sales records.
- Customer registry.
- Utang ledger pages and line items (with running balances).
- Photo ingestion records with parsed output and confirmation status.
- Cash snapshots from user input.
- Health snapshots (cash trend + utang exposure + weekly net result signal).

## Non-functional requirements
- Common confirmation flow must complete in 45 seconds or less.
- Every posted record must reference its source and timestamp.
- No destructive overwrite; corrections append an adjustment event.
- Telegram retries must not duplicate confirmed postings.

## MVP-lite acceptance criteria
- A notebook page photo can produce a confirmed sales bundle.
- A notebook page photo can produce confirmed sales records.
- Bot can produce a restock plan from current stock and reorder targets.
- Margin and price-consistency warnings are shown on new sales.
- Irrecoverable credit amounts are reported and excluded from expected cash recovery.
- Health trend is visible by day and week; “Did I make money this week?” should be answered from all available sources.

## Success criteria for early pilot (30 days)
- At least 50% of operations captured digitally (chat or photo).
- At least 80% of posted events confirmed by user without manual re-entry.
- At least one restock decision per week uses bot-generated prep list.
