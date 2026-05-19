# Advisor Context: Per-Lot Detail and Live Prices

Added 2026-05-19. Documents what `_build_portfolio_context()` now includes
and why, plus the companion `/sim sell` command.

## What's in the advisor context (as of 2026-05-19)

```
=== BALANCES ===
BTC: X.XXXX (A.AA@Exchange1, B.BB@Exchange2)
...

=== OPEN LOTS — BTC (FIFO order, oldest first) ===
  YYYY-MM-DD  0.00XXX BTC @ $XX,XXX/unit  [Exchange1]
  YYYY-MM-DD  0.0XXXX BTC @ $XX,XXX/unit  [Exchange1]
  ...  (up to 20 lots)
  ... N more lots: X.XX BTC @ avg $YY,YYY/unit
  Total: X.XXXX BTC, avg $XX,XXX/unit

=== STABLECOIN / FIAT POSITIONS (aggregate cost basis) ===
  USDT: XXXX.XX @ avg $1.00/unit
  EUR: ...

=== LIVE PRICES (current market) ===
  BTC: $XX,XXX USD

=== LAST 25 TRANSACTIONS (newest first) ===
  ...

=== USER INSTRUCTIONS FOR ADVISOR ===
  (if set via /context)
```

## Why per-lot detail for crypto only

Stablecoins (USDT, USDC, DAI) and fiat (EUR, RUB, USD) can have hundreds
of open lots from P2P transactions. Including them in full would bloat the
LLM prompt and add no useful information (their cost basis is ~$1 per unit).

`_STABLE_OR_FIAT = {"USDT", "USDC", "DAI", "BUSD", "USD", "EUR", "RUB", "GBP", "CHF"}`

Assets NOT in this set get full per-lot detail.

## Why live prices

Without the current price, the LLM asked the user for it — adding a round
trip to every sell PnL question. `_cmd_ask` now calls `_live_prices()` for
all crypto assets with open lots before building the context:

```python
crypto_assets = [a for a in state.open_lots if a in COINGECKO_IDS]
live_prices = _live_prices(user_id, crypto_assets, "USD") if crypto_assets else {}
context = _build_portfolio_context(state, recent_txs, advisor_note=advisor_note,
                                   live_prices=live_prices)
```

Prices are CoinGecko free-tier with a 5-minute DynamoDB TTL cache
(`put_cached_price` / `get_cached_price`). The fetch adds ~0-200ms
(cached: ~50ms; live fetch: ~300ms with 6s timeout).

## LLM arithmetic limitation

For sells spanning **a single lot**, the LLM computes PnL accurately.
For sells spanning **multiple lots** (e.g., 1 BTC across 12 lots), the LLM
correctly identifies which lots are consumed (FIFO) but its per-lot PnL
arithmetic is unreliable — it may show grossly wrong individual numbers
while the total happens to be approximately correct.

→ Use `/sim sell <amount> <asset>` for precise multi-lot calculations.

## `/sim sell` command

Pure accounting engine, zero LLM. Lives in `_cmd_sim` in `src/handler.py`.

```
/sim sell 1 BTC
→
FIFO sell simulation: 1 BTC @ $XX,XXX

`YYYY-MM-DD`  0.00XXX BTC  @$XX,XXX  +$XXX.XX
`YYYY-MM-DD`  0.0XXXX BTC  @$XX,XXX  +$X,XXX.XX
...  (up to 15 rows)

Cost basis:  $XX,XXX.XX
Proceeds:    $XX,XXX.XX
Net PnL:     +$X,XXX.XX  (+X.X%)
```

Handles: partial fills (sell > holdings), assets not in COINGECKO_IDS
(shows cost only, no proceeds), up to 15 detail rows + a summary line for
the rest.
