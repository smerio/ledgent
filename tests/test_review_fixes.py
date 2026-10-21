"""Tests for the 2026-10 code-review fixes (parser totals, webhook secret,
edits, /alert, /undo, price sanity check, unrealized breakdown, lot moves).

Run from the project root with:
    PYTHONPATH=src python3 -m unittest discover tests -v
"""
from __future__ import annotations

import json
import os
import sys
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import accounting  # noqa: E402
import handler  # noqa: E402
import parser as llm_parser  # noqa: E402


def _tx(**kwargs) -> dict:
    base = {
        "amount": 0, "price": 0, "quote_asset": None, "source": None, "destination": None,
        "fee_amount": 0, "fee_asset": None, "raw_text": "", "imported": False,
    }
    base.update(kwargs)
    base.setdefault("tx_id", base["timestamp"])
    return base


def _llm(**kwargs) -> dict:
    payload = {"timestamp": "2026-10-07T10:00:00+00:00", "confidence": 0.95}
    payload.update(kwargs)
    return payload


class TestParserTotals(unittest.TestCase):

    def test_total_from_text_overrides_price(self):
        out = llm_parser._validate(
            _llm(operation="P2P_BUY", asset="USDT", amount=1156, price=86.5, quote_asset="RUB"),
            "1156 usdt on bybit from Tbank for 100000 rub",
        )
        self.assertEqual(out["total_quote_value"], Decimal("100000"))
        self.assertAlmostEqual(float(out["price"]), 100000 / 1156, places=6)

    def test_spent_and_paid_are_recognised(self):
        for word in ("spent", "paid"):
            out = llm_parser._validate(
                _llm(operation="P2P_BUY", asset="USDT", amount=903, price=0, quote_asset="RUB"),
                f"Bought 903 usdt on bybit from tbank {word} 70000 rub",
            )
            self.assertEqual(out["total_quote_value"], Decimal("70000"), word)
            self.assertGreater(out["price"], 0)

    def test_llm_total_is_used_and_decimal(self):
        out = llm_parser._validate(
            _llm(operation="SPOT_BUY", asset="BTC", amount=0.01, price=65000,
                 quote_asset="USDT", total_quote_value=650.5),
            "bought 0.01 btc on binance",
        )
        self.assertIsInstance(out["price"], Decimal)
        self.assertEqual(out["price"], Decimal("65050"))

    def test_trade_without_price_is_rejected(self):
        with self.assertRaises(llm_parser.IncompleteTradeError):
            llm_parser._validate(
                _llm(operation="P2P_BUY", asset="USDT", amount=1339.2259, price=0, quote_asset=None),
                "bought 1339.2259 USDT for 74.67 on bybit p2p",
            )

    def test_trade_without_quote_is_rejected(self):
        with self.assertRaises(llm_parser.IncompleteTradeError):
            llm_parser._validate(
                _llm(operation="SPOT_SELL", asset="USDT", amount=849, price=0, quote_asset=None),
                "Convert 849 usdt to 0.01350779 BTC on binance",
            )

    def test_stable_to_crypto_sell_becomes_crypto_buy(self):
        out = llm_parser._validate(
            _llm(operation="SPOT_SELL", asset="USDT", amount=475, price=0, quote_asset="BTC",
                 source="Binance", destination="Binance"),
            "Convert 475 usdt to 0.00612053 BTC on binance",
        )
        self.assertEqual(out["operation"], "SPOT_BUY")
        self.assertEqual(out["asset"], "BTC")
        self.assertEqual(out["amount"], Decimal("0.00612053"))
        self.assertEqual(out["quote_asset"], "USDT")
        self.assertEqual(out["total_quote_value"], Decimal("475"))
        self.assertAlmostEqual(float(out["price"]), 475 / 0.00612053, places=2)

    def test_stable_to_fiat_sell_is_unchanged(self):
        out = llm_parser._validate(
            _llm(operation="P2P_SELL", asset="USDT", amount=1000, price=96.83, quote_asset="RSD"),
            "Sold 1000 usdt from bybit for 96830 rsd Raiffeisen",
        )
        self.assertEqual(out["operation"], "P2P_SELL")
        self.assertEqual(out["asset"], "USDT")

    def test_claude_tool_schema_has_total(self):
        self.assertIn("total_quote_value", llm_parser._CLAUDE_TOOL["input_schema"]["properties"])


