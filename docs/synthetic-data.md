# Synthetic Data Package (English)

This package supports rapid prototyping and testing for MVP-lite flows.

## Folder
`data/synthetic/`

## Files
- `business_state.json`: unified single-file state used by the MVP-lite runtime.

The JSON file is the source of truth for all tests and fixtures. It contains:
- `customers` ledgers and payment history
- `inventory` stock levels and movement events
- `cash` snapshots
- `loans` obligations
- `offers` and supplier-side price suggestions
- `catalog`, `sales`, `insights`, and `ingestion_log`

Its structure is documented in `docs/business-state-model.md`.

## Design choices
- English-only item naming and message text.
- Single shop (`shop_001`) for MVP focus.
- 30-SKU catalog limit with synthetic product family names and explicit categories.
- Realistic noise included:
  - OCR confidence variance.
  - occasional quantity corrections.
  - price deviations from baseline.
- Cash snapshots are generated with `source=user_input`.
- Ledger photo pages include:
  - up to 20 line items per photo
  - up to 4 sale dates represented by one photo batch
- Credit customers are generated with mixed repayment behavior.
- Net-worth insights can be generated from state transitions for analytics tests.
- Utang ledger page fixtures include mixed entries (`BAYAD`, mixed-date pages) to test robust household-level parsing.

## Intended uses
- Parser and confirmation flow tests.
- Margin/consistency check validation.
- Wholesaler prep-list logic testing.
- Demo data for stakeholder walkthroughs.
