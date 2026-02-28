# Utang Ledger OCR Analysis Notes (Kutan test images)

## Source images
- `assets/test-images/utang_ledger_01.png`
- `assets/test-images/utang_ledger_02.png`

## Observed structure
Both images match the same manual ledger style:
- Header: `UTANG LEDGER-<household name>`
- Column headers: `Items`, `Amount`, `Date`, `Balance`
- Body rows are mixed-date entries for credit/debts
- Footer marker: `TOTAL` plus trailing noise (`HG=230`, `math=2.5`, etc.)

## Raw OCR text samples

### `utang_ledger_01.png`
- `UTANG LEDGER-Ate Nena (Blk 3, beside the barangay hall`
- `Mar 3` `2 Marbobobox 20s),5 KopiPawa` `P241.85` `P241.85`
- `Mar 5` `6 Hydro water,I Bathy soap` `P136.31` `P378.16`
- `Mar7` `BAYAD` `P200.00` `P178.16`
- `Mar 9` `P110.38` `P288.54` `3 Cruncher,5 Wafer Crisp,4 ChocoJo`
- `Mar II.` (OCR typo for Mar 11) `79.84` `368.38` `10Glow sachet, 2 SeasonBite`
- `Mar 14` `BAYAD` `150.00` `P218.38`
- `Mar 15` `3 Luntuk beerIBalao 10s` `199.96` `418.34`
- `TOTAL` `HG=230` `math=2.5`

### `utang_ledger_02.png`
- `UTANG LEDGER-Rodel fishpond,bayaw ni Mang Bert)`
- `Mar4` `Balao los)3 Luntuk beer` `P199.96` `P199.96`
- `Mar6` `8Dishy liquid5 KopiPawa` `P112.77` `312.73`
- `Mar 8` `P181.80` `P494.53` `2 Marbobo box 20s`
- `Mar 10` `BAYAD` `300.00` `P194.53`
- `Mar12` `6 Hydro water,lO Glow sachet` `P155.44` `P349.97`
- `56.01` `405.98` (spurious line with amount-like noise)
- `Mar 13` `2 SeasonBite3 Cruncher` `Mar 164 ChocoJoy5 Wafer Crisp` `87.61` `493.59`
- `TOTAL` `15` `HG-230` `math=2.5`

## Parsing rules for OCR normalization
1. Detect ledger mode by matching `UTANG LEDGER` in OCR text.
2. Extract household name from the header after `UTANG LEDGER-` and trim punctuation/parentheticals.
3. Recognize row date tokens:
   - Normalize `Mar`, `mar`, `MarII` and other OCR variants to valid dates.
4. Classify row type:
   - `BAYAD` (or payment-like OCR variants) => payment row (`amount_delta = -amount`)
   - otherwise => credit row (`amount_delta = +amount` when items present)
5. Parse line item text:
   - OCR often concatenates item/qty with no spaces (`10Glow`, `3 Cruncher`).
   - Keep original text as memory cue; do not require full SKU mapping.
6. Parse amounts:
   - Strip `P`, commas, spaces.
   - Accept `P` + decimal amounts with optional OCR noise.
7. Parse balance:
   - Use provided balance as ground truth; recompute/validate from prior balance + row delta when possible.
8. Ignore footer/footer-like noise lines (`TOTAL`, `HG...`, `math...`) unless a strategy requires them.

## Validation heuristics
- Row ordering should be non-decreasing by date.
- Balance recalculation tolerance: ±0.50 PHP for minor OCR drift, then flag for review.
- Require at least one of `date + amount + balance` for a row to be accepted.
- Preserve low-confidence lines in draft state for manual confirmation rather than dropping.

## Implementation notes
- These pages do not provide a strict per-line SKU grammar; extraction should prioritize:
  1. date, type, amount, and resulting balance
  2. free-form item text
- Goal is to reconstruct ledger state, not perfect product catalog accuracy.

## Recent fixes (2026-02-28)

- Added safer ledger payload rendering in Telegram responses to avoid a runtime crash when preview text was accidentally built as a tuple.
  - Fix location: `src/main.py` around `ledger_json_review` response message.
- Improved OCR parse robustness for ledger photos with merged OCR tokens:
  - Handle dates like `Mar 13.2` and `Mar 164` where the day token and note text are concatenated.
  - Keep payment detection robust for `BAYAD`/OCR variants and preserve them as payment rows (`amount_delta` negative).
  - Add amount-pair selection logic to resolve rows with multiple extracted numbers by checking previous balance consistency.
  - Keep ambiguous rows (`note but no readable amount`) as review rows instead of dropping data.
- Added handling for rows where an extra amount-only token appears before the actual balance (`P56.01` / `405.98` style noise) by selecting the transaction amount and balance pair that best matches running balance math.