class TestTradePriceSanity(unittest.TestCase):

    @patch("handler._reference_price", return_value=Decimal("80"))
    def test_rate_read_as_total_is_corrected(self, _ref):
        # "bought 1205 usdt for 82.99 rub": regex took 82.99 as the total.
        parsed = {"asset": "USDT", "quote_asset": "RUB", "amount": 1205,
                  "price": Decimal("82.99") / 1205, "total_quote_value": Decimal("82.99")}
        self.assertIsNone(handler._check_trade_price(parsed, 1))
        self.assertEqual(parsed["price"], Decimal("82.99"))
        self.assertEqual(parsed["total_quote_value"], Decimal("82.99") * 1205)

    @patch("handler._reference_price", return_value=Decimal("65000"))
    def test_implausible_price_asks_user(self, _ref):
        parsed = {"asset": "BTC", "quote_asset": "USDT", "amount": 0.01,
                  "price": Decimal("100"), "total_quote_value": Decimal("1")}
        msg = handler._check_trade_price(parsed, 1)
        self.assertIn("Nothing was saved", msg)

    @patch("handler._reference_price", return_value=Decimal("86"))
    def test_plausible_price_passes(self, _ref):
        parsed = {"asset": "USDT", "quote_asset": "RUB", "amount": 1156,
                  "price": Decimal("86.5"), "total_quote_value": Decimal("100000")}
        self.assertIsNone(handler._check_trade_price(parsed, 1))
        self.assertEqual(parsed["price"], Decimal("86.5"))

    @patch("handler._reference_price", return_value=None)
    def test_no_reference_passes(self, _ref):
        parsed = {"asset": "XYZ", "quote_asset": "RUB", "amount": 1, "price": Decimal("5")}
        self.assertIsNone(handler._check_trade_price(parsed, 1))


class TestWebhook(unittest.TestCase):

    def _event(self, headers=None, edited=False):
        msg = {"from": {"id": 42}, "chat": {"id": 7}, "text": "bought btc"}
        update = {"update_id": 1, ("edited_message" if edited else "message"): msg}
        return {"headers": headers or {}, "body": json.dumps(update)}

    @patch.dict(os.environ, {"TELEGRAM_WEBHOOK_SECRET": "s3cret", "ALLOWED_TELEGRAM_USER_ID": "42"})
    @patch("handler.database.acquire_update_lock")
    def test_wrong_secret_is_rejected(self, mock_lock):
        resp = handler.lambda_handler(self._event({"x-telegram-bot-api-secret-token": "nope"}), MagicMock())
        self.assertEqual(resp["statusCode"], 401)
        mock_lock.assert_not_called()

    @patch.dict(os.environ, {"TELEGRAM_WEBHOOK_SECRET": "s3cret", "ALLOWED_TELEGRAM_USER_ID": "42"})
    @patch("handler.database.acquire_update_lock", return_value=True)
    def test_right_secret_is_accepted(self, _lock):
        with patch("boto3.client") as mock_client:
            resp = handler.lambda_handler(
                self._event({"X-Telegram-Bot-Api-Secret-Token": "s3cret"}), MagicMock())
        self.assertEqual(resp["statusCode"], 200)
        mock_client.return_value.invoke.assert_called_once()

    @patch.dict(os.environ, {"TELEGRAM_WEBHOOK_SECRET": "", "ALLOWED_TELEGRAM_USER_ID": "42"})
    @patch("handler.tg.send_message")
    @patch("handler.database.acquire_update_lock", return_value=True)
    def test_edited_message_is_not_processed(self, _lock, mock_send):
        with patch("boto3.client") as mock_client:
            resp = handler.lambda_handler(self._event(edited=True), MagicMock())
        self.assertEqual(resp["statusCode"], 200)
        mock_client.return_value.invoke.assert_not_called()
        self.assertIn("Edits aren't applied", mock_send.call_args[0][1])


class TestAlertInput(unittest.TestCase):

    @patch("handler.database.put_custom_alert")
    @patch("handler._live_prices", return_value={"BTC": Decimal("80000")})
    @patch("handler.tg.send_message")
    def test_invalid_numbers_do_not_crash(self, mock_send, _prices, mock_put):
        handler._cmd_alert("/alert BTC abc%", 1, 1)
        handler._cmd_alert("/alert BTC > abc", 1, 1)
        self.assertEqual(mock_send.call_count, 2)
        mock_put.assert_not_called()

    @patch("handler.database.put_custom_alert")
    @patch("handler._live_prices", return_value={"BTC": Decimal("80000")})
    @patch("handler.tg.send_message")
    def test_target_already_crossed_is_rejected(self, mock_send, _prices, mock_put):
        handler._cmd_alert("/alert BTC > 75000", 1, 1)
        mock_put.assert_not_called()
        self.assertIn("already above", mock_send.call_args[0][1])

    @patch("handler.database.put_custom_alert")
    @patch("handler._live_prices", return_value={"BTC": Decimal("80000")})
    @patch("handler.tg.send_message")
    def test_valid_target_with_comma_is_saved(self, _send, _prices, mock_put):
        handler._cmd_alert("/alert BTC > 85,000", 1, 1)
        self.assertEqual(mock_put.call_args.kwargs["target"], Decimal("85000"))


