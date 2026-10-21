# `/strategy`: Find the Cycle in Code, Not in the LLM

Added 2026-10-21 (commit `adb0cce`).

## The bug

Ivan's strategy is cyclic: ENTRY = buy USDT with RUB, then convert 50% to
BTC, send part to Ledger, sell the rest after a 4-day hold. The advisor
(Haiku 4.5) got a flat "last 15 transactions, newest first" list and had to
work out the cycle itself. It failed in three ways on one day:

1. **No current date**: it invented "today ~2026-10-24, 3 days post-entry".
2. **Wrong entry**: it called the USDT→BTC conversion a "new ENTRY".
3. **After a prompt fix**, it anchored on an older entry (2026-10-08) and
   mixed that cycle's P2P sells into today's steps ("transfer from Bybit to Bybit").

Lesson: **prompt rules alone did not make Haiku segment history reliably.**
Do the deterministic part in code and give the LLM a pre-shaped list.

## The fix (`src/handler.py`)

- `_is_cycle_entry(tx)`: P2P_BUY or SPOT_BUY with `quote_asset` in
  `parser._FIAT`, not a reconcile. This is a fresh fiat input.
- `_split_current_cycle(txs_newest_first)` → `(cycle_oldest_first, earlier_newest_first)`.
  It splits at the latest entry and returns an empty cycle if none is found.
- `_cmd_strategy` fetches 30 txs, splits them, and passes `cycle_txs=` plus at
  most 8 earlier txs to `_build_portfolio_context`. If no entry is found, it
  falls back to the plain last-15 list.
- The prompt says: item 1 is the ENTRY, later items are steps of this cycle,
  ignore reconciles, mention earlier cycles only for a pending exit, and use TODAY.
  The answer has four parts: completed steps, deviations, next step, act-by date.

Context shape:

```
=== TODAY ===
2026-10-21 13:47 UTC
...
=== CURRENT CYCLE: 4 TRANSACTIONS SINCE THE ENTRY (oldest first) ===
  1. 2026-10-21 P2P_BUY 1180 USDT @ 84.7458 RUB (total 100000 RUB) [Tbank → Bybit]
  2. 2026-10-21 SPOT_BUY 0.00695248 BTC @ 84861.76 USDT (total 590 USDT) [Bybit → Bybit]
  3. 2026-10-21 TRANSFER 0.00689548 BTC [Bybit → Ledger] fee 0.000057 BTC
  4. 2026-10-21 INCOME 0.00017 BTC [ → Ledger] (balance reconcile, not a trade)

=== EARLIER 8 TRANSACTIONS (previous cycles, newest first) ===
  ...
```

## Assumptions and limits

- **Entry definition is hard-coded** as "crypto bought with fiat". It matches
  Ivan's strategy and the bot's example strategy. A strategy with a different
  entry (e.g. buying BTC directly with EUR as a *non*-entry) would need the rule
  to become configurable.
- **Overlapping cycles**: a new entry before the previous cycle's 4-day exit
  pushes the old cycle into EARLIER. The prompt tells the advisor to still
  mention a pending exit from there.
- **Escalation path** if the advisor still misjudges: use a stronger model
  only for `/strategy` and `/ask` (rare calls, small cost).

## Tests

`tests/test_review_fixes.py::TestStrategyCycle`: split at the latest fiat buy,
conversion is not an entry, and context labels (TODAY, cycle numbering,
total quote, reconcile marker).

Related: [[advisor-live-context]], [[advisor-context-note]].
