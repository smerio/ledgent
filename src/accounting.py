"""USD-normalized replay engine for the crypto ledger.

Every transaction carries `usd_per_quote` — the USD value of 1 unit of its
`quote_asset` at the transaction's time. Every BUY creates one `Lot` with
both the original-quote price and the USD-equivalent cost basis. Every SELL
consumes lots FIFO and computes realized PnL purely in USD, decomposed into
two components:

  pnl_asset_usd = (sell_price − cost_per_unit_quote) × usd_per_quote_at_buy
  pnl_fx_usd    = sell_price × (usd_per_quote_at_sell − usd_per_quote_at_buy)
  total         = pnl_asset_usd + pnl_fx_usd

The engine is stateless and free of DynamoDB dependencies so it can be unit
tested and re-used as the on-the-fly compute path for /balance, /pnl, etc.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Iterable


ZERO = Decimal("0")
ONE = Decimal("1")

# Assets treated as USD-equivalent when no explicit rate is given.
_USD_PEGGED = {"USD", "USDT", "USDC", "DAI", "BUSD", "FDUSD", "TUSD"}


def _D(value) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if value is None:
        return ZERO
    return Decimal(str(value))


def _usd_per_quote(tx: dict) -> Decimal:
    """Return USD value of 1 unit of the transaction's quote asset.

    Prefers the explicit `usd_per_quote` field. Falls back to the legacy
    `quote_per_usd` (fiat per USD) by taking the reciprocal. Falls back to
    1.0 for USD-pegged stablecoins. Returns 0 when truly unknown so the
    caller can decide whether to compute USD PnL.
    """
    explicit = _D(tx.get("usd_per_quote", 0))
    if explicit > ZERO:
        return explicit
    legacy = _D(tx.get("quote_per_usd", 0))
    if legacy > ZERO:
        return ONE / legacy
    quote = (tx.get("quote_asset") or "").upper()
    if quote in _USD_PEGGED:
        return ONE
    return ZERO


def _get_historical_price(asset: str, timestamp: str, price_index: dict[str, list[tuple[str, Decimal]]]) -> Decimal | None:
    """Find the closest trade price for asset near timestamp."""
    if not asset or not timestamp:
        return None
    if asset in _USD_PEGGED:
        return ONE
    known = price_index.get(asset)
    if not known:
        return None
    
    best_price = None
    min_diff = None
    for ts, price in known:
        try:
            # Parse ISO timestamps
            dt_a = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            dt_b = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            diff = abs((dt_a - dt_b).total_seconds())
        except Exception:
            diff = abs(len(ts) - len(timestamp))
            
        if min_diff is None or diff < min_diff:
            min_diff = diff
            best_price = price
            
    return best_price


@dataclass
class Lot:
    lot_id: str
    asset: str
    amount_remaining: Decimal
    cost_per_unit_quote: Decimal     # original price in `quote_asset`
    cost_per_unit_usd: Decimal       # = cost_per_unit_quote × usd_per_quote_at_buy
    usd_per_quote_at_buy: Decimal    # stored explicitly to avoid round-trip precision loss
    quote_asset: str
    acquired_at: str
    location: str


@dataclass
class ReplayState:
    base_currency: str = "USD"
    balances: dict = field(default_factory=dict)            # asset -> {location: Decimal}
    open_lots: dict = field(default_factory=dict)           # asset -> [Lot, ...]
    realized_pnl_usd: Decimal = ZERO                        # total realised PnL in USD
    realized_asset_pnl_usd: Decimal = ZERO                  # asset-price component
    realized_fx_pnl_usd: Decimal = ZERO                     # FX-rate component
    fees: dict = field(default_factory=dict)                # asset -> Decimal
    realized_fees_usd: Decimal = ZERO                       # historically valued fees in USD
    transactions_processed: int = 0

    def add_balance(self, asset: str, location: str, delta: Decimal) -> None:
        if not asset:
            return
        loc = (location or "UNKNOWN").strip()
        bucket = self.balances.setdefault(asset, {})
        # Case-insensitive merge so "Ledger" and "ledger" become one bucket;
        # preserve whichever casing was seen first per asset.
        key = loc
        lower = loc.lower()
        for existing in bucket:
            if existing.lower() == lower:
                key = existing
                break
        bucket[key] = bucket.get(key, ZERO) + delta

    def add_fee(self, asset: str, amount: Decimal, timestamp: str = "", price_index: dict | None = None) -> None:
        if amount <= ZERO or not asset:
            return
        self.fees[asset] = self.fees.get(asset, ZERO) + amount
        
        # Value historical fee in USD
        if asset in _USD_PEGGED:
            self.realized_fees_usd += amount
        else:
            price = None
            if price_index:
                price = _get_historical_price(asset, timestamp, price_index)
            if price is not None:
                self.realized_fees_usd += amount * price

    def total_balance(self, asset: str) -> Decimal:
        return sum(self.balances.get(asset, {}).values(), ZERO)


def _consume_and_book(state: ReplayState, lots: list[Lot], amount: Decimal,
                      sell_price: Decimal, sell_usd_per_quote: Decimal) -> None:
    """FIFO-consume `amount` units. Book USD PnL with asset/FX decomposition."""
    remaining = amount
    while remaining > ZERO and lots:
        lot = lots[0]
        take = min(lot.amount_remaining, remaining)

        if lot.cost_per_unit_quote > ZERO and sell_usd_per_quote > ZERO:
            buy_usd_per_quote = lot.usd_per_quote_at_buy
            pnl_asset = (sell_price - lot.cost_per_unit_quote) * buy_usd_per_quote * take
            pnl_fx = sell_price * (sell_usd_per_quote - buy_usd_per_quote) * take
        else:
            # INCOME-sourced (zero-cost) lot or unknown rate — book full
            # USD proceeds as asset-side gain, FX component zero.
            pnl_asset = sell_price * sell_usd_per_quote * take
            pnl_fx = ZERO

        state.realized_asset_pnl_usd += pnl_asset
        state.realized_fx_pnl_usd += pnl_fx
        state.realized_pnl_usd += pnl_asset + pnl_fx

        lot.amount_remaining -= take
        remaining -= take
        if lot.amount_remaining <= ZERO:
            lots.pop(0)


def _reduce_open_lots(lots: list[Lot], amount: Decimal) -> None:
    """Consume `amount` from `lots` without booking PnL (to keep inventory in sync)."""
    remaining = amount
    while remaining > ZERO and lots:
        lot = lots[0]
        take = min(lot.amount_remaining, remaining)
        lot.amount_remaining -= take
        remaining -= take
        if lot.amount_remaining <= ZERO:
            lots.pop(0)


def apply_transaction(state: ReplayState, tx: dict, price_index: dict | None = None) -> None:
    """Mutate `state` by applying a single transaction."""
    op = tx["operation"]
    asset = tx.get("asset") or ""
    amount = _D(tx.get("amount", 0))
    quote = tx.get("quote_asset") or ""
    price = _D(tx.get("price", 0))
    src = tx.get("source") or ""
    dst = tx.get("destination") or ""
    fee_amount = _D(tx.get("fee_amount", 0))
    fee_asset = tx.get("fee_asset") or asset
    ts = tx.get("timestamp", "")
    tx_id = tx.get("tx_id") or f"tx-{ts}"
    usd_per_quote = _usd_per_quote(tx)

    # Infer missing fiat exchange rates from stablecoin prices
    if usd_per_quote <= ZERO and price > ZERO and asset in _USD_PEGGED:
        usd_per_quote = ONE / price

    # Determine total quote value (with fallback to amount * price)
    total_quote = _D(tx.get("total_quote_value", 0))
    if total_quote <= ZERO and price > ZERO:
        total_quote = amount * price

    if op in ("P2P_BUY", "SPOT_BUY"):
        state.add_balance(asset, dst, amount)
        if quote:
            state.add_balance(quote, src, -total_quote)
            _reduce_open_lots(state.open_lots.setdefault(quote, []), total_quote)
        if fee_amount > ZERO:
            fee_loc = dst if fee_asset == asset else src
            state.add_balance(fee_asset, fee_loc, -fee_amount)
            state.add_fee(fee_asset, fee_amount, timestamp=ts, price_index=price_index)

        state.open_lots.setdefault(asset, []).append(Lot(
            lot_id=tx_id,
            asset=asset,
            amount_remaining=amount,
            cost_per_unit_quote=price,
            cost_per_unit_usd=price * usd_per_quote,
            usd_per_quote_at_buy=usd_per_quote,
            quote_asset=quote or state.base_currency,
            acquired_at=ts,
            location=dst,
        ))

        if fee_amount > ZERO:
            _reduce_open_lots(state.open_lots.setdefault(fee_asset, []), fee_amount)

    elif op in ("P2P_SELL", "SPOT_SELL"):
        state.add_balance(asset, src, -amount)
        if quote:
            state.add_balance(quote, dst, total_quote)
        if fee_amount > ZERO:
            fee_loc = src if fee_asset == asset else dst
            state.add_balance(fee_asset, fee_loc, -fee_amount)
            state.add_fee(fee_asset, fee_amount, timestamp=ts, price_index=price_index)
        lots = state.open_lots.setdefault(asset, [])
        _consume_and_book(state, lots, amount, price, usd_per_quote)

        if fee_amount > ZERO:
            _reduce_open_lots(state.open_lots.setdefault(fee_asset, []), fee_amount)

    elif op == "TRANSFER":
        is_imported = tx.get("imported", False)
        if fee_asset == asset:
            if is_imported:
                # Imported (Case 2: Net-Receive): amount is the net amount received.
                # Source is debited the gross amount (amount + fee).
                state.add_balance(asset, src, -(amount + fee_amount))
                state.add_balance(asset, dst, amount)
            else:
                # Manual (Case 1: Gross-Send): amount is the gross amount sent.
                # Destination receives the net amount (amount - fee).
                state.add_balance(asset, src, -amount)
                state.add_balance(asset, dst, amount - fee_amount)
        else:
            # Fee in a different asset (e.g. transfer USDC, fee ETH)
            state.add_balance(asset, src, -amount)
            state.add_balance(asset, dst, amount)
            if fee_amount > ZERO:
                state.add_balance(fee_asset, src, -fee_amount)

        if fee_amount > ZERO:
            state.add_fee(fee_asset, fee_amount, timestamp=ts, price_index=price_index)
            _reduce_open_lots(state.open_lots.setdefault(fee_asset, []), fee_amount)

    elif op == "INCOME":
        state.add_balance(asset, dst, amount)
        # A reconciliation is a pure balance correction — don't create a
        # zero-cost lot that would inflate future PnL on the next sell.
        if "reconcil" not in (tx.get("raw_text") or "").lower():
            state.open_lots.setdefault(asset, []).append(Lot(
                lot_id=tx_id,
                asset=asset,
                amount_remaining=amount,
                cost_per_unit_quote=ZERO,
                cost_per_unit_usd=ZERO,
                usd_per_quote_at_buy=ONE,   # zero-cost lot, quote = base currency
                quote_asset=state.base_currency,
                acquired_at=ts,
                location=dst,
            ))

    elif op == "EXPENSE":
        state.add_balance(asset, src, -amount)
        # Reconciliations are pure balance corrections, not real fees.
        if "reconcil" not in (tx.get("raw_text") or "").lower():
            state.add_fee(asset, amount, timestamp=ts, price_index=price_index)
        _reduce_open_lots(state.open_lots.setdefault(asset, []), amount)

    elif op == "STAKE":
        staked_loc = f"{src}:STAKED"
        state.add_balance(asset, src, -amount)
        state.add_balance(asset, staked_loc, amount)

    elif op == "UNSTAKE":
        staked_loc = f"{dst}:STAKED"
        state.add_balance(asset, staked_loc, -amount)
        state.add_balance(asset, dst, amount)


def replay(transactions: Iterable[dict], base_currency: str = "USD") -> ReplayState:
    # Build a price index of known trade prices to value historical fees
    prices_by_asset: dict[str, list[tuple[str, Decimal]]] = {}
    for tx in transactions:
        op = tx.get("operation")
        asset = tx.get("asset")
        price = _D(tx.get("price", 0))
        usd_per_quote = _usd_per_quote(tx)
        ts = tx.get("timestamp", "")
        if op in ("P2P_BUY", "P2P_SELL", "SPOT_BUY", "SPOT_SELL") and price > ZERO and usd_per_quote > ZERO:
            prices_by_asset.setdefault(asset, []).append((ts, price * usd_per_quote))
            
    # Sort entries chronologically for closest search
    for asset in prices_by_asset:
        prices_by_asset[asset].sort(key=lambda x: x[0])

    state = ReplayState(base_currency=base_currency)
    txs = sorted(transactions, key=lambda t: (t.get("timestamp", ""), t.get("tx_id", "")))
    for tx in txs:
        apply_transaction(state, tx, price_index=prices_by_asset)
        state.transactions_processed += 1
    return state


def unrealized_pnl_usd(state: ReplayState, price_lookup: dict) -> Decimal:
    """Sum (current_USD_price − cost_per_unit_usd) × amount_remaining across open lots.

    `price_lookup` maps asset symbol → current price in USD. Assets without
    a price entry are skipped.
    """
    total = ZERO
    for asset, lots in state.open_lots.items():
        bal = state.total_balance(asset)
        if bal <= ZERO:
            continue
        if asset not in price_lookup:
            continue
        price = _D(price_lookup[asset])

        lots_sum = sum(lot.amount_remaining for lot in lots)
        factor = ONE
        if lots_sum > bal and lots_sum > ZERO:
            factor = bal / lots_sum

        for lot in lots:
            total += (price - lot.cost_per_unit_usd) * lot.amount_remaining * factor
    return total


def total_fees_in(state: ReplayState, base_currency: str, price_lookup: dict | None = None) -> Decimal:
    """Sum all fees historically valued, or convert via price_lookup if historical value not tracked."""
    if base_currency == "USD" and hasattr(state, "realized_fees_usd"):
        return state.realized_fees_usd
    
    # Fallback to legacy calculation if not USD
    total = ZERO
    price_lookup = price_lookup or {}
    for asset, amount in state.fees.items():
        if asset == base_currency:
            total += amount
        elif asset in _USD_PEGGED and base_currency == "USD":
            total += amount
        elif asset in price_lookup:
            total += amount * _D(price_lookup[asset])
    return total
