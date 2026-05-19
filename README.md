# Crypto Ledger Bot

Serverless Telegram bot that doubles as a personal crypto financial ledger and
investment assistant. You message the bot in plain text or with slash commands;
an LLM parses each message, persists structured records to DynamoDB, and
returns balance, PnL, fees, or DCA-nudge replies on demand.

- Single-user, zero-trust auth by Telegram user ID
- AWS-native (Lambda + DynamoDB + API Gateway HTTP), pay-per-request only
- Infrastructure-as-code via Terraform
- LLM provider selectable at deploy time: Gemini, OpenAI, or Claude
- Open-source, MIT-licensed

See [AGENT_SPEC.md](AGENT_SPEC.md) for the complete design specification.

---

## Architecture

```
                       ┌───────────────────────────┐
   Telegram User ──▶   │ HTTPS POST /webhook       │
                       │ API Gateway (HTTP API)    │
                       └──────────────┬────────────┘
                                      │ Lambda proxy
                                      ▼
                       ┌───────────────────────────┐
                       │  crypto-ledger-bot Lambda │
                       │  • handler.py (auth gate) │
                       │  • parser.py  (LLM)       │
                       │  • accounting.py (FIFO)   │
                       │  • database.py            │
                       └──────────────┬────────────┘
                                      │
                                      ▼
                       ┌───────────────────────────┐
                       │  DynamoDB single table    │
                       │  PK=USER#<id>             │
                       │  SK=TX# / LOT# / FXLOT#   │
                       └───────────────────────────┘

                                  ▲
                                  │ weekly cron(0 9 ? * MON *)
                       ┌──────────┴────────────────┐
                       │  EventBridge Scheduler    │
                       └──────────┬────────────────┘
                                  ▼
                       ┌───────────────────────────┐
                       │ crypto-ledger-nudge Lambda│
                       │ • scheduler.py            │
                       │ → sendMessage to Telegram │
                       └───────────────────────────┘
```

---

## Prerequisites

- AWS account with CLI credentials (`aws configure`)
- Terraform ≥ 1.6
- Python 3.12
- A Telegram bot token from [@BotFather](https://t.me/BotFather)
- Your Telegram numeric user ID (ask [@userinfobot](https://t.me/userinfobot))
- An LLM API key — pick one:
  - Anthropic (`claude`) — model `claude-haiku-4-5-20251001`
  - OpenAI (`openai`) — model `gpt-4o-mini`
  - Google (`gemini`) — model `gemini-2.0-flash`

---

## Step-by-step deployment

### 1. Clone and configure

```bash
git clone <this-repo> crypto-ledger-bot
cd crypto-ledger-bot
cp .env.example .env                                  # for local CLI use
cp terraform/terraform.tfvars.example terraform/terraform.tfvars
$EDITOR terraform/terraform.tfvars                    # fill in real values
```

### 2. Build the Lambda dependency layer (arm64 wheels)

```bash
mkdir -p layer/python
pip install --platform manylinux2014_aarch64 --target layer/python \
    --implementation cp --python-version 3.12 --only-binary=:all: \
    -r requirements.txt
```

If you only need one LLM provider, comment out the others in `requirements.txt`
before this step to keep the layer small.

### 3. Apply Terraform

```bash
cd terraform
terraform init
terraform apply
```

Capture the `webhook_url` from the outputs.

### 4. Register the webhook with Telegram

```bash
curl -X POST "https://api.telegram.org/bot<TELEGRAM_BOT_TOKEN>/setWebhook" \
     -d "url=<webhook_url>"
```

The `set_webhook_command` Terraform output prints this verbatim.

### 5. Import historical data

```bash
# Always preview with --dry-run first.
python scripts/import_historical_csv.py \
    --file fiat-usdt.csv --type fiat-to-stable \
    --user-id <TG_USER_ID> \
    --source "Sberbank" --destination "Bybit P2P" \
    --dry-run
```

`--type` must be one of:
- `fiat-to-stable`    — `fiat-usdt.csv` (P2P buy USDT with RUB)
- `stable-to-crypto`  — `usdt-btc.csv` (spot buy BTC with USDT)
- `crypto-transfer`   — `btc-transfer*.csv`
- `crypto-to-stable`  — `btc-usdt.csv` (spot sell BTC for USDT)
- `stable-to-fiat`    — `usdt-fiat.csv` (P2P sell USDT for EUR)
- `stable-transfer`   — `usdt_transfer*.csv`

Drop `--dry-run` to perform the actual import.

---

## Commands

| Command | Action |
|---|---|
| `/balance` | Assets grouped by location with USD valuation |
| `/pnl` | Realized PnL: L1 (asset) + L2 (forex) − fees |
| `/unrealized` | Unrealized PnL using live CoinGecko prices |
| `/history [N]` | Last N transactions (default 10) |
| `/fees` | Total fees paid, by asset |
| `/stats` | DCA frequency, average buy price |
| `/help` | This list |

Natural language examples — the LLM will infer the operation type:

```
bought 1205 usdt for 82.99 rub each on bybit p2p
transferred 0.012 btc from binance to ledger, fee was 0.0001
sold 1000 usdt for 871 eur
bought 0.0115 btc for 1101 usdt on binance
```

---

## Switching LLM providers

Edit `llm_provider` in `terraform.tfvars` and `terraform apply`. The factory in
[src/parser.py](src/parser.py) handles all three providers behind a single
`LLMParser` interface — adding a fourth is a matter of subclassing it and
extending the factory.

---

## Adding a new exchange or asset

No code change is required. The bot stores `source`, `destination`, `asset`,
and `quote_asset` as free-form strings. Just type the new name in your next
message and it will appear in subsequent `/balance` outputs.

---

## Cost estimate

For a single user logging ~30 messages a month:

| Resource | Volume | AWS cost (us-east-1, beyond free tier) |
|---|---|---|
| Lambda invocations | ~150/mo @ 512 MB, <1 s avg | < $0.01 |
| DynamoDB | <1 GB, PAY_PER_REQUEST | < $0.10 |
| API Gateway HTTP | ~150 requests | < $0.01 |
| CloudWatch Logs | retention 14 days | < $0.05 |
| EventBridge | 4 schedules/mo | $0.00 |
| **Total** | | **well under $0.20/mo**, $0 within free tier |

LLM costs depend on provider — Claude Haiku 4.5 is typically <$0.001 per parse.

---

## Local testing

You can invoke the handler against a mock Telegram payload without deploying:

```bash
export $(cat .env | xargs)
PYTHONPATH=src python3.12 - <<'EOF'
import json, handler
event = {"body": json.dumps({
    "message": {
        "from": {"id": int(__import__("os").environ["ALLOWED_TELEGRAM_USER_ID"])},
        "chat": {"id": int(__import__("os").environ["ALLOWED_TELEGRAM_USER_ID"])},
        "text": "/help",
    }
})}
print(handler.lambda_handler(event, None))
EOF
```

Run the unit tests with:

```bash
PYTHONPATH=src python3.12 -m unittest discover tests -v
```

---

## License

MIT — see [LICENSE](LICENSE).
