# Business State JSON model (v1)

Single-file data store used by the current lightweight MVP:
- Default path: `data/business_state.json`
- Override: `BUSINESS_STATE_STORE_PATH`

## Root document

- `version`: schema version number (starts at `1`)
- `updated_utc`: timestamp of the last successful write
- `business_metadata`: owner notes and defaults
- `customers`: house-by-household debt ledger rows
- `inventory`: current stock by SKU with adjustment events
- `cash`: manual cash snapshot history
- `loans`: shop borrowings / payable obligations
- `offers`: supplier offers captured later
- `catalog`: canonical SKU hints and aliases
- `sales`: confirmed cash sales and non-utang sales events
- `insights`: cached computed summaries (optional)
- `ingestion_log`: append-only write audit trail

## Section contracts

- `customers`
  - Key: normalized customer name (slug)
  - Values:
    - `customer_name`
    - `entries[]`:
      - `date` (ISO date if parseable)
      - `entry_kind` (`credit_sale` | `payment`)
      - `note` (free text memory cue)
      - `amount` (positive for credit, negative for `BAYAD`)
      - `running_balance`
      - `raw_lines`
      - `confidence`
      - `warnings`
      - `source`, `source_id`
    - `current_balance`
    - `first_seen_utc`, `last_updated_utc`
    - `is_irrecoverable` (default `false`)
  - Writes:
    - `/ledger` image -> draft -> confirm -> `upsert_customer_ledger`

- `cash`
  - `snapshots[]`:
    - `cash_snapshot_id`
    - `snapshot_utc`
    - `cash_amount`
    - `source`, `source_id`, `note`
  - `latest_snapshot`
  - Writes:
    - `/cash` -> confirm -> `add_cash_snapshot`

- `loans`
  - Key: normalized lender row key
  - Values:
    - `loan_id`, `lender_name`, `principal`, `current_balance`
    - `interest_rate`, `installment_amount`, `next_due_date`
    - `status`, `created_utc`, `payments[]`, `source`, `source_id`
  - Writes:
    - `/loan` -> confirm -> `add_loan`

- `inventory`
  - Key: normalized SKU id
  - Values:
    - `sku_id`, `display_name`, `unit`, `on_hand_qty`, `reorder_point`
    - `cost_estimate`
    - `events[]` (`inventory_event_id`, `qty_delta`, `reason`, `source`, `source_id`)
  - Writes:
    - `/stock` -> confirm -> `adjust_inventory`
    - later: sales integration (not required in this MVP-lite)

- `offers`
  - Values:
    - `offer_id`, `supplier_name`, `supplier_type`, `sku_id`, `sku_name`
    - `unit_price`, `unit`, `effective_date`, `recorded_utc`, `source`, `source_id`
  - Not yet wired to a Telegram command in this pass.

- `catalog`
  - Keyed by SKU for SKU aliasing
  - Values:
    - `sku_id`, `aliases`, `unit`, `default_sale_price`

- `sales`
  - Values per record:
    - `sale_id`, `draft_id`, `recorded_utc`, `source`
    - `lines[]` (`sku_name`, `qty`, `unit_sale_price`, `line_total`)
    - `total`, `customer_name`, `paid_cash`
  - Writes:
    - confirmed text/photo transaction drafts -> `add_sale_record`

- `insights`
  - Optional derived records for historical trend checks
  - Not required for writes in the current pass

- `ingestion_log`
  - Append-only write events:
    - `event_id`, `recorded_at`, `kind`, `source`, `source_id` and payload metadata

## Confirm-first contract

All user data writes are blocked until a confirm callback:
1. User enters `/ledger`, `/cash`, `/loan`, `/stock`, or transaction text/photo.
2. Bot stores a `pending` draft with parsed output only.
3. `/cancel`, timeout, or non-activity keeps data unwritten.
4. `✅ Confirm` triggers `_persist_*` writer method and records in `ingestion_log`.

## Missing in v1 (planned)
- explicit loan repayment entries
- supplier offer ingestion and supplier-level compare
- returns/spoilage adjustments
- cost-of-goods ledger (`unit_cost` per sale/stock change)
- linked sales-to-utang split for mixed-payment single transactions
