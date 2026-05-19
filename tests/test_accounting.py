"""Unit tests for the USD-normalized accounting replay engine.

Run from the project root with:
    PYTHONPATH=src python3.12 -m unittest discover tests -v
"""
from __future__ import annotations

import sys
import unittest
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from accounting import ZERO, replay, unrealized_pnl_usd, total_fees_in  # noqa: E402


def _tx(**kwargs) -> dict:
    """Build a transaction dict with sensible defaults."""
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
        "raw_text": kwargs.get("raw_text", ""),
        "imported": kwargs.get("imported", False),
    }


class TestFifoConsumption(unittest.TestCase):
    """Partial sell spanning two FIFO lots, pure USDT-quoted (USDT ≈ USD)."""

    def test_partial_fill_across_multiple_lots(self):
        txs = [
            _tx(operation="SPOT_BUY", asset="BTC", amount="0.01", price="20000",
                quote_asset="USDT", source="Bybit", destination="Bybit",
                timestamp="2024-01-01T00:00:00Z"),
            _tx(operation="SPOT_BUY", asset="BTC", amount="0.02", price="30000",
                quote_asset="USDT", source="Bybit", destination="Bybit",
                timestamp="2024-02-01T00:00:00Z"),
            _tx(operation="SPOT_SELL", asset="BTC", amount="0.015", price="40000",
                quote_asset="USDT", source="Bybit", destination="Bybit",
                timestamp="2024-03-01T00:00:00Z"),
        ]
        state = replay(txs)
        # 0.01 @ 20000 → (40000-20000)*0.01 = 200; +0.005 @ 30000 → (40000-30000)*0.005 = 50
        self.assertEqual(state.realized_pnl_usd, Decimal("250"))
        # All movement is asset price; quote (USDT) ≈ USD throughout → fx component = 0
        self.assertEqual(state.realized_asset_pnl_usd, Decimal("250"))
        self.assertEqual(state.realized_fx_pnl_usd, Decimal("0"))
        self.assertEqual(state.total_balance("BTC"), Decimal("0.015"))


class TestPureFxTrade(unittest.TestCase):
    """USDT bought for RUB, later sold for RUB at a different P2P rate.

    All PnL should be FX (asset USDT ≈ USD on both legs).
    """

    def test_usdt_round_trip_rub(self):
        txs = [
            # Bought 1000 USDT at 82 RUB/USDT, market USDRUB=80
            _tx(operation="P2P_BUY", asset="USDT", amount="1000", price="82",
                quote_asset="RUB", source="Tbank", destination="Bybit P2P",
                quote_per_usd="80", timestamp="2024-01-01T00:00:00Z"),
            # Sold 600 USDT at 90 RUB/USDT, market USDRUB=85
            _tx(operation="P2P_SELL", asset="USDT", amount="600", price="90",
                quote_asset="RUB", source="Bybit P2P", destination="Tbank",
                quote_per_usd="85", timestamp="2024-04-01T00:00:00Z"),
        ]
        state = replay(txs)
        # Asset component: (90 - 82) RUB/USDT × (1/80) USD/RUB × 600 USDT = $60
        expected_asset = (Decimal("90") - Decimal("82")) * (Decimal("1") / Decimal("80")) * Decimal("600")
        # FX component: 90 RUB/USDT × (1/85 - 1/80) USD/RUB × 600 USDT ≈ -$39.70
        expected_fx = Decimal("90") * (Decimal("1") / Decimal("85") - Decimal("1") / Decimal("80")) * Decimal("600")
        self.assertEqual(state.realized_asset_pnl_usd, expected_asset)
        self.assertEqual(state.realized_fx_pnl_usd, expected_fx)
        self.assertEqual(state.realized_pnl_usd, expected_asset + expected_fx)


