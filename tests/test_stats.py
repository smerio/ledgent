"""Unit tests for the Telegram bot stats command.

Run from the project root with:
    PYTHONPATH=src python3 -m unittest discover tests -v
"""
from __future__ import annotations

import sys
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import handler


def _tx(**kwargs) -> dict:
    """Build a transaction dict with sensible defaults for stats testing."""
    return {
        "operation": kwargs["operation"],
        "asset": kwargs.get("asset"),
        "amount": kwargs.get("amount", 0),
        "price": kwargs.get("price", 0),
        "quote_asset": kwargs.get("quote_asset"),
        "source": kwargs.get("source"),
        "destination": kwargs.get("destination"),
        "fee_amount": kwargs.get("fee_amount", 0),
        "fee_asset": kwargs.get("fee_asset"),
        "timestamp": kwargs["timestamp"],
        "tx_id": kwargs.get("tx_id", kwargs["timestamp"]),
        "quote_per_usd": kwargs.get("quote_per_usd", 0),
        "usd_per_quote": kwargs.get("usd_per_quote", 0),
        "raw_text": kwargs.get("raw_text", ""),
        "imported": kwargs.get("imported", False),
    }


class TestStatsCommand(unittest.TestCase):

    @patch("handler.tg.send_message")
    @patch("handler.database.query_transactions")
    def test_stats_no_buys(self, mock_query, mock_send_message):
        # Setup: No transactions
        mock_query.return_value = []

        handler._cmd_stats(chat_id=123, user_id=456, asset="BTC")

        # Verify correct placeholder message sent
        mock_send_message.assert_called_once_with(123, "_No buy transactions for BTC yet._")

    @patch("handler.tg.send_message")
    @patch("handler.database.query_transactions")
    def test_stats_all_assets_no_buys(self, mock_query, mock_send_message):
        # Setup: No transactions, querying all assets
        mock_query.return_value = []

        handler._cmd_stats(chat_id=123, user_id=456, asset=None)

        mock_send_message.assert_called_once_with(123, "_No buy transactions for any asset yet._")

    @patch("handler.tg.send_message")
    @patch("handler.database.query_transactions")
    def test_stats_simple_usdt_buys(self, mock_query, mock_send_message):
        # Setup: 2 USDT-denominated buys of BTC, 10 days apart
        txs = [
            _tx(operation="SPOT_BUY", asset="BTC", amount="1.5", price="40000",
                quote_asset="USDT", timestamp="2026-05-01T12:00:00Z"),
            _tx(operation="SPOT_BUY", asset="BTC", amount="0.5", price="60000",
                quote_asset="USDT", timestamp="2026-05-11T12:00:00Z"),
        ]
        # database.query_transactions is called once with asset="BTC" and once without if asset is provided
        mock_query.side_effect = [txs, txs]

        handler._cmd_stats(chat_id=123, user_id=456, asset="BTC")

        # Verify that we sent a message formatting the correct stats
        mock_send_message.assert_called_once()
        args = mock_send_message.call_args[0]
        self.assertEqual(args[0], 123)
        formatted_message = args[1]
        
        # Check that the stats contain correct values
        self.assertIn("Total buys logged: `2`", formatted_message)
        self.assertIn("Average buy price: `45000` USDT", formatted_message)
        self.assertIn("Avg days between buys: `10`", formatted_message)
        self.assertIn("First transaction: `2026-05-01`", formatted_message)
        self.assertIn("Last transaction: `2026-05-11`", formatted_message)

    @patch("handler.tg.send_message")
    @patch("handler.database.query_transactions")
    def test_stats_mixed_currencies_with_inference(self, mock_query, mock_send_message):
        # Setup:
        # 1. Buy BTC at 20000 USDT on Jan 1st (Explicit price index point for BTC)
        # 2. Buy USDT at 80 RUB on Jan 2nd (Stablecoin inverse inference: 1/80 USD per RUB)
        # 3. Buy BTC at 1,600,000 RUB on Jan 3rd (Non-stablecoin asset inference using Jan 1st BTC price:
        #    BTC historical USD price = 20000 USD, so 20000 / 1600000 = 1/80 = 0.0125 USD per RUB)
        # 4. Buy BTC at 20000 USDT on Jan 4th (Ensures USDT is primary quote asset due to higher count)
        txs = [
            _tx(operation="SPOT_BUY", asset="BTC", amount="0.1", price="20000",
                quote_asset="USDT", timestamp="2026-01-01T00:00:00Z"),
            _tx(operation="P2P_BUY", asset="USDT", amount="1000", price="80",
                quote_asset="RUB", timestamp="2026-01-02T00:00:00Z"),
            _tx(operation="SPOT_BUY", asset="BTC", amount="0.1", price="1600000",
                quote_asset="RUB", timestamp="2026-01-03T00:00:00Z"),
            _tx(operation="SPOT_BUY", asset="BTC", amount="0.05", price="20000",
                quote_asset="USDT", timestamp="2026-01-04T00:00:00Z"),
        ]
        # For querying BTC buys only (excludes txs[1] since it is USDT buy, not BTC buy)
        btc_buys = [txs[0], txs[2], txs[3]]
        mock_query.side_effect = [btc_buys, txs]

        handler._cmd_stats(chat_id=123, user_id=456, asset="BTC")

        # Let's verify the calculations:
        # Buys:
        # Buy 1: Amount = 0.1, Price = 20000, Quote = USDT (rate = 1.0) -> Cost USD = 2000
        # Buy 2: Amount = 0.1, Price = 1600000, Quote = RUB (rate = 20000 / 1600000 = 0.0125) -> Cost USD = 2000
        # Buy 3: Amount = 0.05, Price = 20000, Quote = USDT (rate = 1.0) -> Cost USD = 1000
        # Total Amount = 0.25
        # Total Cost USD = 5000
        # Avg Buy Price USD = 20000
        #
        # Quotes are USDT (2) and RUB (1), so primary_quote is "USDT".
        # Converting back to USDT: rate to USD = 1.0 (USDT is USD pegged).
        # Avg Buy Price = 20000 USDT.
        
        mock_send_message.assert_called_once()
        formatted_message = mock_send_message.call_args[0][1]
        self.assertIn("Total buys logged: `3`", formatted_message)
        self.assertIn("Average buy price: `20000` USDT", formatted_message)

    @patch("handler.tg.send_message")
    @patch("handler.database.query_transactions")
    def test_stats_avg_days_calculation_multiple(self, mock_query, mock_send_message):
        # 3 transactions, exactly 5 days between each
        txs = [
            _tx(operation="SPOT_BUY", asset="BTC", amount="1", price="30000",
                quote_asset="USDT", timestamp="2026-05-01T00:00:00Z"),
            _tx(operation="SPOT_BUY", asset="BTC", amount="1", price="35000",
                quote_asset="USDT", timestamp="2026-05-06T00:00:00Z"),
            _tx(operation="SPOT_BUY", asset="BTC", amount="1", price="40000",
                quote_asset="USDT", timestamp="2026-05-11T00:00:00Z"),
        ]
        mock_query.side_effect = [txs, txs]

        handler._cmd_stats(chat_id=123, user_id=456, asset="BTC")

        mock_send_message.assert_called_once()
        formatted_message = mock_send_message.call_args[0][1]
        self.assertIn("Avg days between buys: `5`", formatted_message)

    @patch("handler.tg.send_message")
    @patch("handler.database.query_transactions")
    def test_stats_none_and_empty_values(self, mock_query, mock_send_message):
        # Setup: Buy transactions, but with some entries having None and empty string values for price/amount
        # simulating transactions in the production DB (e.g. transfers, reconciliations)
        txs = [
            _tx(operation="SPOT_BUY", asset="BTC", amount="1", price="30000",
                quote_asset="USDT", timestamp="2026-05-01T00:00:00Z"),
            # An entry with None values
            {
                "operation": "TRANSFER",
                "asset": "BTC",
                "amount": None,
                "price": None,
                "quote_asset": None,
                "timestamp": "2026-05-05T00:00:00Z",
            },
            # An entry with empty string values
            {
                "operation": "INCOME",
                "asset": "BTC",
                "amount": "",
                "price": "",
                "quote_asset": "",
                "timestamp": "2026-05-06T00:00:00Z",
            },
            _tx(operation="SPOT_BUY", asset="BTC", amount="1", price="40000",
                quote_asset="USDT", timestamp="2026-05-11T00:00:00Z"),
        ]
        # Only buy operations are processed in the stats calculation,
        # but the complete list is retrieved for build chronological indices.
        buys = [txs[0], txs[3]]
        mock_query.side_effect = [buys, txs]

        # Call stats command. It should not raise decimal.ConversionSyntax error.
        handler._cmd_stats(chat_id=123, user_id=456, asset="BTC")

        mock_send_message.assert_called_once()
        formatted_message = mock_send_message.call_args[0][1]
        self.assertIn("Total buys logged: `2`", formatted_message)
        self.assertIn("Average buy price: `35000` USDT", formatted_message)


