# Domain Model and Data Boundaries (MVP-Lite)

## Primary entities
- Shop: tenant boundary for all records.
- User: authorized Telegram participant.
- Product: sellable stock unit (SKU).
- Supplier: source of purchase offers (`agent` or `wholesaler`).
- SupplierOffer: offered purchase price for a SKU at a point in time.
- InventoryEvent: append-only stock movement (`add`, `remove`, `adjust`).
- SaleRecord: posted sale line item.
- Customer: person or household with credit standing.
- UtangLedgerPage: one page per customer/household credit ledger.
- UtangLedgerEntry: credit line with note, amount, and running balance.
- NetWorthSnapshot: daily business value signal.
- PhotoIngestion: received image, parsed draft payload, and confirmation status.
- PriceSignal: derived warning outcome for margin and consistency.
- HealthInsightSnapshot: weekly business-health signal (cash flow, gross margin direction, utang trend).

## Event types
- `inventory_photo_draft`
- `inventory_adjust_confirmed`
- `notebook_sales_draft`
- `sales_record_confirmed`
- `supplier_offer_draft`
- `supplier_offer_confirmed`
- `utang_ledger_draft`
- `utang_ledger_posted`
- `customer_credit_write_off`
- `ledger_payment_adjustment`
- `manual_edit`

## Required relationships
- `SaleRecord.sku_id -> Product.id`
- `SupplierOffer.sku_id -> Product.id`
- `SupplierOffer.supplier_id -> Supplier.id`
- `InventoryEvent.sku_id -> Product.id`
- `UtangLedgerPage.customer_id -> Customer.id`
- `UtangLedgerEntry.page_id -> UtangLedgerPage.id`
- `UtangLedgerEntry.photo_ingestion_id -> PhotoIngestion.id`
- `PhotoIngestion` links to created records via `source_photo_id`

## Core invariants
- Confirmed events are append-only; edits create a new event.
- Every inventory change has `sku_id`, `quantity_delta`, and reason/source.
- Every sale has `quantity` and `unit_sale_price`.
- Every supplier offer has `offered_unit_cost` and `effective_date`.
- Every derived margin check references a specific sale and latest known cost.
- Every utang ledger entry has `customer_id` (via page), `line_amount`, `running_balance`, `entry_kind` (`credit_sale`/`payment`), and optional `line_note`.
- Utang health is derived from ledger balances plus cash/sales context, not from a per-transaction ledger.
- Net worth is computed as `inventory_cost + cash - irrecoverable_debt` for MVP-lite, with weekly health delta derived from snapshots.

## Data boundaries
- Bot layer: message/photo intake and user confirmation UX.
- Extraction layer: OCR and item parsing to structured drafts.
- Core data layer: products, inventory, sales, suppliers, offers, customers, utang ledger pages/entries, cash/owner check-ins, health snapshots.
- Insight layer: margin warning, price consistency warning, weekly health trend, wholesaler prep list.

## Deferred entities (not in MVP-lite)
- Credit customer entities.
- Full double-entry ledger entities.
- Loan tracking entities.
