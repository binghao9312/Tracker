import unittest


class PaperPnlTests(unittest.TestCase):
    def test_long_and_short_pnl_use_opposite_price_signs(self) -> None:
        quantity = 10
        entry, exit_price = 100, 105
        long_gross = (exit_price - entry) * quantity
        short_gross = (exit_price - entry) * quantity * -1
        self.assertEqual(long_gross, 50)
        self.assertEqual(short_gross, -50)


if __name__ == "__main__":
    unittest.main()