class TestSetCommandResilience(unittest.TestCase):

    @patch("handler.tg.send_message")
    @patch("handler._reconcile")
    @patch("handler._replay_state")
    def test_set_with_standard_minus(self, mock_replay, mock_reconcile, mock_send):
        mock_state = MagicMock()
        mock_state.balances = {"RUB": {"tbank": Decimal("0")}}
        mock_replay.return_value = mock_state

        handler._cmd_set(
            text="/set RUB tbank -12345.6",
            chat_id=123,
            user_id=456
        )

        mock_reconcile.assert_called_once_with(
            123, 456, "RUB", "tbank", Decimal("-12345.6"), note="manual /set"
        )
        mock_send.assert_not_called()

    @patch("handler.tg.send_message")
    @patch("handler._reconcile")
    @patch("handler._replay_state")
    def test_set_with_unicode_minus(self, mock_replay, mock_reconcile, mock_send):
        mock_state = MagicMock()
        mock_state.balances = {"RUB": {"tbank": Decimal("0")}}
        mock_replay.return_value = mock_state

        # Using U+2212 '−'
        handler._cmd_set(
            text="/set rub tbank −12345.6",
            chat_id=123,
            user_id=456
        )

        mock_reconcile.assert_called_once_with(
            123, 456, "RUB", "tbank", Decimal("-12345.6"), note="manual /set"
        )
        mock_send.assert_not_called()

    @patch("handler.tg.send_message")
    @patch("handler._reconcile")
    @patch("handler._replay_state")
    def test_set_with_en_and_em_dash(self, mock_replay, mock_reconcile, mock_send):
        mock_state = MagicMock()
        mock_state.balances = {"RUB": {"tbank": Decimal("0")}}
        mock_replay.return_value = mock_state

        # U+2013 '–' en-dash
        handler._cmd_set(
            text="/set rub tbank –100",
            chat_id=123,
            user_id=456
        )
        mock_reconcile.assert_any_call(
            123, 456, "RUB", "tbank", Decimal("-100"), note="manual /set"
        )

        # U+2014 '—' em-dash
        handler._cmd_set(
            text="/set rub tbank —200.5",
            chat_id=123,
            user_id=456
        )
        mock_reconcile.assert_any_call(
            123, 456, "RUB", "tbank", Decimal("-200.5"), note="manual /set"
        )

    @patch("handler.tg.send_message")
    @patch("handler._reconcile")
    @patch("handler._replay_state")
    def test_set_swapped_with_unicode_minus(self, mock_replay, mock_reconcile, mock_send):
        mock_state = MagicMock()
        mock_state.balances = {"RUB": {"tbank": Decimal("0")}}
        mock_replay.return_value = mock_state

        handler._cmd_set(
            text="/set tbank rub to −12345.6",
            chat_id=123,
            user_id=456
        )

        mock_reconcile.assert_not_called()
        mock_send.assert_called_once()
        args = mock_send.call_args[0]
        self.assertEqual(args[0], 123)
        self.assertIn("Did you mean `/set RUB Tbank -12345.6`?", args[1])

