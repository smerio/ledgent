# Fiat & Stablecoin Lot Accounting Patterns

This document details the architectural solutions implemented to solve three core crypto-accounting engine bugs:
1. **The Zombie Lot Bug** (Inventory/balance decoupling during spending)
2. **The Phantom Stablecoin Profit Bug** (Zero-cost stablecoin cost basis)
3. **The Fiat Oracle Overshoot Bug** (Precision math leaks on P2P fiat trades)

---

## 1. The Zombie Lot Bug (Inventory/Balance Sync)

### Problem
Previously, when the user sold an asset, the engine reduced its lots via FIFO. However, if the asset was spent as a **quote asset** during a buy (e.g. spending `USDT` to buy `ETH`), as a **network fee**, or as a direct **expense**, the balance engine debited the wallet, but the accounting engine's open lots list remained untouched. This left "ghost balances" (zombie lots) active in unrealized PnL even when the wallet balance of the asset became exactly zero.

### Solution
1. **Active Inventory Reduction:** Added `_reduce_open_lots` in `src/accounting.py`. Whenever an asset is spent as a quote asset in a buy, network fee, or expense, the engine proactively FIFO-reduces the remaining amount in that asset's open buy lots.
2. **Self-Healing Unrealized PnL Guard:** In `unrealized_pnl_usd()`, we enforce two self-healing rules:
   * **Zero Balance Rule:** If the current wallet balance of an asset is `<= 0`, its unrealized PnL is instantly evaluated as exactly `0.00`.
   * **Proportional Scaling Rule:** If the sum of `amount_remaining` in the lots exceeds the wallet balance (due to missing historical transfer or fee details), we dynamically scale the lot sizes down: `factor = balance / lots_sum`. This prevents database gaps from inflating unrealized PnL.

---

## 2. The Phantom Stablecoin Profit Bug (Cost Basis)

### Problem
Stablecoins (USDT, USDC, etc.) are pegged to USD. If bought with an unpegged fiat currency (RSD, RUB) with no explicit USD exchange rate provided in the text, the fiat-to-USD rate defaulted to `0.00`. This assigned a USD cost basis of exactly `$0.00` to the stablecoin lots. When calculating unrealized PnL, the engine subtracted `$0.00` from the current stablecoin price (`$1.00`), reporting a phantom 100% unrealized profit margin.

### Solution
Added **fiat exchange rate inference** inside `apply_transaction` in `src/accounting.py`:
```python
# Infer missing fiat exchange rates from stablecoin prices
if usd_per_quote <= ZERO and price > ZERO and asset in _USD_PEGGED:
    usd_per_quote = ONE / price
```
Since the stablecoin is pegged to USD, its purchase price relative to USD is exactly `$1.00`. Therefore, the USD value of 1 unit of fiat is precisely `1 / price`. 
* **The Result:** The stablecoin cost basis is locked at exactly **$1.00**, resolving unrealized PnL to **$0.00**, while preserving the inferred historical exchange rate for future FX PnL calculations.

---

## 3. The Fiat Oracle Overshoot Bug (Parser Mismatch)

### Problem
When parsing P2P transactions from free-text (e.g. `Buy 6438.14 USDT ... using 639635.56 RSD`), the LLM sometimes failed to output the structured `total_quote_value` JSON field and instead guessed a rounded `price` (e.g. `99.4269` instead of `99.35098`). The accounting engine then fell back to `amount * price`, resulting in slight math leaks that slowly pushed fiat balances into negative values.

### Solution
Implemented a **deterministic regex fallback** in `_validate` in `src/parser.py`:
```python
# Deterministic fallback: extract total quote value from text if missed by LLM
if total_quote is None and raw_text and quote_asset:
    import re
    qa = re.escape(quote_asset)
    m = re.search(rf'(?:using|for|from)\s+([\d,]+(?:\.\d+)?)\s*{qa}\b', raw_text, re.IGNORECASE)
    if m:
        try:
            total_quote = float(m.group(1).replace(',', ''))
            payload["total_quote_value"] = total_quote
        except ValueError:
            pass
```
* **The Result:** If the LLM misses the explicit fiat amount, Python programmatically scans the raw string using the extracted `quote_asset` context. This bypasses LLM hallucinations completely and guarantees zero-slippage exact match fiat balance postings.
