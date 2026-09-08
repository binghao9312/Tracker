import unittest
from decimal import Decimal

from app.collectors.orderbooks import _depth_levels, _integer, _json_object


class OrderBookCollectorParserTests(unittest.TestCase):
    def test_parses_depth_messages(self) -> None:
        event = _json_object('{"U": 7, "b": [["99.5", "2"]]}')

        self.assertEqual(_integer(event, "U"), 7)
        self.assertEqual(_depth_levels(event["b"]), [(Decimal("99.5"), Decimal("2"))])

    def test_rejects_malformed_messages(self) -> None:
        with self.assertRaises(ValueError):
            _json_object("[]")
        with self.assertRaises(ValueError):
            _depth_levels([["100"]])


if __name__ == "__main__":
    unittest.main()
