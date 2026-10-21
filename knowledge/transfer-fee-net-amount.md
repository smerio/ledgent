# Transfer Fees: Net Amount With the Fee on Top (`amount_is_net`)

Added 2026-10-21. Replaces the "manual transfers are Gross-Send" rule from
the 2026-05 Ghost Balance fix for every chat transfer logged from that date on.

## The bug

```
sent 0.00689548 btc from bybit to Ledger fee 0.000057 btc
```

Bybit held exactly 0.00695248 BTC (= 0.00689548 + 0.000057). After logging,
`/balance` still showed `Bybit: 0.000057` BTC: the fee was stranded on the source.

Root cause: two layers disagreed about what `amount` means.

| Layer | Meaning of TRANSFER `amount` |
|---|---|
| `parser.SYSTEM_PROMPT` | "the amount that arrives at destination" (net) |
| `accounting.apply_transaction` (chat rows) | gross amount sent; destination gets `amount - fee` |

So the source was debited only X, and the destination got X − Y instead of X.

## The fix

The webhook path in `handler.py` saves new chat TRANSFER rows with
`"amount_is_net": True`. In `accounting.apply_transaction`:

```python
is_net = tx.get("imported", False) or tx.get("amount_is_net", False)
if is_net:    # Net-Receive: source −(amount + fee), destination +amount
else:         # legacy chat rows: Gross-Send, source −amount, destination +(amount − fee)
```

- **Why a flag and not a global switch:** flipping all chat rows would change
  the replay of every older chat transfer with a same-asset fee and shift
  historical balances. The flag changes only rows written after 2026-10-21.
- Fees paid in a different asset (transfer USDC, fee ETH) are unaffected.
- The "Logged:" confirmation now adds `Debited from <src>: <amount + fee>`.
  That makes the convention visible, and a wrong reading can be fixed with `/undo`.

## Repairing a row logged before the fix

If it is still the last chat-logged tx: `/undo confirm`, then re-send the same
message after the deploy. Otherwise reconcile the source with `/set` (see
[[reconciliation-protocol]]).

## Test

`tests/test_accounting.py::test_chat_transfer_net_amount_drains_source`
(the exact 2026-10-21 numbers). The legacy behaviour stays covered by
`test_manual_transfer_deducts_fee_from_volume`.