class TestMixedTrade(unittest.TestCase):
    """BTC bought with RUB, sold for USDT — split should be non-zero on both axes."""

    def test_btc_bought_rub_sold_usdt(self):
        txs = [
            # Bought 0.1 BTC at 1,800,000 RUB/BTC, USDRUB=80 → cost = $2250 USD/BTC
            _tx(operation="P2P_BUY", asset="BTC", amount="0.1", price="1800000",
                quote_asset="RUB", source="Tbank", destination="Binance",
                quote_per_usd="80", timestamp="2024-01-01T00:00:00Z"),
            # Sold 0.1 BTC at 30,000 USDT/BTC (USDT ≈ USD) → proceeds = $3000 USD/BTC
            _tx(operation="SPOT_SELL", asset="BTC", amount="0.1", price="30000",
                quote_asset="USDT", source="Binance", destination="Binance",
                quote_per_usd="1", timestamp="2024-06-01T00:00:00Z"),
        ]
        state = replay(txs)
        # Total: (30000*1 - 1800000/80) * 0.1 = (30000 - 22500) * 0.1 = 750
        self.assertEqual(state.realized_pnl_usd, Decimal("750"))
        # Asset component: (sell_price - buy_price) * usd_per_quote_buy * amount
        #   sell_price (USDT/BTC) = 30000, buy_price (RUB/BTC) = 1800000 — different units!
        # The formula evaluates (30000 - 1800000) * (1/80) * 0.1 = -2212.5 ← reflects nominal RUB drop
        # FX component fills the rest: 750 - (-2212.5) = 2962.5 USD attributed to FX/unit change.
        # This decomposition is exact arithmetically; the interpretation only stays meaningful
        # when buy/sell share a quote unit. We assert the total equals the components' sum.
        self.assertEqual(state.realized_pnl_usd,
                         state.realized_asset_pnl_usd + state.realized_fx_pnl_usd)


class TestTransferAndBalances(unittest.TestCase):
    """Transfers move balances between locations but do not realize PnL."""

    def test_transfer_no_pnl_but_locations_updated_imported(self):
        txs = [
            _tx(operation="SPOT_BUY", asset="BTC", amount="0.05", price="20000",
                quote_asset="USDT", source="Bybit", destination="Bybit",
                quote_per_usd="1", timestamp="2024-01-01T00:00:00Z"),
            _tx(operation="TRANSFER", asset="BTC", amount="0.03",
                fee_amount="0.0001", fee_asset="BTC",
                source="Bybit", destination="Ledger",
                imported=True, timestamp="2024-02-01T00:00:00Z"),
        ]
        state = replay(txs)
        self.assertEqual(state.realized_pnl_usd, ZERO)
        self.assertEqual(state.balances["BTC"]["Bybit"], Decimal("0.0199"))
        self.assertEqual(state.balances["BTC"]["Ledger"], Decimal("0.03"))
        self.assertEqual(state.total_balance("BTC"), Decimal("0.0499"))
        self.assertEqual(state.fees["BTC"], Decimal("0.0001"))

    def test_manual_transfer_deducts_fee_from_volume(self):
        txs = [
            _tx(operation="SPOT_BUY", asset="BTC", amount="0.05", price="20000",
                quote_asset="USDT", source="Bybit", destination="Bybit",
                quote_per_usd="1", timestamp="2024-01-01T00:00:00Z"),
            _tx(operation="TRANSFER", asset="BTC", amount="0.03",
                fee_amount="0.0001", fee_asset="BTC",
                source="Bybit", destination="Ledger",
                imported=False, timestamp="2024-02-01T00:00:00Z"),
        ]
        state = replay(txs)
        self.assertEqual(state.realized_pnl_usd, ZERO)
        self.assertEqual(state.balances["BTC"]["Bybit"], Decimal("0.02"))  # Gross amount deducted
        self.assertEqual(state.balances["BTC"]["Ledger"], Decimal("0.0299"))  # Destination receives amount - fee
        self.assertEqual(state.total_balance("BTC"), Decimal("0.0499"))
        self.assertEqual(state.fees["BTC"], Decimal("0.0001"))


class TestFeeAccumulation(unittest.TestCase):
    """Fees in different assets accumulate independently."""

    def test_fees_across_assets(self):
        txs = [
            _tx(operation="P2P_BUY", asset="USDT", amount="1000", price="82",
                quote_asset="RUB", source="Sberbank", destination="Bybit",
                fee_amount="1.3", fee_asset="USDT",
                quote_per_usd="80", timestamp="2024-01-01T00:00:00Z"),
            _tx(operation="TRANSFER", asset="BTC", amount="0.01",
                fee_amount="0.0002", fee_asset="BTC",
                source="Bybit", destination="Ledger",
                timestamp="2024-01-15T00:00:00Z"),
            _tx(operation="TRANSFER", asset="BTC", amount="0.005",
                fee_amount="0.00015", fee_asset="BTC",
                source="Ledger", destination="Cold",
                timestamp="2024-02-01T00:00:00Z"),
            _tx(operation="EXPENSE", asset="USDT", amount="5",
                source="Bybit", timestamp="2024-02-15T00:00:00Z"),
        ]
        state = replay(txs)
        self.assertEqual(state.fees["USDT"], Decimal("6.3"))
        self.assertEqual(state.fees["BTC"], Decimal("0.00035"))


