import io
import contextlib
import unittest

from src.main import main


class TestSmoke(unittest.TestCase):
    def test_smoke(self) -> None:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            main()
        out = buffer.getvalue()
        self.assertIn("bootstrap ready", out)


if __name__ == "__main__":
    unittest.main()
