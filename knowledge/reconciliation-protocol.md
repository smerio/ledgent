# Reconciliation: keeping bot balances in sync with reality

> Updated 2026-05-18: `/set` improved with noise-word stripping, swap detection,
> and a clearer "nothing to do" message. See `/set` command caveats below.

The CSV importer can never be 100% accurate because the spreadsheet is the
real source of truth and exports lose precision, attribution, and some
transactions entirely. The bot accepts manual corrections via two equivalent
paths:

## `/set <asset> <location> <amount>`

Slash form. Computes `delta = amount − current_balance(asset, location)`
and writes a single INCOME (delta > 0) or EXPENSE (delta < 0) transaction
with `raw_text = "reconcile {asset}@{location} {current} → {target}: manual /set"`.

Multi-word locations need quoting:

```
/set BTC Ledger 1.23456789
/set USDT "Binance P2P" 0
/set BTC Blockchain 0
```

## Natural language ("`Ledger BTC = 1.23456789`")

The LLM parser is trained to recognize declarative-balance phrasing and
emit `operation: SET_BALANCE`. The handler routes it to the same
`_reconcile()` function. Trigger phrases include: `balance`, `is`, `=`,
`set to`, `should be`, `reconciliation`.

## How the engine treats reconcile entries

`apply_transaction()` in [`src/accounting.py`](../src/accounting.py) checks
for the substring `"reconcil"` in `raw_text`. When present:

- INCOME does NOT create a zero-cost lot (would otherwise inflate future PnL)
- EXPENSE does NOT add to the fee tally (would otherwise inflate `/pnl` fees)

This detection is **case-insensitive** and matches both `reconcile` (from
`/set`) and `reconciliation` (from the user's natural-language form).

## `/set` command caveats (parser behaviour as of 2026-05-18)

### Argument order: asset first, location second
`/set USDT Binance 0` — correct.
`/set Binance USDT 0` — the bot now detects the swap and replies with a
correction suggestion instead of silently doing nothing.

### Noise words are stripped from location
Users sometimes type `/set BTC UNKNOWN to 0` or `/set BTC Ledger = 1.395`.
The trailing preposition (`to`, `from`, `at`, `=`, `→`) is stripped before
the location name is looked up, so these parse identically to
`/set BTC UNKNOWN 0` and `/set BTC Ledger 1.395`.

### "Nothing to do" message shows the actual balance
If the computed delta is zero, the bot now replies:
`BTC@Ledger is already 1.23456789. Nothing to do.`
(Previously it showed the target, which was confusing when the command
was mis-parsed.)

## Operational gotchas

- **Case-insensitive location merge means `/set BTC ledger 0` zeros the same
  bucket as `/set BTC Ledger ...`**. Once data exists, lowercase and
  capitalised variants are the same bucket. Don't try to drain them
  separately.
- **Reconciles do not retroactively delete lots from earlier buys.** If you
  reconcile BTC@Binance to 0 to "remove" inventory, the BTC lots from prior
  SPOT_BUYs are still present and will still consume on the next SELL.
  Reconcile only adjusts the balance number reported by `/balance`.
- **Reconciles are immutable**, like any other transaction. `/wipe confirm`
  in the bot is the only way to undo (two-step: `/wipe` shows warning,
  `/wipe confirm` deletes all user data).

## When to wipe + re-import

- Importer logic changed in a way that affects existing rows (e.g. the
  xlsx-extras RUB→BTC price fix on 2026-05-17).
- Source/destination attribution was wrong on a previous import.
- You want to re-run with stricter / different anomaly handling.

Reconciliations made via `/set` persist across wipes only if you keep a
record outside DynamoDB; otherwise re-apply them after re-import.