class TestUnrealizedUsd(unittest.TestCase):
    """Unrealized PnL uses cost_per_unit_usd and a USD price lookup."""

    def test_unrealized(self):
        txs = [
            _tx(operation="SPOT_BUY", asset="BTC", amount="0.01", price="20000",
                quote_asset="USDT", source="Bybit", destination="Bybit",
                quote_per_usd="1", timestamp="2024-01-01T00:00:00Z"),
            _tx(operation="SPOT_BUY", asset="BTC", amount="0.02", price="30000",
                quote_asset="USDT", source="Bybit", destination="Bybit",
                quote_per_usd="1", timestamp="2024-02-01T00:00:00Z"),
        ]
        state = replay(txs)
        u = unrealized_pnl_usd(state, {"BTC": Decimal("50000")})
        # (50000-20000)*0.01 + (50000-30000)*0.02 = 300 + 400 = 700
        self.assertEqual(u, Decimal("700"))


class TestIncome(unittest.TestCase):
    """Non-reconcile INCOME creates a zero-cost lot whose later sale realizes full proceeds."""

    def test_income_then_sell(self):
        txs = [
            _tx(operation="INCOME", asset="ETH", amount="0.5",
                destination="Stake Pool", timestamp="2024-01-01T00:00:00Z"),
            _tx(operation="SPOT_SELL", asset="ETH", amount="0.5", price="3000",
                quote_asset="USDT", source="Stake Pool", destination="Bybit",
                quote_per_usd="1", timestamp="2024-02-01T00:00:00Z"),
        ]
        state = replay(txs)
        self.assertEqual(state.realized_pnl_usd, Decimal("1500"))


class TestTotalFeesIn(unittest.TestCase):
    """Fee summarization to a base currency uses price_lookup for non-base assets."""

    def test_total_fees_conversion(self):
        txs = [
            _tx(operation="TRANSFER", asset="BTC", amount="0.01",
                fee_amount="0.0001", fee_asset="BTC",
                source="A", destination="B", timestamp="2024-01-01T00:00:00Z"),
            _tx(operation="EXPENSE", asset="USDT", amount="3",
                source="A", timestamp="2024-01-02T00:00:00Z"),
        ]
        state = replay(txs)
        total = total_fees_in(state, "USDT", {"BTC": Decimal("50000")})
        # 0.0001 BTC * 50000 + 3 USDT = 5 + 3 = 8
        self.assertEqual(total, Decimal("8"))


class TestReconcilePollution(unittest.TestCase):
    """INCOME/EXPENSE tagged as 'reconcile' must not pollute fees or lots."""

    def test_reconcile_does_not_add_to_fees(self):
        txs = [
            _tx(operation="EXPENSE", asset="USDT", amount="100", source="Binance",
                timestamp="2024-01-01T00:00:00Z"),
            _tx(operation="EXPENSE", asset="USDT", amount="60000", source="Binance",
                raw_text="reconcile USDT@Binance 60000 → 0: manual /set",
                timestamp="2024-02-01T00:00:00Z"),
        ]
        state = replay(txs)
        self.assertEqual(state.fees["USDT"], Decimal("100"))
        self.assertEqual(state.balances["USDT"]["Binance"], Decimal("-60100"))

    def test_reconcile_income_does_not_create_zero_cost_lot(self):
        txs = [
            _tx(operation="INCOME", asset="BTC", amount="0.5", destination="Binance",
                raw_text="reconcile BTC@Binance -0.5 → 0: manual /set",
                timestamp="2024-01-01T00:00:00Z"),
            _tx(operation="SPOT_SELL", asset="BTC", amount="0.5", price="50000",
                quote_asset="USDT", source="Binance", destination="Binance",
                quote_per_usd="1", timestamp="2024-02-01T00:00:00Z"),
        ]
        state = replay(txs)
        self.assertEqual(state.realized_pnl_usd, Decimal("0"))


class TestLocationCaseInsensitive(unittest.TestCase):
    """'Ledger' and 'ledger' must merge into one bucket per asset."""

    def test_case_insensitive_merge(self):
        txs = [
            _tx(operation="INCOME", asset="BTC", amount="1.0", destination="Ledger",
                timestamp="2024-01-01T00:00:00Z"),
            _tx(operation="INCOME", asset="BTC", amount="0.5", destination="ledger",
                timestamp="2024-02-01T00:00:00Z"),
            _tx(operation="EXPENSE", asset="BTC", amount="0.1", source="LEDGER",
                timestamp="2024-03-01T00:00:00Z"),
        ]
        state = replay(txs)
        self.assertEqual(len(state.balances["BTC"]), 1)
        self.assertEqual(state.balances["BTC"]["Ledger"], Decimal("1.4"))


