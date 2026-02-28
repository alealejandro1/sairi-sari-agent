# Data requirements for lightweight, shopkeeper-friendly operation

## Design principle
Collect only what helps the weekly decisions. The shopkeeper should only type 2–3 simple things:
- send ledger pages
- send occasional cash snapshots
- send occasional short text or photo events for restocks/loans/offers

Everything else should be inferred, defaulted, or filled from existing rows.

## 1) Customer utang ledger
Core goal: know total exposure and running balance per household/customer.

Required to persist:
- `customer_name` (as shown in ledger title)
- `entries[]` per customer, each entry:
  - `date`
  - `amount`
  - `entry_kind` (`credit_sale` | `payment`)
  - `running_balance` (preferred)
  - `note` (free text memory note)
  - optional `confidence`, `raw_lines`, `source`, `source_id`

Minimal capture:
- OCR from utang photos only.
- User confirmation before final write.

Derived by system:
- `current_balance`
- `days_open` from first unpaid row date
- `risk_flags` (optional: no payment in N days, balance growth)

## 2) Inventory
Core goal: know enough stock to propose restock actions.

Required to persist:
- `sku_id`
- `display_name`
- `unit`
- `on_hand_qty`
- `events[]`:
  - `date`
  - `type` (`add` | `remove` | `adjust`)
  - `qty`
  - `reason` (`sale`, `restock`, `manual`, `correction`)
  - optional `source`, `source_id`

Minimal capture:
- OCR from stock photos (future) + ledger-sales lines can optionally decrement stock.
- Cash sales captured from text/photos can reduce stock immediately.
- Restock update from one number input: `SKU qty cost` (lightweight fallback) or future photo OCR.

Derived by system:
- low-stock flags (`on_hand_qty <= reorder point`)
- stock trend from recent add/remove history.

## 3) Cash ledger
Core goal: know what is actually available now.

Required to persist:
- `cash_snapshot_id`
- `date`
- `cash_amount`
- `source` (`user_input` preferred)

Minimal capture:
- one `/cash` text command (can default to “last value” if skipped).
- no reconciliation workflow in MVP-lite.

Derived by system:
- daily cash trend
- cash coverage ratio vs expected debt/payments.

## 4) Loans (shopkeeper debt)
Core goal: avoid pretending all liabilities are inventory.

Required to persist:
- `loan_id`
- `lender_name`
- `principal`
- `current_balance`
- `interest_rate_or_markup` (small default acceptable)
- `installment_amount` (or `repayment_rule` text)
- `next_due_date` (optional if unknown)
- `payments[]`:
  - `date`
  - `amount`
  - `note`

Minimal capture:
- single setup command after first loan appears
- follow-up update when shopkeeper repays.

Derived by system:
- next payout risk
- debt service burden.

## 5) Supplier offers / price lists
Core goal: compare restock options.

Required to persist:
- `supplier_name`
- `supplier_type` (`agent` | `wholesaler`)
- `sku_id` or `item_label`
- `unit_price`
- `effective_date`
- optional `source` (`photo`, `text`)

Minimal capture:
- parse photos into draft + user confirm.
- if OCR misses a few lines, allow quick manual correction.

Derived by system:
- effective best-known cost by SKU
- margin warning input for sale decisions.

## 6) Product catalog / SKU normalization
Core goal: avoid broken matching from OCR noise.

Required to persist:
- `sku_id`
- `aliases` (common misspellings/shortcuts)
- `unit` (piece, pack, sachet)
- `default_sale_price` (optional)

Minimal capture:
- small seeded catalog (top SKUs only) plus manual add when unknown item first appears.

## 7) Sales (including mixed cash/utang transactions)
Core goal: keep money flow and inventory consistent.

Required to persist:
- `transaction_id`
- `date`
- `lines[]`:
  - `sku_id`
  - `qty`
  - `unit_sale_price`
  - `paid_cash` (`true/false`)
  - optional `customer_name`

Minimal capture:
- text entry (`item qty price`) for quick cash sales.
- photo entries converted into draft lines and confirmed.

Derived by system:
- gross sales by period
- top movers
- gross margin proxy.

## 8) Insight snapshots
Core goal: answer “Did I make money this week?” quickly.

Required to persist:
- period key (`week_yyyy_mm_dd`)
- `cash_on_hand`
- `inventory_cost_estimate`
- `utang_open`
- `loan_outstanding`
- `gross_sales`
- `gross_cost`
- `net_position`
- `trend_signal` (`up|flat|down`)
- short `explain` text

Minimal capture:
- computed nightly or on-demand from other ledgers.

## What we should not ask the shopkeeper for
- No per-item OCR confidence review every time.
- No full reconciliation form.
- No tax/financial statement style fields.
- No duplicate manual ledger balancing.

Ask only:
1. cash snapshot (daily/major)
2. new loan details when loan occurs
3. occasional catalog fixes (if unknown item appears)
4. confirm OCR drafts (single confirmation flow).

## Suggested default JSON store shape

Keep everything in one lightweight store until scale grows:
- `data/business_state.json`
- grouped sections: `customers`, `inventory`, `cash`, `loans`, `offers`, `catalog`, `sales`, `insights`
- versioned document with append-only event arrays and computed latest states.

## Suggested implementation map (MVP-lite)

Minimum shopkeeper input paths:
- Utang ledger: `/ledger` photo draft + confirm.
- Cash check-in: `/cash` + confirm.
- Shop loans: `/loan` + confirm.
- Inventory adjustments: `/stock` + confirm.
- Supplier offers: to be added in next pass from offer OCR/text (`offers` section in store) + confirm.

## Fill-plan by feature (what to capture, what to infer)

To keep data entry lightweight, capture only these fields directly from the shopkeeper:

- Utang ledger pages
  - capture: photo + optional date/amount corrections during confirm
  - infer: running balance validity, oldest age, overdue-risk flags
- Cash on hand
  - capture: one number per check-in
  - infer: daily trend and weekly delta
- Loans to the shop
  - capture: lender name and principal, optional interest/due
  - infer: outstanding total + risk bucket
- Inventory
  - capture: manual `/stock sku qty reason`
  - infer: stock status (`low`, `reorder`), movement totals
- Sales
  - capture: quick text lines `item qty price` or confirmed photos
  - infer: weekly gross total, top movers, average basket size
- Supplier offers (future)
  - capture: supplier name, SKU shorthand, unit price per item
  - infer: cheapest supplier, restock recommendations

Missing but useful later:
- Supplier offer ingestion and ranking logic.
- Loan payment entries (to track repayments against principal).
- Stock cost basis per SKU for stronger margin.
- Return/spoilage adjustments.
- Customer-level delinquency flags and reminders.
