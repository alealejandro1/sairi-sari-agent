import tempfile
from pathlib import Path
import unittest
from datetime import datetime

from ledger_ocr import UtangLedgerStore, parse_ledger_ocr_text


class TestLedgerOCR(unittest.TestCase):
    def test_parse_kutan_ledger_sample(self) -> None:
        sample = """UTANG LEDGER-Ate Nena (Blk 3, beside the barangay hall
Mar 3
2 Marbobobox 20s),5 KopiPawa
P241.85
P241.85
Mar 5
6 Hydro water,I Bathy soap
P136.31
P378.16
Mar7
BAYAD
P200.00
P178.16
Mar 9
P110.38
P288.54
3 Cruncher,5 Wafer Crisp,4 ChocoJo
Mar II.
79.84
368.38
10Glow sachet, 2 SeasonBite
Mar 14BAYAD
150.00
P218.38
Mar 15
3 Luntuk beerIBalao 10s
199.96
418.34
TOTAL
HG=230
math=2.5"""

        parsed = parse_ledger_ocr_text(sample)
        self.assertEqual(parsed["customer_name"], "Ate Nena")
        entries = parsed["entries"]
        self.assertGreaterEqual(len(entries), 6)
        self.assertTrue(entries[0]["date"].endswith("-03-03"))
        self.assertEqual(entries[0]["date"], f"{datetime.utcnow().year}-03-03")
        self.assertAlmostEqual(entries[0]["amount"], 241.85)
        payment_rows = [row for row in entries if row["entry_kind"] == "payment"]
        self.assertEqual(len(payment_rows), 2)
        self.assertLess(payment_rows[0]["amount"], 0)
        self.assertLess(payment_rows[1]["amount"], 0)

    def test_parse_mayad_variant_is_payment(self) -> None:
        sample = """UTANG LEDGER-Unit Test
Mar 1
2 Test goods
P100.00
P100.00
Mar 3
MAYAD
P30.00
P70.00
TOTAL
HG=100"""
        parsed = parse_ledger_ocr_text(sample)
        entries = parsed["entries"]
        self.assertEqual(len(entries), 2)
        payment_rows = [row for row in entries if row["entry_kind"] == "payment"]
        self.assertEqual(len(payment_rows), 1)
        self.assertLess(payment_rows[0]["amount"], 0)
        self.assertEqual(payment_rows[0]["running_balance"], 70.00)


    def test_json_store_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store_path = Path(tmpdir) / "ledger_store.json"
            store = UtangLedgerStore(str(store_path))

            store.upsert_ledger(
                customer_name="Rodel Fishpond",
                entries=[
                    {
                        "date": "2026-03-01",
                        "entry_kind": "credit_sale",
                        "note": "2 marbobobox",
                        "amount": 199.96,
                        "running_balance": 199.96,
                        "raw_lines": ["1: Mar 1", "2: 2 marbobobox", "3: P199.96", "4: P199.96"],
                        "confidence": 0.9,
                        "warnings": [],
                    }
                ],
                source="test",
                source_id="d1",
            )

            entries = store.get_customer_ledger("Rodel Fishpond")
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0]["amount"], 199.96)
