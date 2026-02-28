# Parallel Thread Charter

Use this when running multiple Codex threads simultaneously.

## Global rules
- One branch per thread (`codex/<track-name>`).
- One PR per branch.
- `docs/product-requirements.md` is the scope contract.
- Avoid cross-thread edits to the same file unless coordinated.

## Suggested MVP-lite tracks

### Track A: Multimodal intake
- Scope: Telegram text/photo intake, parsing pipeline, confirmation prompts.
- Done when: shelf photo and notebook photo both produce editable drafts.
- Key files: `src/bot/*`, extraction adapters, intake tests.

### Track B: Core records and checks
- Scope: product/inventory/sales/customer/utang/loan models and append-only event logic.
- Done when: confirmed drafts post records and margin/consistency checks run on sale records.
- Key files: `src/accounting/*` (or core domain modules), model tests.

### Track C: Wholesaler prep output
- Scope: compute low-stock list and propose purchase quantities using existing stock and reorder targets.
- Done when: bot can respond with a usable trip-prep list.
- Key files: `src/api/*`, summary/recommendation modules, integration tests.

## Definition of done per track
- PR maps changes to specific MVP-lite requirements.
- Tests cover happy path + correction/edit path.
- Docs updated when behavior changes.
