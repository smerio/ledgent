# Unit-Safe Average Buy Price Calculation and Decimal Safety Patterns

This document details the architectural solutions implemented to solve two key database and command execution issues:
1. **The Average Buy Price Mismatch** (Mixed quote currency summation dimension error)
2. **The Decimal ConversionSyntax Bug** (Exception on None and empty string database fields)

---

## 1. Unit-Safe Average Buy Price Normalization (`/stats`)

### Problem
Previously, the `/stats <asset>` command calculated the average buy price using a simple direct formula: `Σ(amount * price) / Σamount`.
While mathematically correct for transactions denominated in a single quote currency, this produced highly inflated and incorrect averages for portfolios with mixed quote currencies (e.g. buying BTC with `RUB`, `USD`, and `USDT` combined). Directly summing these amounts violates dimensional analysis (e.g. adding `100,000 RUB` directly to `1,000 USD`), causing average buy prices to explode (e.g. reporting `522,557.87 USDT` for BTC instead of `45,755.00 USDT`).

### Solution: USD-Normalized Weighted Average Buy Price
We redesigned `_cmd_stats` in `src/handler.py` to be unit-safe and run in strict read-only mode over the production database:

1. **Chronological Trade Price Index Builder**:
   We scan all historical user transactions to build a chronological ledger of trade prices mapped to their USD equivalents:
   ```python
   prices_by_asset: dict[str, list[tuple[str, Decimal]]] = {}
   for tx in all_txs:
       op = tx.get("operation")
       tx_asset = tx.get("asset")
       p = _D(tx.get("price"))
       usd_per_quote = accounting._usd_per_quote(tx)
       ts = tx.get("timestamp", "")
       if op in ("P2P_BUY", "P2P_SELL", "SPOT_BUY", "SPOT_SELL") and p > Decimal("0") and usd_per_quote > Decimal("0"):
           prices_by_asset.setdefault(tx_asset, []).append((ts, p * usd_per_quote))
   ```

2. **USD Exchange Rate Inference for Incomplete Records**:
   For transactions that don't have an explicit `usd_per_quote` or are denominated in non-standard/fiat quotes (e.g., `RUB`), we infer the historical USD exchange rate using:
   - **Stablecoins**: `usd_per_quote = 1 / price` if quote asset is a stablecoin.
   - **Unpegged Assets**: Finding the closest chronological USD-valued trade price of the asset at the transaction's timestamp:
     `usd_per_quote = usd_asset_price / price_in_quote`

3. **USD Normalization & Primary Quote Conversion**:
   - Normalize every buy's cost in USD: `amount * price * usd_per_quote`.
   - Compute the weighted average buy price in USD: `avg_buy_price_usd = total_cost_usd / total_amount`.
   - Determine the portfolio's **primary quote currency** (the most frequently used quote asset, e.g., `USDT`).
   - Extract the latest known USD conversion rate for that primary quote asset.
   - Convert the weighted average buy price back to the primary quote currency: `avg_buy_price = avg_buy_price_usd / latest_rate`.

---

## 2. Robust Decimal Safe-Parser (`None` / Empty String Handling)

### Problem
In the production database, some transaction operations (like `TRANSFER`, `INCOME`, `EXPENSE`, or manual `reconciliation` logs) do not contain values for fields like `price`, `amount`, or `quote_asset`.
When the bot parsed these fields in `_cmd_stats`, standard conversions using `Decimal(str(tx.get("price", 0)))` evaluated to either `Decimal("None")` or `Decimal("")`. Under the `decimal` library, these string conversions throw:
`decimal.InvalidOperation: [<class 'decimal.ConversionSyntax'>]`
This caused the entire `/stats` command to crash instantly in production.

### Solution: Local Robust `_D(val)` Safe-Parser
We introduced a custom local helper `_D(val)` inside the `/stats` handler to gracefully handle database gaps:

```python
def _D(val) -> Decimal:
    if val is None or val == "":
        return Decimal("0")
    if isinstance(val, Decimal):
        return val
    try:
        return Decimal(str(val))
    except Exception:
        return Decimal("0")
```

### Why a local helper is used:
Although the project contains a global `accounting._D` utility, it only handles `None` values and does not catch empty strings `""` or other malformed formats. Having a localized, Exception-safe helper inside the stats command provides complete isolation and robust protection against any unexpected formats stored in the production transaction logs.
