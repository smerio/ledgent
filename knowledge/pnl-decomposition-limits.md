# PnL asset/FX decomposition: when it is meaningful

The replay engine in [`src/accounting.py`](../src/accounting.py) reports realized
PnL in USD with a split into "asset price change" and "FX rate movement". The
formula per consumed lot:

```
pnl_asset_usd = (sell_price − cost_per_unit_quote) × usd_per_quote_at_buy × take
pnl_fx_usd    = sell_price × (usd_per_quote_at_sell − usd_per_quote_at_buy) × take
pnl_total_usd = pnl_asset_usd + pnl_fx_usd
```

`pnl_total_usd` is always arithmetically correct. The **decomposition** is
only physically meaningful when buy and sell share a `quote_asset`.

## When the split is intuitive

- Pure crypto trade — bought BTC for USDT, sold BTC for USDT:
  `usd_per_quote ≈ 1` on both sides; FX ≈ 0; all PnL attributed to asset price.
- Pure FX trade — bought USDT for RUB, sold USDT for RUB at different P2P
  rates: asset price (RUB/USDT) movement is the "asset" component (denominated
  at entry FX), FX rate movement (RUB/USD) is the "FX" component.

## When the split is noise

Cross-quote round trips — e.g. BTC bought in RUB, sold in USDT:

- `sell_price = 30,000` (USDT/BTC)
- `cost_per_unit_quote = 1,800,000` (RUB/BTC)
- The subtraction `30,000 − 1,800,000` is meaningless (different units)
- `pnl_asset` evaluates to a large negative number that doesn't correspond
  to anything physical
- `pnl_fx` compensates with a symmetric positive
- Net `pnl_total` is still correct in USD

Example: `asset = −$117,000`, `fx = +$167,000`, `total = +$50,000`.
Total is right; the two components are arithmetic artefacts.

## What this means in practice

- **Trust the total**, not the split, when reading `/pnl` for portfolios that
  trade across different quote currencies.
- **Don't try to "fix" the formula** without also redesigning the quote model.
  The only clean fix is to value each lot at entry in a common unit (USD)
  and re-value at sell time, then decompose by what changed — asset price
  in USD vs the quote currency's USD value — which requires looking up a USD
  market price for the **asset** (not just the quote) at both timestamps.
  We don't have that data for historical rows.
- **For new clean trades (everything USDT-quoted), the split is fine.**

## Potential future fix

Track per-lot the asset's market USD price at acquisition (`asset_price_usd_at_buy`)
in addition to the quote-denominated price. Then:

```
pnl_asset_usd = (asset_price_usd_at_sell − asset_price_usd_at_buy) × take
pnl_fx_usd    = pnl_total_usd − pnl_asset_usd
```

This requires either (a) a price oracle in the importer, or (b) the source
spreadsheet already carrying USD prices for non-stablecoin assets — which
`Budget.xlsx` does in some columns but inconsistently.
