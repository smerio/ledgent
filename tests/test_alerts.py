"""Unit tests for the price alerting system (custom & automatic volatility).

Run from the project root with:
    PYTHONPATH=src python3.12 -m unittest discover tests -v
"""
from __future__ import annotations

import sys
import json
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import alerts


class TestPriceAlerts(unittest.TestCase):

    def setUp(self):
        # Reset any environment variables needed
        self.patch_env = patch.dict("os.environ", {
            "ALLOWED_TELEGRAM_USER_ID": "123456789",
            "BASE_CURRENCY": "USD",
        })
        self.patch_env.start()

    def tearDown(self):
        self.patch_env.stop()

    @patch("alerts.requests.get")
    @patch("alerts.database")
    @patch("alerts.accounting.replay")
    @patch("alerts.tg")
    def test_run_price_alerts_custom_price_triggers(self, mock_tg, mock_replay, mock_database, mock_get):
        # 1. Mock transactions and held assets
        mock_database.query_transactions.return_value = []
        mock_state = MagicMock()
        mock_state.open_lots = {}
        mock_replay.return_value = mock_state

        # 2. Mock custom active alerts in DB:
        # - BTC: crosses above 75000 (currently 76000 -> trigger)
        # - ETH: crosses below 3500 (currently 3600 -> no trigger)
        # - SOL: crosses below 150 (currently 140 -> trigger)
        mock_database.list_custom_alerts.return_value = [
            {
                "alert_id": "ulid1",
                "asset": "BTC",
                "condition": ">",
                "target": Decimal("75000"),
                "baseline_price": Decimal("70000"),
            },
            {
                "alert_id": "ulid2",
                "asset": "ETH",
                "condition": "<",
                "target": Decimal("3500"),
                "baseline_price": Decimal("3800"),
            },
            {
                "alert_id": "ulid3",
                "asset": "SOL",
                "condition": "<",
                "target": Decimal("150"),
                "baseline_price": Decimal("160"),
            },
        ]

        # 3. Mock CoinGecko simple price response
        mock_response = MagicMock()
        mock_response.ok = True
        mock_response.json.return_value = {
            "bitcoin": {"usd": 76000.0, "usd_24h_change": 1.5},
            "ethereum": {"usd": 36000.0, "usd_24h_change": -0.5},  # Wait, wait... 3600.0 vs 36000.0
            "solana": {"usd": 140.0, "usd_24h_change": -6.0},
        }
        mock_get.return_value = mock_response

        # Call price alerts runner
        alerts.run_price_alerts()

        # BTC crosses above 75000 (currently 76000) -> should trigger and be deleted
        mock_tg.format_custom_alert_triggered.assert_any_call(
            "BTC", ">", Decimal("75000"), Decimal("76000"), Decimal("70000")
        )
        mock_database.delete_custom_alert.assert_any_call(123456789, "BTC", "ulid1")

        # SOL crosses below 150 (currently 140) -> should trigger and be deleted
        mock_tg.format_custom_alert_triggered.assert_any_call(
            "SOL", "<", Decimal("150"), Decimal("140"), Decimal("160")
        )
        mock_database.delete_custom_alert.assert_any_call(123456789, "SOL", "ulid3")

        # ETH crosses below 3500 (currently 36000/3600) -> not met -> should NOT trigger or delete
        for call in mock_database.delete_custom_alert.call_args_list:
            self.assertNotEqual(call[0][2], "ulid2")

    @patch("alerts.requests.get")
    @patch("alerts.database")
    @patch("alerts.accounting.replay")
    @patch("alerts.tg")
    def test_run_price_alerts_custom_percent_triggers(self, mock_tg, mock_replay, mock_database, mock_get):
        mock_database.query_transactions.return_value = []
        mock_state = MagicMock()
        mock_state.open_lots = {}
        mock_replay.return_value = mock_state

        # Mock relative percent alerts:
        # - BTC: moves by 5% (baseline 100000, current 105100 -> +5.1% -> trigger)
        # - ETH: moves by 10% (baseline 4000, current 3900 -> -2.5% -> no trigger)
        mock_database.list_custom_alerts.return_value = [
            {
                "alert_id": "pct1",
                "asset": "BTC",
                "condition": "%",
                "target": Decimal("5"),
                "baseline_price": Decimal("100000"),
            },
            {
                "alert_id": "pct2",
                "asset": "ETH",
                "condition": "%",
                "target": Decimal("10"),
                "baseline_price": Decimal("4000"),
            },
        ]

        mock_response = MagicMock()
        mock_response.ok = True
        mock_response.json.return_value = {
            "bitcoin": {"usd": 105100.0, "usd_24h_change": 5.1},
            "ethereum": {"usd": 3900.0, "usd_24h_change": -2.5},
        }
        mock_get.return_value = mock_response

        alerts.run_price_alerts()

        # BTC moves by 5% (currently +5.1%) -> trigger
        mock_tg.format_custom_alert_triggered.assert_any_call(
            "BTC", "%", Decimal("5"), Decimal("105100"), Decimal("100000")
        )
        mock_database.delete_custom_alert.assert_any_call(123456789, "BTC", "pct1")

        # ETH moves by 10% (currently -2.5%) -> should not trigger
        for call in mock_database.delete_custom_alert.call_args_list:
            self.assertNotEqual(call[0][2], "pct2")

    @patch("alerts.requests.get")
    @patch("alerts.database")
    @patch("alerts.accounting.replay")
    @patch("alerts.tg")
    def test_run_price_alerts_volatility_alerts(self, mock_tg, mock_replay, mock_database, mock_get):
        # 1. Setup portfolio active holdings
        # BTC and ETH are held (total balances > 0)
        mock_state = MagicMock()
        mock_state.open_lots = {
            "BTC": [MagicMock(amount_remaining=Decimal("1.5"))],
            "ETH": [MagicMock(amount_remaining=Decimal("10.0"))],
        }
        mock_state.total_balance.side_effect = lambda asset: Decimal("1.5") if asset == "BTC" else Decimal("10.0")
        mock_replay.return_value = mock_state
        mock_database.query_transactions.return_value = []
        mock_database.list_custom_alerts.return_value = []

        # 2. Mock CoinGecko
        # - BTC: moves by +5.2% (volatility trigger!)
        # - ETH: moves by -1.5% (no trigger)
        mock_response = MagicMock()
        mock_response.ok = True
        mock_response.json.return_value = {
            "bitcoin": {"usd": 68000.0, "usd_24h_change": 5.2},
            "ethereum": {"usd": 3400.0, "usd_24h_change": -1.5},
        }
        mock_get.return_value = mock_response

        # 3. Mock no suppression active for BTC
        mock_database.get_user_config.return_value = None

        alerts.run_price_alerts()

        # BTC volatility alert should trigger
        mock_tg.format_volatility_alert.assert_any_call("BTC", Decimal("5.2"), Decimal("68000"))
        # Check that suppression log is stored
        mock_database.put_user_config.assert_called_once()
        args = mock_database.put_user_config.call_args[0]
        self.assertEqual(args[0], 123456789)
        self.assertEqual(args[1], "volatility_alert_BTC")
        val = json.loads(args[2])
        self.assertEqual(val["last_notified_change"], 5.2)

    @patch("alerts.requests.get")
    @patch("alerts.database")
    @patch("alerts.accounting.replay")
    @patch("alerts.tg")
    def test_run_price_alerts_volatility_suppression(self, mock_tg, mock_replay, mock_database, mock_get):
        # Setup portfolio active holdings
        mock_state = MagicMock()
        mock_state.open_lots = {
            "BTC": [MagicMock(amount_remaining=Decimal("1.5"))],
        }
        mock_state.total_balance.return_value = Decimal("1.5")
        mock_replay.return_value = mock_state
        mock_database.query_transactions.return_value = []
        mock_database.list_custom_alerts.return_value = []

        # BTC moves by +5.5%
        mock_response = MagicMock()
        mock_response.ok = True
        mock_response.json.return_value = {
            "bitcoin": {"usd": 68000.0, "usd_24h_change": 5.5},
        }
        mock_get.return_value = mock_response

        # Scenario A: Suppression log exists, within 12 hours, absolute difference is small (e.g. from 5.0% to 5.5% is 0.5% diff)
        # Should be SUPPRESSED!
        mock_database.get_user_config.return_value = {
            "value": json.dumps({
                "last_notified_change": 5.0,
                "last_notified_at": datetime.now(timezone.utc).isoformat(),
            })
        }

        alerts.run_price_alerts()
        mock_tg.format_volatility_alert.assert_not_called()

        # Scenario B: Suppression log exists, within 12 hours, BUT absolute difference is large (e.g. from 5.0% to 7.8% is 2.8% diff)
        # Should TRIGGER!
        mock_response.json.return_value = {
            "bitcoin": {"usd": 68000.0, "usd_24h_change": 7.8},
        }
        mock_tg.reset_mock()

        alerts.run_price_alerts()
        mock_tg.format_volatility_alert.assert_called_once_with("BTC", Decimal("7.8"), Decimal("68000"))

        # Scenario C: Suppression log exists, but older than 12 hours (e.g. 13 hours ago), price difference is small (e.g. 5.0% to 5.5%)
        # Should TRIGGER!
        mock_response.json.return_value = {
            "bitcoin": {"usd": 68000.0, "usd_24h_change": 5.5},
        }
        mock_database.get_user_config.return_value = {
            "value": json.dumps({
                "last_notified_change": 5.0,
                "last_notified_at": "2026-05-19T02:00:00+00:00", # Fixed old timestamp (older than 12 hours)
            })
        }
        mock_tg.reset_mock()

        alerts.run_price_alerts()
        mock_tg.format_volatility_alert.assert_called_once_with("BTC", Decimal("5.5"), Decimal("68000"))


if __name__ == "__main__":
    unittest.main()