class TestUndo(unittest.TestCase):

    TX = _tx(operation="SPOT_BUY", asset="BTC", amount=Decimal("0.01"), price=Decimal("65000"),
             quote_asset="USDT", timestamp="2026-10-07T10:00:00+00:00")

    @patch("handler.database.delete_transaction")
    @patch("handler.database.put_user_config")
    @patch("handler.database.get_transaction")
    @patch("handler.database.get_user_config", return_value={"value": "TX#2026-10-07#X"})
    @patch("handler.tg.send_message")
    def test_undo_asks_then_deletes(self, mock_send, _cfg, mock_get, mock_put_cfg, mock_delete):
        mock_get.return_value = dict(self.TX)
        handler._cmd_undo("/undo", 1, 1)
        mock_delete.assert_not_called()
        self.assertIn("/undo confirm", mock_send.call_args[0][1])

        handler._cmd_undo("/undo confirm", 1, 1)
        mock_delete.assert_called_once_with(1, "TX#2026-10-07#X")
        mock_put_cfg.assert_called_once_with(1, "last_tx_sk", "")

    @patch("handler.database.delete_transaction")
    @patch("handler.database.get_transaction")
    @patch("handler.database.get_user_config", return_value={"value": "TX#2026-10-07#X"})
    @patch("handler.tg.send_message")
    def test_imported_tx_is_never_undone(self, mock_send, _cfg, mock_get, mock_delete):
        mock_get.return_value = dict(self.TX, imported=True)
        handler._cmd_undo("/undo confirm", 1, 1)
        mock_delete.assert_not_called()
        self.assertIn("Nothing to undo", mock_send.call_args[0][1])


class TestUnrealizedBreakdown(unittest.TestCase):

    def test_breakdown_sums_to_total_with_scaling(self):
        txs = [
            _tx(operation="SPOT_BUY", asset="BTC", amount="1", price="40000", quote_asset="USDT",
                destination="Binance", timestamp="2026-01-01T00:00:00Z"),
            # Reconcile down to 0.5 BTC: lots (1.0) now exceed the balance and must be scaled.
            _tx(operation="EXPENSE", asset="BTC", amount="0.5", source="Binance",
                raw_text="reconcile BTC@Binance 1 → 0.5", timestamp="2026-01-02T00:00:00Z"),
        ]
        state = accounting.replay(txs)
        # EXPENSE also reduces lots, so no scaling needed here; force a mismatch instead.
        state.open_lots["BTC"][0].amount_remaining = Decimal("1")
        prices = {"BTC": Decimal("50000")}
        breakdown = accounting.unrealized_breakdown_usd(state, prices)
        self.assertEqual(breakdown["BTC"], Decimal("5000"))  # (50000-40000) × 1 × 0.5
        self.assertEqual(accounting.unrealized_pnl_usd(state, prices), Decimal("5000"))


class TestLotLocationAndUnmatched(unittest.TestCase):

    def test_transfer_moves_lot_location_with_split(self):
        txs = [
            _tx(operation="SPOT_BUY", asset="BTC", amount="1", price="40000", quote_asset="USDT",
                destination="Binance", timestamp="2026-01-01T00:00:00Z"),
            _tx(operation="TRANSFER", asset="BTC", amount="0.4", source="Binance",
                destination="Ledger", imported=True, timestamp="2026-01-02T00:00:00Z"),
        ]
        lots = accounting.replay(txs).open_lots["BTC"]
        self.assertEqual([(l.location, l.amount_remaining) for l in lots],
                         [("Ledger", Decimal("0.4")), ("Binance", Decimal("0.6"))])
        self.assertTrue(all(l.cost_per_unit_usd == Decimal("40000") for l in lots))

    def test_sell_beyond_lots_is_tracked(self):
        txs = [
            _tx(operation="SPOT_BUY", asset="BTC", amount="0.1", price="40000", quote_asset="USDT",
                destination="Binance", timestamp="2026-01-01T00:00:00Z"),
            _tx(operation="SPOT_SELL", asset="BTC", amount="0.3", price="50000", quote_asset="USDT",
                source="Binance", destination="Binance", timestamp="2026-01-02T00:00:00Z"),
        ]
        state = accounting.replay(txs)
        self.assertEqual(state.unmatched_sells["BTC"], Decimal("0.2"))
        self.assertEqual(state.realized_pnl_usd, Decimal("1000"))

    def test_historical_price_picks_nearest(self):
        index = {"BTC": [("2026-01-01T00:00:00Z", Decimal("1")),
                         ("2026-01-10T00:00:00Z", Decimal("2")),
                         ("2026-01-20T00:00:00Z", Decimal("3"))]}
        get = accounting._get_historical_price
        self.assertEqual(get("BTC", "2025-12-01T00:00:00Z", index), Decimal("1"))
        self.assertEqual(get("BTC", "2026-01-12T00:00:00Z", index), Decimal("2"))
        self.assertEqual(get("BTC", "2026-01-18T00:00:00Z", index), Decimal("3"))
        self.assertEqual(get("BTC", "2027-01-01T00:00:00Z", index), Decimal("3"))


