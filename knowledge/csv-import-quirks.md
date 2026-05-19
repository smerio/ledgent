# CSV / xlsx import quirks discovered

The historical data in this project (9 CSVs from Google Sheets + a `Budget.xlsx`
with computed sheets) has several traps that bit us. Documenting them so the
importer doesn't get patched to "fix" what's intentional.

## 1. Numbers: comma vs dot

`scripts/import_historical_csv.py::parse_number` heuristic:

- Both `,` and `.` present → `,` is thousand separator (`-4,936.20` = 4936.20)
- Multiple `,`s → all thousand (`1,234,567`)
- Single `,` followed by exactly 3 digits → thousand (`25,747` = 25747)
- Single `,` followed by 1, 2, or 4+ digits → decimal (`82,99` = 82.99)

The `25,747` case is the one that bit us — the early naive `,→.` rule
parsed it as `25.747` and threw spot prices off by 1000×. Don't simplify the
heuristic without re-confirming.

## 2. BTC amounts > 10 are typos

Some CSV rows contain `BTC in = 31` or `Sell BTC = -31` (clearly missing a
decimal point) where the USD value implies ~0.031 BTC.

Heuristic: any BTC amount > 10 is treated as a data error.

- For transfers: skip the row with a warning.
- For sells (`crypto-to-stable`): if `Sell USD` and `P2P rate $` are
  available, recover the BTC amount as `Sell USD / P2P rate`. Otherwise skip.

## 3. Transfer fee anomalies

Some transfer CSV rows record `Fee in BTC = -1` where the correct fee is
`out − in = 0.001`. The `-1` is a typo. The importer detects when
`fee > 50% of out` or `|fee − (out − in)| > (out − in) + 0.0001` and
substitutes `derived_fee = out − in`.

## 4. Wrong field semantics in `Budget.xlsx`

In the "Sell RUB buy BTC" section of the `History Crypto` sheet:

- Column F is labelled "P2P rate" but is actually **USD per BTC** (the
  market BTC price), not RUB per BTC.
- The true RUB/BTC rate is `sell_rub / buy_btc`.
- USDRUB (col C) is the market FX rate to use for `quote_per_usd`.

`scripts/import_xlsx_extras.py` accounts for this; do not "fix" it by
reading col F as the quote-side price.

## 5. Source/destination not in CSVs

None of the CSVs carry exchange/wallet names. Every importer invocation
takes `--source` and `--destination` flags. Wrong attributions silently
create negative balances in locations that never held that asset (seen
when `usdt-btc.csv` was imported with `--source Bybit` when buys actually
happened on Binance).

Example mapping (adjust for your own exchange/wallet names):

| CSV | source | destination |
|---|---|---|
| `fiat-usdt.csv` | Tbank | Binance P2P |
| `usdt_transfer.csv` | Binance | Ledger |
| `usdt_transfer2.csv` | Ledger | Binance |
| `usdt_transfer3.csv` | Binance | Ledger |
| `usdt-btc.csv` | Binance | Binance |
| `btc-transfer.csv` | Binance | Ledger |
| `btc-transfer2.csv` | Ledger | Binance |
| `btc-usdt.csv` | Binance | Binance |
| `usdt-fiat.csv` | Binance P2P | EU Bank |

## 6. Overlapping rows across CSVs

The first row of one CSV may be identical to the last row of an overlapping
xlsx sheet ingested by `xlsx-extras`. The
dedup check `transaction_exists(user, ts, op, amount, asset)` catches it
on re-import. Keep that dedup conservative — fuzzy match (`±0.1%` on
amount) would catch slightly-rounded duplicates between `usdt_transfer*.csv`
files but risks dropping real transactions.

## 7. USDRUB = 0 in newer btc-usdt rows

For 2025+ rows in `btc-usdt.csv`, the USDRUB column is `0.00` because the
sell was directly to USDT (no subsequent P2P leg in the user's spreadsheet).
This is not an error — just means there's no FX context, and we set
`quote_per_usd = 1` (USDT ≈ USD) for that transaction.

## 8. `usdt_transfer3.csv` ≠ `usdt_transfer.csv`

These files look like duplicates because some rows share dates and similar
amounts (e.g. same date: 1855 vs 1854.55). They are NOT duplicates — one
is a regular transfer flow and the other is platform interest payouts.
Both should be imported. Dedup catches actual duplicates.
