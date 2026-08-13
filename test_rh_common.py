import unittest
from unittest.mock import Mock, patch

import robin_stocks.robinhood.helper as helper

import rh_common as rc

class RobinhoodUniverseTest(unittest.TestCase):
    def test_only_active_instruments_are_returned(self):
        response = Mock()
        response.json.return_value = {
            "results": [
                {"symbol": "AAPL", "state": "active"},
                {"symbol": "ADMP", "state": "inactive"},
                {"symbol": "OLD", "state": "unlisted"},
                {"symbol": "BRK.B", "state": "active"},
            ],
            "next": None,
        }
        with patch.object(helper.SESSION, "get", return_value=response):
            symbols = rc.fetch_all_robinhood_symbols(rc.RateLimiter(), max_pages=1)
        self.assertEqual(symbols, ["AAPL", "BRK-B"])


if __name__ == "__main__":
    unittest.main()