class TestTimestampNormalization(unittest.TestCase):

    def test_normalizes_and_falls_back(self):
        fb = "2026-10-07T00:00:00+00:00"
        self.assertEqual(handler._normalize_timestamp("2026-05-25T10:00:00Z", fb), "2026-05-25T10:00:00+00:00")
        self.assertEqual(handler._normalize_timestamp("2026-05-25", fb), "2026-05-25T00:00:00+00:00")
        self.assertEqual(handler._normalize_timestamp("yesterday", fb), fb)


if __name__ == "__main__":
    unittest.main()


class TestStrategyCycle(unittest.TestCase):
    """/strategy hands the advisor the current cycle, found in code (2026-10-21)."""

    def _history(self):
        # Newest first, as query_transactions(descending=True) returns it.
        return [
            _tx(operation="INCOME", asset="BTC", amount="0.00017", destination="Ledger",
                raw_text="reconcile BTC@Ledger", timestamp="2026-10-21T13:47:00+00:00"),
            _tx(operation="TRANSFER", asset="BTC", amount="0.00689548", source="Bybit",
                destination="Ledger", fee_amount="0.000057", fee_asset="BTC",
                timestamp="2026-10-21T13:45:00+00:00"),
            _tx(operation="SPOT_BUY", asset="BTC", amount="0.00695248", price="84861.76",
                quote_asset="USDT", total_quote_value="590", source="Bybit", destination="Bybit",
                timestamp="2026-10-21T13:28:00+00:00"),
            _tx(operation="P2P_BUY", asset="USDT", amount="1180", price="84.7458",
                quote_asset="RUB", source="Tbank", destination="Bybit",
                timestamp="2026-10-21T13:22:00+00:00"),
            _tx(operation="P2P_SELL", asset="USDT", amount="500", price="100",
                quote_asset="RSD", source="Bybit", destination="Raiffeisen",
                timestamp="2026-10-10T10:00:00+00:00"),
            _tx(operation="P2P_BUY", asset="USDT", amount="1080", price="87.963",
                quote_asset="RUB", source="Tbank", destination="Bybit",
                timestamp="2026-10-08T10:00:00+00:00"),
        ]

    def test_cycle_starts_at_latest_fiat_funded_buy(self):
        cycle, earlier = handler._split_current_cycle(self._history())
        self.assertEqual([t["operation"] for t in cycle],
                         ["P2P_BUY", "SPOT_BUY", "TRANSFER", "INCOME"])
        self.assertEqual(cycle[0]["amount"], "1180")
        self.assertEqual([t["timestamp"][:10] for t in earlier], ["2026-10-10", "2026-10-08"])

    def test_usdt_to_btc_conversion_is_not_an_entry(self):
        cycle, _ = handler._split_current_cycle(self._history()[:3])
        self.assertEqual(cycle, [])

    def test_context_lists_cycle_and_marks_reconcile(self):
        cycle, earlier = handler._split_current_cycle(self._history())
        state = accounting.replay([])
        ctx = handler._build_portfolio_context(state, earlier, cycle_txs=cycle)
        self.assertIn("=== TODAY ===", ctx)
        self.assertIn("CURRENT CYCLE: 4 TRANSACTIONS", ctx)
        self.assertIn("1. 2026-10-21 P2P_BUY 1180 USDT", ctx)
        self.assertIn("(total 590 USDT)", ctx)
        self.assertIn("(balance reconcile, not a trade)", ctx)
        self.assertIn("EARLIER 2 TRANSACTIONS", ctx)
