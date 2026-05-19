# NL Batch Import Pipeline

When the DynamoDB is empty (after a wipe or fresh start), the historical
CSV + xlsx data is re-imported via a two-script pipeline that produces a
human-readable log you can review before uploading.

## Scripts (in `scripts/`, gitignored — exist only on disk)

### 1. `generate_nl_log.py`

Reads all 9 CSVs + Budget.xlsx and writes `history.log` (also gitignored).

```bash
python scripts/generate_nl_log.py          # writes history.log
python scripts/generate_nl_log.py --no-xlsx  # skip xlsx extras
```

Each transaction becomes one natural-language line sorted by date, grouped
by year with `# --- YYYY ---` headers. Comment lines (`#`) are skipped
during upload.

### 2. `batch_upload.py`

Reads `history.log`, sends each line through the LLM parser (same Haiku
model as the live bot), writes to DynamoDB. ~$0.50–$1 in API cost for
~956 lines.

```bash
# Always dry-run first — parses every line, writes nothing
python scripts/batch_upload.py --user-id <YOUR_TELEGRAM_USER_ID> --dry-run

# Real upload (dedup on by default — safe to re-run after interruptions)
python scripts/batch_upload.py --user-id <YOUR_TELEGRAM_USER_ID>

# Resume after interruption (dedup handles already-written rows)
python scripts/batch_upload.py --user-id <YOUR_TELEGRAM_USER_ID>
```

Options: `--delay SECS` (default 0.3), `--no-dedup` (faster on fresh DB).

## NL format per transaction type

| CSV | NL template |
|---|---|
| fiat-usdt.csv | `Bought {usdt} USDT using {rub} RUB from Tbank on Binance P2P {date}` |
| usdt-btc.csv | `Buy {btc} BTC for {usdt} USDT on Binance[, fee X USDT] {date}` |
| btc-usdt.csv | `Sell {btc} BTC for {usdt} USDT on Binance[, fee X USDT] {date}` |
| usdt-fiat.csv | `Sell {usdt} USDT for {eur} EUR via P2P Binance P2P to EU Bank[, fee X USDT] {date}` |
| usdt_transfer*.csv | `Transfer {amt} USDT from {src} to {dst}[, fee X USDT] {date}` |
| btc-transfer*.csv | `Transfer {amt} BTC from {src} to {dst}[, fee X BTC] {date}` |
| xlsx RUB→BTC buys | `Bought {btc} BTC using {rub} RUB from Tbank on Binance P2P {date}` |
| xlsx opening bal. | `Buy {btc} BTC for {usd} USD, held at {Ledger\|Blockchain} {date}` |
| xlsx early sells | `Sell {btc} BTC for {proceeds} {quote} [from {src}] on {dst}[, fee X USDT] {date}` |

## Why `using` and `for` matter

The regex fallback in `parser._validate` extracts `total_quote_value` from
the phrase `(?:using|for|from)\s+([\d,]+(?:\.\d+)?)\s*{quote_asset}`.
Using the exact column value (e.g. `Sell USDT = 1329.56`) in the NL text
ensures math-accurate cost basis — avoids the fiat oracle overshoot bug.
See [[fiat-stablecoin-lot-accounting]].

## Filling the $90k PnL gap

After generating `history.log`, open it and add missing sell transactions
in the same format before uploading. The gap is believed to be sells from
the `Crypto Flow` AY column in `Budget.xlsx` that were never exported to
CSV. Adding them manually closes the gap.

## Dependencies (local only, not in Lambda layer)

`anthropic`, `python-dotenv`, `openpyxl` — install into `.venv`:
```bash
pip install anthropic python-dotenv openpyxl
```
`anthropic` and `python-dotenv` are now listed in `requirements.txt`.
