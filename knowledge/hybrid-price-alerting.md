# Hybrid Price Alerting & Volatility Monitor Pattern

Added 2026-05-19. Documenting the design, scheduling logic, and zero-cost scaling implementation of our crypto price alert system.

## 1. Problem Statement
The user wants custom target-price notifications (e.g. BTC > 75000), baseline price delta notifications (e.g. SOL 5%), and automatic volatility warnings on portfolio holdings without:
1. Running an expensive always-on polling server (e.g. EC2/Fargate).
2. Spreading spam alerts on minor or high-frequency market oscillations.
3. Exceeding AWS Free Tier limits (keeping incremental running cost at $0.00).

## 2. Architecture

```
                    ┌────────────────────────────┐
                    │   AWS EventBridge Schedule │
                    │   (cron: rate(15 minutes)) │
                    └─────────────┬──────────────┘
                                  │
                                  ▼ {"_alert_check": true}
                    ┌────────────────────────────┐
                    │ crypto-ledger-bot Lambda   │
                    │ • handler.py (fast branch) │
                    └─────────────┬──────────────┘
                                  │
                                  ▼ calls run_price_alerts()
                    ┌────────────────────────────┐
                    │    alerts.py               │
                    │    • fetch CoinGecko API   │
                    │    • query active alerts   │
                    │    • evaluate conditions   │
                    │    • post notifications    │
                    └────────────────────────────┘
```

## 3. High-Performance Execution & Fast Branching
Since EventBridge schedules run every 15 minutes, they target the primary bot Lambda directly. To avoid invoking heavy LLM parsers and database replay structures when the trigger fires, the `lambda_handler` intercepts the Scheduler payload on entry:

```python
# In src/handler.py
if event.get("_alert_check") is True:
    try:
        from alerts import run_price_alerts
        run_price_alerts()
        return {"statusCode": 200, "body": "Alert check complete"}
    except Exception as e:
        logger.error(f"Error running price alerts: {e}")
        return {"statusCode": 500, "body": str(e)}
```
This fast branch runs in milliseconds, using no LLM parse calls, keeping cost at absolute minimums.

## 4. Key Logic & Heuristics

### A. Volatility Calculation & Active Positions
Automatic volatility tracking monitors held positions (assets with a net-positive balance in current FIFO lots) by comparing:
- Current CoinGecko spot price
- 24-hour percentage change (`usd_24h_change` returned in the CoinGecko payload)
A notification is generated if the 24-hour change matches or exceeds $\ge 5\%$ in either direction.

### B. Smart 12-Hour suppression with Absolute Delta Bypass
To prevent notification fatigue, each asset has a state storage entry in `USER_CONFIG` (`volatility_alert_<asset>`) containing:
- `last_notified_change` (percentage change at the time of last alert)
- `last_notified_at` (ISO timestamp)

When a $\ge 5\%$ swing is detected, the engine applies these rules:
1. If no previous alert is registered, or more than **12 hours** have elapsed since `last_notified_at`, the alert fires.
2. If less than **12 hours** have elapsed, the alert is suppressed **unless** the current percentage change deviates from the `last_notified_change` by a further **$\ge 2.0\%$ absolute percentage delta** (e.g. if previous alert was at `+5.2%` and the price leaps to `+7.5%`, the suppression is bypassed and a new alert triggers).

### C. Auto-Deleting Custom Triggers
Custom target conditions (e.g. `/alert BTC > 75000` or `/alert ETH < 3200`) use specific baseline values. Once met, the engine:
1. Formats and sends a custom alert telegram card to the user.
2. Automatically deletes the corresponding alert item from DynamoDB to ensure it fires exactly once.

## 5. Cost Mechanics ($0.00 Free-Tier Model)
1. **EventBridge Scheduler**: ~2,880 runs/month. (AWS Free Tier includes 14 Million schedules per month).
2. **Lambda Invocations**: ~2,880 runs/month at <1 second duration and 512 MB. (AWS Free Tier includes 1 Million requests and 400,000 GB-seconds per month).
3. **CoinGecko Public API**: Free-tier public rate limits are respected by executing a single batch fetch containing all active assets.
Result: The incremental running cost of this background daemon is **$0.00**.
