import unittest

from src.main import _parse_text_to_lines, _format_draft


class TestParser(unittest.TestCase):
    def test_parse_items_with_qty_and_price(self):
        lines = _parse_text_to_lines("chips 2 12\nsoap x3 @ 15\nsoap")
        self.assertEqual(len(lines), 3)
        self.assertEqual(lines[0].item, "chips")
        self.assertEqual(lines[0].qty, 2)
        self.assertEqual(lines[0].price, 12)
        self.assertEqual(lines[1].item, "soap")
        self.assertEqual(lines[1].qty, 3)
        self.assertEqual(lines[1].price, 15)
        self.assertEqual(lines[2].item, "soap")
        self.assertEqual(lines[2].qty, 1)

    def test_format_draft(self):
        lines = _parse_text_to_lines("coke 1 10")
        text = _format_draft("d1", "text", lines)
        self.assertIn("Draft #d1", text)
        self.assertIn("coke", text)
        self.assertIn("qty 1", text)
        self.assertIn("PHP 10.00", text)
        self.assertIn("Grand Total", text)


if __name__ == "__main__":
    unittest.main()
