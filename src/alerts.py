"""Core price alerts check and evaluation logic for the crypto ledger."""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from decimal import Decimal

import requests

import accounting
import database
import telegram_utils as tg

logger = logging.getLogger()
logger.setLevel(logging.INFO)

COINGECKO_IDS = {
    "BTC": "bitcoin",
    "ETH": "ethereum",
    "SOL": "solana",
    "USDT": "tether",
    "USDC": "usd-coin",
    "BNB": "binancecoin",
    "TON": "the-open-network",
    "TRX": "tron",
    "XRP": "ripple",
}


def run_price_alerts() -> None:
    """Evaluate both custom price alerts and automatic volatility alerts for held assets."""
    allowed_raw = os.environ.get("ALLOWED_TELEGRAM_USER_ID", "")
    try:
        user_id = int(allowed_raw)
    except ValueError:
        logger.error("ALLOWED_TELEGRAM_USER_ID is not configured correctly or missing: %s", allowed_raw)
        return

    logger.info("Running price alerts check for user %s", user_id)

    # 1. Retrieve all custom alerts
    try:
        custom_alerts = database.list_custom_alerts(user_id)
    except Exception:
        logger.exception("Failed to list custom alerts")
        custom_alerts = []

    # 2. Retrieve currently held assets (positive remaining balances in FIFO lots)
    held_assets = []
    try:
        txs = database.query_transactions(user_id)
        state = accounting.replay(txs, base_currency=os.environ.get("BASE_CURRENCY", "USD"))
        for asset in state.open_lots:
            # We treat asset as held if total balance is positive
            if state.total_balance(asset) > Decimal("0"):
                held_assets.append(asset.upper())
    except Exception:
        logger.exception("Failed to retrieve user transactions or replay portfolio state")

    # 3. Identify unique assets we need to check
    custom_assets = {a["asset"].upper() for a in custom_alerts}
    unique_assets = custom_assets.union(held_assets)

    # Map to CoinGecko IDs
    cg_ids = []
    asset_to_cg = {}
    for asset in unique_assets:
        if asset in COINGECKO_IDS:
            cg_id = COINGECKO_IDS[asset]
            cg_ids.append(cg_id)
            asset_to_cg[asset] = cg_id
        else:
            logger.warning("Asset %s is not mapped to a CoinGecko ID and will be skipped in alert check", asset)

    if not cg_ids:
        logger.info("No assets to check prices for.")
        return

    # 4. Fetch spot prices and 24h change metrics from CoinGecko simple price API in one request
    prices: dict[str, dict[str, Decimal]] = {}
    try:
        resp = requests.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={
                "ids": ",".join(cg_ids),
                "vs_currencies": "usd",
                "include_24hr_change": "true",
            },
            timeout=10,
        )
        if resp.ok:
            data = resp.json()
            for asset, cg_id in asset_to_cg.items():
                asset_data = data.get(cg_id, {})
                price_val = asset_data.get("usd")
                change_val = asset_data.get("usd_24h_change")
                if price_val is not None:
                    prices[asset] = {
                        "price": Decimal(str(price_val)),
                        "change_24h": Decimal(str(change_val)) if change_val is not None else Decimal("0"),
                    }
        else:
            logger.error("CoinGecko API returned status code %s: %s", resp.status_code, resp.text)
            return
    except requests.RequestException:
        logger.exception("Failed to query CoinGecko simple price API")
        return

    # 5. Evaluate Custom Price Alerts
    for alert in custom_alerts:
        asset = alert["asset"].upper()
        if asset not in prices:
            continue

        current_price = prices[asset]["price"]
        condition = alert["condition"]
        target = Decimal(str(alert["target"]))
        baseline = Decimal(str(alert["baseline_price"])) if alert.get("baseline_price") is not None else None

        triggered = False
        if condition == ">":
            triggered = current_price >= target
        elif condition == "<":
            triggered = current_price <= target
        elif condition == "%":
            if baseline and baseline > 0:
                change_pct = (current_price - baseline) / baseline * 100
                triggered = abs(change_pct) >= target

        if triggered:
            logger.info("Custom alert triggered: %s %s %s (current: %s)", asset, condition, target, current_price)
            try:
                # Format notification and send
                msg = tg.format_custom_alert_triggered(asset, condition, target, current_price, baseline)
                tg.send_message(user_id, msg)
                # Auto-delete the alert
                database.delete_custom_alert(user_id, asset, alert["alert_id"])
            except Exception:
                logger.exception("Error processing custom alert trigger for %s", asset)

    # 6. Evaluate Automatic Volatility Alerts
    for asset in held_assets:
        if asset not in prices:
            continue

        price_info = prices[asset]
        current_price = price_info["price"]
        change_24h = price_info["change_24h"]

        # Trigger if absolute 24h change is >= 5.0%
        if abs(change_24h) >= Decimal("5.0"):
            config_key = f"volatility_alert_{asset}"
            suppressed = False
            last_change = None
            last_at = None

            try:
                config_item = database.get_user_config(user_id, config_key)
                if config_item and config_item.get("value"):
                    val_dict = json.loads(config_item["value"])
                    last_change = Decimal(str(val_dict.get("last_notified_change", 0)))
                    last_at_str = val_dict.get("last_notified_at")
                    if last_at_str:
                        last_at = datetime.fromisoformat(last_at_str)
            except Exception:
                logger.exception("Failed to parse volatility suppression config for %s", asset)

            if last_at and last_change is not None:
                # Check 12-hour suppression window
                time_diff = datetime.now(timezone.utc) - last_at
                hours_passed = time_diff.total_seconds() / 3600.0
                if hours_passed < 12.0:
                    # Suppressed unless absolute difference between current 24h change
                    # and last alerted 24h change is >= 2.0%
                    pct_diff = abs(change_24h - last_change)
                    if pct_diff < Decimal("2.0"):
                        suppressed = True
                        logger.info("Volatility alert for %s is suppressed (change: %s%%, last: %s%%, %s hours ago)",
                                    asset, change_24h, last_change, round(hours_passed, 1))

            if not suppressed:
                logger.info("Volatility alert triggered for %s: %s%%", asset, change_24h)
                try:
                    msg = tg.format_volatility_alert(asset, change_24h, current_price)
                    tg.send_message(user_id, msg)

                    # Update suppression log
                    new_val = {
                        "last_notified_change": float(change_24h),
                        "last_notified_at": datetime.now(timezone.utc).isoformat(),
                    }
                    database.put_user_config(user_id, config_key, json.dumps(new_val))
                except Exception:
                    logger.exception("Failed to process volatility alert notification for %s", asset)