class TestHistoricalFees(unittest.TestCase):
    """Network fees in different assets must be valued historically using chronologically close prices."""

    def test_historical_fee_pricing(self):
        txs = [
            # Buy BTC in 2023 at $16,600
            _tx(operation="SPOT_BUY", asset="BTC", amount="0.06", price="16600",
                quote_asset="USDT", source="Binance", destination="Binance",
                timestamp="2023-01-01T12:00:00Z"),
            # Transfer BTC in 2023 with $16,600 price proximity
            _tx(operation="TRANSFER", asset="BTC", amount="0.06",
                fee_amount="0.0001", fee_asset="BTC",
                source="Binance", destination="Ledger",
                timestamp="2023-01-01T13:00:00Z"),
            # Transfer BTC back in 2025 before sell, price proximity to 2025 sell at $104,000
            _tx(operation="TRANSFER", asset="BTC", amount="0.059",
                fee_amount="0.00002", fee_asset="BTC",
                source="Ledger", destination="Binance",
                timestamp="2025-06-01T11:00:00Z"),
            # Sell BTC in 2025 at $104,000
            _tx(operation="SPOT_SELL", asset="BTC", amount="0.059", price="104000",
                quote_asset="USDT", source="Binance", destination="Binance",
                timestamp="2025-06-01T12:00:00Z"),
        ]
        state = replay(txs)
        # Expected fee valuation:
        # 1st BTC fee: 0.0001 * 16600 = $1.66
        # 2nd BTC fee: 0.00002 * 104000 = $2.08
        # Total USD fees = $3.74
        self.assertEqual(total_fees_in(state, "USD"), Decimal("3.74"))


class TestZombieLotsAndScaling(unittest.TestCase):
    """Test that zero balances clear unrealized PnL and scaling protects against drifts."""

    def test_zombie_lot_cleared_on_zero_balance(self):
        txs = [
            _tx(operation="SPOT_BUY", asset="BTC", amount="1", price="50000",
                quote_asset="USDT", source="Binance", destination="Binance",
                quote_per_usd="1", timestamp="2024-01-01T00:00:00Z"),
            # Spend entire BTC as an expense, which reduces BTC balance to zero
            _tx(operation="EXPENSE", asset="BTC", amount="1", source="Binance",
                timestamp="2024-02-01T00:00:00Z"),
        ]
        state = replay(txs)
        self.assertEqual(state.total_balance("BTC"), ZERO)
        u = unrealized_pnl_usd(state, {"BTC": Decimal("60000")})
        # Balance is zero, so unrealized PnL must be exactly zero
        self.assertEqual(u, ZERO)

    def test_scaling_down_on_inventory_drift(self):
        txs = [
            _tx(operation="SPOT_BUY", asset="BTC", amount="1", price="50000",
                quote_asset="USDT", source="Binance", destination="Binance",
                quote_per_usd="1", timestamp="2024-01-01T00:00:00Z"),
            # Spend half BTC as an expense, which reduces balance and lots to 0.5
            _tx(operation="EXPENSE", asset="BTC", amount="0.5", source="Binance",
                timestamp="2024-02-01T00:00:00Z"),
        ]
        state = replay(txs)
        self.assertEqual(state.total_balance("BTC"), Decimal("0.5"))
        # Sum of lot amount remaining is 0.5. Current price is 60000.
        # Unrealized PnL = (60000 - 50000) * 0.5 = 5000
        u = unrealized_pnl_usd(state, {"BTC": Decimal("60000")})
        self.assertEqual(u, Decimal("5000"))


class TestStablecoinFiatCostBasis(unittest.TestCase):
    """Test that stablecoins bought with unpegged fiat infer their USD exchange rate to keep cost basis at $1.00."""

    def test_phantom_stablecoin_profit_prevented(self):
        txs = [
            # Buy USDT with RSD, without specifying any USD exchange rate.
            # Price = 100 RSD / USDT
            _tx(operation="P2P_BUY", asset="USDT", amount="1000", price="100",
                quote_asset="RSD", source="Bank", destination="Bybit",
                timestamp="2024-01-01T00:00:00Z"),
        ]
        state = replay(txs)
        # Verify total balance
        self.assertEqual(state.total_balance("USDT"), Decimal("1000"))
        # Verify unrealized PnL is 0 if current price is 1.0
        u = unrealized_pnl_usd(state, {"USDT": Decimal("1.0")})
        self.assertEqual(u, ZERO)
        # Check explicit cost basis
        lots = state.open_lots["USDT"]
        self.assertEqual(len(lots), 1)
        self.assertEqual(lots[0].cost_per_unit_usd, Decimal("1.0"))


if __name__ == "__main__":
    unittest.main()
