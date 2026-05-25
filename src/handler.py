"""Lambda webhook entry point. First gate: zero-trust auth by Telegram user ID."""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from decimal import Decimal

import requests

import accounting
import database
import parser as llm_parser
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


def lambda_handler(event, context):
    """API Gateway → Telegram webhook entry (fast path) or async processor (slow path)."""
    # EventBridge schedule price alert check
    if "_alert_check" in event:
        from alerts import run_price_alerts
        run_price_alerts()
        return {"statusCode": 200, "body": "OK"}

    # Async processing path: invoked by the webhook Lambda, no API GW time constraint.
    if "_proc" in event:
        p = event["_proc"]
        try:
            _route(p["text"], p["chat_id"], p["user_id"])
        except Exception as e:  # noqa: BLE001
            logger.exception("Unhandled error in async processing")
            tg.send_message(p["chat_id"], f"_Error: {e}_")
        return {"statusCode": 200, "body": "OK"}

    # Webhook path: parse, authenticate, dedup — then dispatch async and return 200 fast.
    try:
        body = event.get("body") or "{}"
        update = json.loads(body)
    except json.JSONDecodeError:
        logger.warning("Webhook received invalid JSON")
        return {"statusCode": 200, "body": "OK"}

    message = update.get("message") or update.get("edited_message") or {}
    from_user = (message.get("from") or {}).get("id")
    chat_id = (message.get("chat") or {}).get("id")
    text = (message.get("text") or "").strip()

    allowed_raw = os.environ.get("ALLOWED_TELEGRAM_USER_ID", "")
    try:
        allowed_id = int(allowed_raw)
    except ValueError:
        logger.error("ALLOWED_TELEGRAM_USER_ID is not configured correctly")
        return {"statusCode": 200, "body": "OK"}

    if from_user != allowed_id:
        logger.warning("Unauthorized access attempt")
        return {"statusCode": 200, "body": "OK"}

    if not text or not chat_id:
        return {"statusCode": 200, "body": "OK"}

    update_id = update.get("update_id")
    if update_id:
        if not database.acquire_update_lock(from_user, update_id):
            logger.info("Duplicate Telegram update %s, skipping", update_id)
            return {"statusCode": 200, "body": "OK"}

    import boto3
    try:
        boto3.client("lambda").invoke(
            FunctionName=context.function_name,
            InvocationType="Event",
            Payload=json.dumps({"_proc": {"text": text, "chat_id": chat_id, "user_id": allowed_id}}),
        )
    except Exception:
        logger.exception("Async dispatch failed; processing synchronously")
        try:
            _route(text, chat_id, allowed_id)
        except Exception as e:  # noqa: BLE001
            tg.send_message(chat_id, f"_Error: {e}_")
    return {"statusCode": 200, "body": "OK"}


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------


def _route(text: str, chat_id: int, user_id: int) -> None:
    lower = text.lower()
    base_currency = os.environ.get("BASE_CURRENCY", "USD")

    if lower.startswith("/help") or lower == "/start":
        tg.send_message(chat_id, tg.HELP_TEXT)
        return
    if lower.startswith("/alerts"):
        _cmd_alerts(chat_id, user_id)
        return
    if lower.startswith("/alert"):
        _cmd_alert(text, chat_id, user_id)
        return
    if lower.startswith("/balance"):
        _cmd_balance(chat_id, user_id, base_currency)
        return
    if lower.startswith("/pnl"):
        _cmd_pnl(chat_id, user_id, base_currency)
        return
    if lower.startswith("/unrealized"):
        _cmd_unrealized(chat_id, user_id, base_currency)
        return
    if lower.startswith("/history"):
        n = _parse_int_arg(text, default=10)
        _cmd_history(chat_id, user_id, n)
        return
    if lower.startswith("/fees"):
        _cmd_fees(chat_id, user_id)
        return
    if lower.startswith("/stats"):
        asset = _parse_stats_asset(text)
        _cmd_stats(chat_id, user_id, asset)
        return
    if lower.startswith("/set"):
        _cmd_set(text, chat_id, user_id)
        return
    if lower.startswith("/sim"):
        _cmd_sim(text, chat_id, user_id)
        return
    if lower.startswith("/ask"):
        _cmd_ask(text, chat_id, user_id)
        return
    if lower.startswith("/strategy"):
        _cmd_strategy(text, chat_id, user_id)
        return
    if lower.startswith("/context"):
        _cmd_context(text, chat_id, user_id)
        return
    if lower.startswith("/funds"):
        _cmd_funds(chat_id, user_id)
        return
    if lower.startswith("/fund"):
        _cmd_fund(text, chat_id, user_id)
        return
    if lower.startswith("/wipe"):
        _cmd_wipe(text, chat_id, user_id)
        return

    # Any other slash command resets the advisor session then runs normally.
    if lower.startswith("/"):
        database.clear_advisor_session(user_id)
        _cmd_freeform(text, chat_id, user_id, base_currency)
        return

    # Plain text: resume an active advisor conversation before trying the parser.
    session_msgs = database.get_advisor_session(user_id)
    if session_msgs is not None:
        _cmd_continue_advisor(text, chat_id, user_id, session_msgs)
        return

    _cmd_freeform(text, chat_id, user_id, base_currency)


def _cmd_alerts(chat_id: int, user_id: int) -> None:
    """`/alerts` — list all active custom price alerts."""
    try:
        alerts = database.list_custom_alerts(user_id)
        text = tg.format_alerts_list(alerts)
        tg.send_message(chat_id, text)
    except Exception as e:
        logger.exception("Failed to query alerts")
        tg.send_message(chat_id, f"❌ _Error retrieving alerts: {e}_")


def _cmd_alert(text: str, chat_id: int, user_id: int) -> None:
    """`/alert` — set or clear price alerts."""
    parts = text.strip().split()
    if len(parts) < 2:
        tg.send_message(chat_id, (
            "🔔 *Price Alerts Help*\n\n"
            "Use the following formats to manage alerts:\n"
            "• `/alert <asset> > <price>` — Crosses above (e.g. `/alert BTC > 75000`)\n"
            "• `/alert <asset> < <price>` — Crosses below (e.g. `/alert BTC < 65000`)\n"
            "• `/alert <asset> <pct>%` — Relative percent movement (e.g. `/alert BTC 5%`)\n"
            "• `/alert clear <asset>` — Clear all alerts for an asset (e.g. `/alert clear BTC`)\n"
            "• `/alerts` — List all your active alerts"
        ))
        return

    # Check for "clear"
    if parts[1].lower() == "clear":
        if len(parts) < 3:
            tg.send_message(chat_id, "❌ Please specify an asset ticker to clear alerts for (e.g. `/alert clear BTC`).")
            return
        asset = parts[2].upper()
        try:
            count = database.clear_custom_alerts_for_asset(user_id, asset)
            tg.send_message(chat_id, f"🧹 Cleared {count} active alert(s) for *{asset}*.")
        except Exception as e:
            logger.exception("Failed to clear alerts")
            tg.send_message(chat_id, f"❌ _Error clearing alerts: {e}_")
        return

    # Set custom price alert
    asset = parts[1].upper()
    if asset not in COINGECKO_IDS:
        tg.send_message(chat_id, f"❌ Unsupported or invalid asset ticker: *{asset}*.\nSupported: {', '.join(sorted(COINGECKO_IDS.keys()))}")
        return

    # Fetch current spot price for validation and baseline
    try:
        prices = _live_prices(user_id, [asset], "USD")
        if asset not in prices:
            tg.send_message(chat_id, f"❌ Could not fetch price for *{asset}*. Please try again later.")
            return
        current_price = prices[asset]
    except Exception as e:
        logger.exception("Failed to fetch price validation")
        tg.send_message(chat_id, f"❌ _Error fetching validation price: {e}_")
        return

    if len(parts) == 3:
        # Relative percent alert: "/alert BTC 5%" or "/alert BTC 5"
        arg = parts[2]
        pct_str = arg.rstrip("%")
        try:
            pct = Decimal(pct_str)
        except ValueError:
            tg.send_message(chat_id, f"❌ Invalid price or percentage: `{arg}`")
            return

        target_pct = abs(pct)
        if target_pct <= Decimal("0"):
            tg.send_message(chat_id, "❌ Percentage trigger must be greater than 0%.")
            return

        try:
            database.put_custom_alert(
                user_id=user_id,
                asset=asset,
                condition="%",
                target=target_pct,
                baseline_price=current_price
            )
            tg.send_message(chat_id, (
                f"🔔 *Custom Alert Set!*\n\n"
                f"Asset: *{asset}*\n"
                f"Trigger: Moves by *{target_pct}%*\n"
                f"Baseline Price: `${tg._fmt(current_price)} USD`"
            ))
        except Exception as e:
            logger.exception("Failed to save custom alert")
            tg.send_message(chat_id, f"❌ _Error setting custom alert: {e}_")
        return

    elif len(parts) == 4:
        # Price target alert: "/alert BTC > 75000" or "/alert BTC < 60000"
        operator = parts[2]
        price_str = parts[3]
        if operator not in (">", "<"):
            tg.send_message(chat_id, f"❌ Invalid operator `{operator}`. Use `>` or `<`.")
            return

        try:
            target_price = Decimal(price_str)
        except ValueError:
            tg.send_message(chat_id, f"❌ Invalid target price: `{price_str}`")
            return

        if target_price <= Decimal("0"):
            tg.send_message(chat_id, "❌ Target price must be greater than 0.")
            return

        try:
            database.put_custom_alert(
                user_id=user_id,
                asset=asset,
                condition=operator,
                target=target_price,
                baseline_price=current_price
            )
            op_name = "crosses above" if operator == ">" else "crosses below"
            tg.send_message(chat_id, (
                f"🔔 *Custom Alert Set!*\n\n"
                f"Asset: *{asset}*\n"
                f"Trigger: Price {op_name} `${tg._fmt(target_price)} USD`\n"
                f"Current Price: `${tg._fmt(current_price)} USD`"
            ))
        except Exception as e:
            logger.exception("Failed to save custom alert")
            tg.send_message(chat_id, f"❌ _Error setting custom alert: {e}_")
        return

    else:
        tg.send_message(chat_id, "❌ Invalid syntax. Use `/alert <asset> > <price>`, `/alert <asset> 5%`, or `/alert clear <asset>`.")


def _parse_int_arg(text: str, default: int) -> int:
    m = re.search(r"\d+", text)
    return int(m.group()) if m else default


def _parse_stats_asset(text: str) -> str | None:
    """`/stats btc` → 'BTC'; `/stats` → None."""
    parts = text.split()
    if len(parts) >= 2:
        return parts[1].upper()
    return None


_SET_RE = re.compile(
    r"^/set\s+(?P<asset>\S+)\s+(?P<location>.+?)\s+(?P<amount>[-−–—]?\d+(?:\.\d+)?)\s*$",
    re.IGNORECASE,
)


_SET_NOISE_SUFFIX = re.compile(r"\s+(to|from|at|=|→)\s*$", re.IGNORECASE)


def _cmd_set(text: str, chat_id: int, user_id: int) -> None:
    """`/set <asset> <location> <amount>` — reconcile to target without LLM."""
    m = _SET_RE.match(text.strip())
    if not m:
        tg.send_message(
            chat_id,
            "_Usage: `/set <asset> <location> <amount>` — e.g. `/set BTC Ledger 1.23456789`_",
        )
        return
    asset = m.group("asset").upper()
    loc = _SET_NOISE_SUFFIX.sub("", m.group("location").strip().strip('"').strip("'"))
    # Normalize unicode/typographic dashes to standard hyphen-minus for Decimal conversion
    amount_str = m.group("amount").replace("−", "-").replace("–", "-").replace("—", "-")
    # Detect swapped asset/location (e.g. "/set Binance USDT 0" instead of "/set USDT Binance 0")
    state = _replay_state(user_id)
    if asset not in state.balances and loc.upper() in state.balances:
        tg.send_message(
            chat_id,
            f"_Did you mean `/set {loc.upper()} {asset.title()} {amount_str}`? "
            f"(asset and location appear swapped)_",
        )
        return
    _reconcile(chat_id, user_id, asset, loc, Decimal(amount_str), note="manual /set")


# ---------------------------------------------------------------------------
# Command implementations
# ---------------------------------------------------------------------------


def _replay_state(user_id: int):
    txs = database.query_transactions(user_id)
    return accounting.replay(txs, base_currency=os.environ.get("BASE_CURRENCY", "USD"))


def _cmd_balance(chat_id: int, user_id: int, base_currency: str) -> None:
    state = _replay_state(user_id)
    valuations = _live_prices(user_id, list(state.balances.keys()), base_currency)
    tg.send_message(chat_id, tg.format_balance(state.balances, base_currency, valuations))


def _cmd_pnl(chat_id: int, user_id: int, base_currency: str) -> None:
    state = _replay_state(user_id)
    fee_assets = list(state.fees.keys())
    valuations = _live_prices(user_id, fee_assets, base_currency)
    fees_in_base = accounting.total_fees_in(state, base_currency, valuations)
    tg.send_message(
        chat_id,
        tg.format_pnl(
            state.realized_pnl_usd,
            state.realized_asset_pnl_usd,
            state.realized_fx_pnl_usd,
            fees_in_base,
            base_currency,
            state.fees,
        ),
    )


def _cmd_unrealized(chat_id: int, user_id: int, base_currency: str) -> None:
    state = _replay_state(user_id)
    assets = list(state.open_lots.keys())
    prices = _live_prices(user_id, assets, base_currency)
    breakdown = {}
    for asset, lots in state.open_lots.items():
        if asset not in prices:
            continue
        p = prices[asset]
        breakdown[asset] = sum(
            (p - lot.cost_per_unit_usd) * lot.amount_remaining for lot in lots
        )
    total = accounting.unrealized_pnl_usd(state, prices)
    tg.send_message(chat_id, tg.format_unrealized(total, base_currency, breakdown))


def _cmd_history(chat_id: int, user_id: int, n: int) -> None:
    txs = database.query_transactions(user_id, limit=n, descending=True)
    txs.reverse()
    tg.send_message(chat_id, tg.format_history(txs, n))


def _cmd_fees(chat_id: int, user_id: int) -> None:
    state = _replay_state(user_id)
    tg.send_message(chat_id, tg.format_fees(state.fees))


def _cmd_stats(chat_id: int, user_id: int, asset: str | None = None) -> None:
    def _D(val) -> Decimal:
        if val is None or val == "":
            return Decimal("0")
        if isinstance(val, Decimal):
            return val
        try:
            return Decimal(str(val))
        except Exception:
            return Decimal("0")

    txs = database.query_transactions(user_id, asset=asset) if asset else database.query_transactions(user_id)
    buys = [t for t in txs if t.get("operation") in ("P2P_BUY", "SPOT_BUY")]
    if asset:
        buys = [t for t in buys if t.get("asset") == asset]
    if not buys:
        label = asset or "any asset"
        tg.send_message(chat_id, f"_No buy transactions for {label} yet._")
        return
    timestamps = sorted(t.get("timestamp", "") for t in buys)
    deltas = []
    for a, b in zip(timestamps, timestamps[1:]):
        try:
            dt_a = datetime.fromisoformat(a.replace("Z", "+00:00"))
            dt_b = datetime.fromisoformat(b.replace("Z", "+00:00"))
            deltas.append((dt_b - dt_a).total_seconds() / 86400)
        except ValueError:
            continue
    avg_days = (sum(deltas) / len(deltas)) if deltas else 0

    stats = {
        "total_buys": len(buys),
        "avg_days_between_buys": avg_days,
        "first_tx": timestamps[0][:10],
        "last_tx": timestamps[-1][:10],
    }
    # Weighted avg price only makes sense per-asset since different buys
    # quote in different units (RUB/USDT, USDT/BTC, RUB/BTC, ...).
    if asset:
        # Build price index chronologically over all user transactions
        # to value quote currencies accurately.
        all_txs = database.query_transactions(user_id)
        prices_by_asset: dict[str, list[tuple[str, Decimal]]] = {}
        for tx in all_txs:
            op = tx.get("operation")
            tx_asset = tx.get("asset")
            p = _D(tx.get("price"))
            usd_per_quote = accounting._usd_per_quote(tx)
            ts = tx.get("timestamp", "")
            if op in ("P2P_BUY", "P2P_SELL", "SPOT_BUY", "SPOT_SELL") and p > Decimal("0") and usd_per_quote > Decimal("0"):
                prices_by_asset.setdefault(tx_asset, []).append((ts, p * usd_per_quote))
                
        for a in prices_by_asset:
            prices_by_asset[a].sort(key=lambda x: x[0])

        def _get_usd_per_quote_inferred(tx: dict) -> Decimal:
            usd_per_quote = accounting._usd_per_quote(tx)
            if usd_per_quote > Decimal("0"):
                return usd_per_quote
            tx_asset = tx.get("asset")
            p = _D(tx.get("price"))
            ts = tx.get("timestamp", "")
            if tx_asset in accounting._USD_PEGGED and p > Decimal("0"):
                return Decimal("1") / p
            if p > Decimal("0") and tx_asset not in accounting._USD_PEGGED:
                usd_asset_price = accounting._get_historical_price(tx_asset, ts, prices_by_asset)
                if usd_asset_price and usd_asset_price > Decimal("0"):
                    return usd_asset_price / p
            return Decimal("0")

        quotes = [b.get("quote_asset") for b in buys if b.get("quote_asset")]
        primary_quote = max(set(quotes), key=quotes.count) if quotes else "USD"

        total_amount = sum(_D(b.get("amount")) for b in buys)
        total_cost_usd = sum(
            _D(b.get("amount")) * _D(b.get("price")) * _get_usd_per_quote_inferred(b)
            for b in buys
        )
        avg_buy_price_usd = total_cost_usd / total_amount if total_amount > 0 else Decimal("0")

        # Convert back to primary quote asset using its latest rate to USD
        if primary_quote in accounting._USD_PEGGED:
            latest_rate = Decimal("1")
        else:
            primary_rates = [
                _get_usd_per_quote_inferred(tx)
                for tx in all_txs
                if (tx.get("quote_asset") or "").upper() == primary_quote.upper()
            ]
            latest_rate = primary_rates[-1] if primary_rates else Decimal("0")

        avg_buy_price = avg_buy_price_usd / latest_rate if latest_rate > Decimal("0") else avg_buy_price_usd

        if total_amount > 0:
            stats["avg_buy_price"] = avg_buy_price
        stats["asset"] = asset
        if quotes:
            stats["quote_asset"] = primary_quote
    tg.send_message(chat_id, tg.format_stats(stats))




def _reconcile(chat_id: int, user_id: int, asset, location, target_amount, note: str) -> None:
    """Bring `asset` at `location` to exactly `target_amount` by writing an INCOME or EXPENSE delta."""
    if not asset or location is None or target_amount is None:
        tg.send_message(chat_id, "_Reconciliation needs asset, location, and target amount._")
        return
    target = Decimal(str(target_amount))
    state = _replay_state(user_id)
    bucket = state.balances.get(asset, {})
    current = Decimal("0")
    for k, v in bucket.items():
        if k.lower() == str(location).strip().lower():
            current = v
            break
    delta = target - current
    if delta == 0:
        tg.send_message(chat_id, f"_{asset}@{location} is already `{current}`. Nothing to do._")
        return
    op = "INCOME" if delta > 0 else "EXPENSE"
    tx = {
        "operation": op,
        "asset": asset,
        "amount": abs(delta),
        "price": Decimal("0"),
        "quote_asset": None,
        "source": location if op == "EXPENSE" else None,
        "destination": location if op == "INCOME" else None,
        "fee_amount": Decimal("0"),
        "fee_asset": None,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "raw_text": f"reconcile {asset}@{location} {current} → {target}: {note}",
        "imported": False,
    }
    database.put_transaction(user_id, tx)
    tg.send_message(
        chat_id,
        f"Reconciled *{asset}* @ *{location}*: `{current}` → `{target}`  "
        f"({op} `{abs(delta)}`)",
    )


def _build_portfolio_context(state, recent_txs: list, advisor_note: str = "", live_prices: dict | None = None) -> str:
    """Compact portfolio summary for the LLM advisor."""
    lines = ["=== BALANCES ==="]
    for asset in sorted(state.balances):
        locs = state.balances[asset]
        total = sum(locs.values(), Decimal("0"))
        if total <= 0:
            continue
        loc_parts = ", ".join(
            f"{tg._fmt(v)}@{k}" for k, v in sorted(locs.items()) if v > 0
        )
        lines.append(f"{asset}: {tg._fmt(total)} ({loc_parts})")

    lines.append(f"\n=== REALIZED PnL ===\nTotal: ${tg._fmt(state.realized_pnl_usd)} USD")

    # Non-stable crypto gets full FIFO lot detail so the advisor can simulate sells.
    # Stablecoins and fiat get an aggregate line only — their lots are numerous and
    # lot-level detail doesn't help answer typical PnL questions.
    _STABLE_OR_FIAT = {"USDT", "USDC", "DAI", "BUSD", "USD", "EUR", "RUB", "GBP", "CHF"}
    _MAX_LOTS = 20
    aggregate_lines = []
    for asset, lots in sorted(state.open_lots.items()):
        active = [l for l in lots if l.amount_remaining > 0]
        if not active:
            continue
        remaining = sum(l.amount_remaining for l in active)
        avg_cost = sum(l.cost_per_unit_usd * l.amount_remaining for l in active) / remaining
        if asset in _STABLE_OR_FIAT:
            aggregate_lines.append(f"  {asset}: {tg._fmt(remaining)} @ avg ${tg._fmt(avg_cost)}/unit")
            continue
        lines.append(f"\n=== OPEN LOTS — {asset} (FIFO order, oldest first) ===")
        for lot in active[:_MAX_LOTS]:
            lines.append(
                f"  {lot.acquired_at[:10]}  {tg._fmt(lot.amount_remaining)} {asset}"
                f" @ ${tg._fmt(lot.cost_per_unit_usd)}/unit  [{lot.location}]"
            )
        if len(active) > _MAX_LOTS:
            tail = active[_MAX_LOTS:]
            tail_amt = sum(l.amount_remaining for l in tail)
            tail_avg = sum(l.cost_per_unit_usd * l.amount_remaining for l in tail) / tail_amt
            lines.append(
                f"  ... {len(tail)} more lots: {tg._fmt(tail_amt)} {asset}"
                f" @ avg ${tg._fmt(tail_avg)}/unit"
            )
        lines.append(f"  Total: {tg._fmt(remaining)} {asset}, avg ${tg._fmt(avg_cost)}/unit")
    if aggregate_lines:
        lines.append("\n=== STABLECOIN / FIAT POSITIONS (aggregate cost basis) ===")
        lines.extend(aggregate_lines)

    lines.append(f"\n=== LAST {len(recent_txs)} TRANSACTIONS (newest first) ===")
    for tx in recent_txs:
        ts = (tx.get("timestamp") or "")[:10]
        op = tx.get("operation", "?")
        asset = tx.get("asset", "?")
        amount = tx.get("amount", 0)
        price = Decimal(str(tx.get("price") or 0))
        quote = tx.get("quote_asset") or ""
        price_s = f" @ {tg._fmt(price)} {quote}" if price > 0 and quote else ""
        src = tx.get("source") or ""
        dest = tx.get("destination") or ""
        flow_s = f" [{src} → {dest}]" if src or dest else ""
        lines.append(f"  {ts} {op} {tg._fmt(amount)} {asset}{price_s}{flow_s}")

    if live_prices:
        price_lines = [f"  {a}: ${tg._fmt(p)} USD" for a, p in sorted(live_prices.items())]
        lines.append("\n=== LIVE PRICES (current market) ===")
        lines.extend(price_lines)

    if advisor_note:
        lines.append(f"\n=== USER INSTRUCTIONS FOR ADVISOR ===\n{advisor_note}")

    return "\n".join(lines)


def _advisor_is_asking(reply: str) -> bool:
    """Heuristic: does the advisor reply end with a question needing user input?"""
    return bool(re.search(r'\?', reply[-200:]))


_SIM_RE = re.compile(r"^/sim\s+sell\s+([\d.]+)\s+(\w+)", re.IGNORECASE)


def _cmd_sim(text: str, chat_id: int, user_id: int) -> None:
    m = _SIM_RE.match(text.strip())
    if not m:
        tg.send_message(chat_id, "_Usage:_ `/sim sell <amount> <asset>`\nExample: `/sim sell 1 BTC`")
        return

    sell_amount = Decimal(m.group(1))
    asset = m.group(2).upper()

    state = _replay_state(user_id)
    lots = [l for l in state.open_lots.get(asset, []) if l.amount_remaining > 0]
    if not lots:
        tg.send_message(chat_id, f"_No open {asset} lots found._")
        return

    prices = _live_prices(user_id, [asset], "USD") if asset in COINGECKO_IDS else {}
    current_price = prices.get(asset)

    # FIFO simulation — walk lots without mutating state
    rows = []
    total_cost = Decimal("0")
    total_proceeds = Decimal("0")
    remaining = sell_amount
    for lot in lots:
        if remaining <= Decimal("0"):
            break
        take = min(lot.amount_remaining, remaining)
        cost = take * lot.cost_per_unit_usd
        proceeds = take * current_price if current_price else None
        rows.append((lot.acquired_at[:10], take, lot.cost_per_unit_usd, cost, proceeds))
        total_cost += cost
        if proceeds is not None:
            total_proceeds += proceeds
        remaining -= take

    filled = sell_amount - remaining  # may be < sell_amount if insufficient holdings
    total_pnl = total_proceeds - total_cost if current_price else None
    pct = (total_pnl / total_cost * 100) if (current_price and total_cost > 0) else None

    price_str = f"@ ${tg._fmt(current_price)}" if current_price else "(no live price)"
    header = f"*FIFO sell simulation: {tg._fmt(filled)} {asset} {price_str}*"
    if remaining > 0:
        header += f"\n_⚠️ Only {tg._fmt(filled)} available (requested {tg._fmt(sell_amount)})_"

    lines = [header, ""]
    _MAX_ROWS = 15
    for date, qty, cost_unit, cost_tot, proc in rows[:_MAX_ROWS]:
        if current_price:
            pnl_lot = proc - cost_tot
            sign = "+" if pnl_lot >= 0 else ""
            lines.append(f"`{date}`  {tg._fmt(qty)} {asset}  @${tg._fmt(cost_unit)}  {sign}${tg._fmt(pnl_lot)}")
        else:
            lines.append(f"`{date}`  {tg._fmt(qty)} {asset}  @${tg._fmt(cost_unit)}  cost ${tg._fmt(cost_tot)}")
    if len(rows) > _MAX_ROWS:
        hidden = rows[_MAX_ROWS:]
        hidden_qty = sum(r[1] for r in hidden)
        hidden_cost = sum(r[3] for r in hidden)
        lines.append(f"  _... {len(hidden)} more lots: {tg._fmt(hidden_qty)} {asset}  cost ${tg._fmt(hidden_cost)}_")

    lines.append("")
    lines.append(f"Cost basis:  `${tg._fmt(total_cost)}`")
    if current_price:
        sign = "+" if total_pnl >= 0 else ""
        lines.append(f"Proceeds:    `${tg._fmt(total_proceeds)}`")
        pct_str = f"  ({sign}{tg._fmt(pct)}%)" if pct is not None else ""
        lines.append(f"*Net PnL:     {sign}${tg._fmt(total_pnl)}{pct_str}*")
    else:
        lines.append("_Provide current price to calculate proceeds._")

    tg.send_message(chat_id, "\n".join(lines))


def _cmd_ask(text: str, chat_id: int, user_id: int) -> None:
    question = text[4:].strip()
    if not question:
        tg.send_message(
            chat_id,
            "_Usage:_ `/ask <question>`\n\n"
            "Examples:\n"
            "• `/ask what is my average BTC cost basis?`\n"
            "• `/ask how much have I invested total in RUB?`\n"
            "• `/ask what is my best trade so far?`",
        )
        return
    state = _replay_state(user_id)
    recent_txs = database.query_transactions(user_id, limit=25, descending=True)
    cfg = database.get_user_config(user_id, "advisor_note")
    advisor_note = (cfg.get("value") or "").strip() if cfg else ""
    crypto_assets = [a for a in state.open_lots if a in COINGECKO_IDS]
    live_prices = _live_prices(user_id, crypto_assets, "USD") if crypto_assets else {}
    context = _build_portfolio_context(state, recent_txs, advisor_note=advisor_note, live_prices=live_prices)
    # Embed portfolio context in the first user turn so subsequent turns inherit it.
    messages = [{"role": "user", "content": f"Portfolio data:\n{context}\n\nQuestion: {question}"}]
    tg.send_message(chat_id, "_Thinking…_")
    try:
        reply, updated = llm_parser.get_parser().continue_conversation(messages)
    except Exception as e:  # noqa: BLE001
        tg.send_message(chat_id, f"_Could not answer: {e}_")
        return
    tg.send_message(chat_id, reply)
    if _advisor_is_asking(reply):
        database.put_advisor_session(user_id, updated)
    else:
        database.clear_advisor_session(user_id)


def _cmd_continue_advisor(text: str, chat_id: int, user_id: int, messages: list) -> None:
    """Resume an in-progress /ask conversation with the user's follow-up reply."""
    updated = messages + [{"role": "user", "content": text}]
    tg.send_message(chat_id, "_Thinking…_")
    try:
        reply, final = llm_parser.get_parser().continue_conversation(updated)
    except Exception as e:  # noqa: BLE001
        database.clear_advisor_session(user_id)
        tg.send_message(chat_id, f"_Could not continue: {e}_")
        return
    tg.send_message(chat_id, reply)
    if _advisor_is_asking(reply):
        database.put_advisor_session(user_id, final)
    else:
        database.clear_advisor_session(user_id)


def _cmd_strategy(text: str, chat_id: int, user_id: int) -> None:
    parts = text.strip().split(None, 2)
    sub = parts[1].lower() if len(parts) >= 2 else ""

    if sub == "set":
        strategy_text = parts[2].strip() if len(parts) > 2 else ""
        if not strategy_text:
            tg.send_message(chat_id, "_Usage: `/strategy set <description>`_")
            return
        database.put_user_config(user_id, "strategy", strategy_text)
        tg.send_message(
            chat_id,
            f"Strategy saved.\nRun `/strategy` after any trade to check your next step.\n\n_{strategy_text}_",
        )
        return

    if sub == "clear":
        database.put_user_config(user_id, "strategy", "")
        tg.send_message(chat_id, "_Strategy cleared._")
        return

    # /strategy → check
    cfg = database.get_user_config(user_id, "strategy")
    strategy_text = (cfg.get("value") or "").strip() if cfg else ""
    if not strategy_text:
        tg.send_message(
            chat_id,
            "_No strategy saved yet._\n\n"
            "Define one with `/strategy set <description>`. Example:\n"
            "`/strategy set When I buy USDT with RUB I immediately buy BTC with 50% of that USDT. "
            "4 days later I sell 50% of that BTC back to USDT and repeat.`",
        )
        return

    state = _replay_state(user_id)
    recent_txs = database.query_transactions(user_id, limit=15, descending=True)
    cfg = database.get_user_config(user_id, "advisor_note")
    advisor_note = (cfg.get("value") or "").strip() if cfg else ""
    context = _build_portfolio_context(state, recent_txs, advisor_note=advisor_note)
    prompt = (
        f"My personal trading strategy:\n{strategy_text}\n\n"
        "Based on my portfolio and recent transactions answer:\n"
        "1. What is my next step in this strategy?\n"
        "2. What exact amount should I trade and in which direction?\n"
        "3. If there is a timing element, what date should I act by?"
    )
    tg.send_message(chat_id, "_Analyzing…_")
    try:
        advice = llm_parser.get_parser().ask(prompt, context)
    except Exception as e:  # noqa: BLE001
        tg.send_message(chat_id, f"_Could not analyze strategy: {e}_")
        return
    tg.send_message(chat_id, f"*Strategy:*\n_{strategy_text}_\n\n*Advisor:*\n{advice}")


def _cmd_context(text: str, chat_id: int, user_id: int) -> None:
    """`/context [set <note> | clear]` — persistent advisor instruction injected into every /ask and /strategy call."""
    parts = text.strip().split(None, 2)
    sub = parts[1].lower() if len(parts) >= 2 else ""

    if sub == "set":
        note = parts[2].strip() if len(parts) > 2 else ""
        if not note:
            tg.send_message(chat_id, "_Usage: `/context set <instruction>`_")
            return
        database.put_user_config(user_id, "advisor_note", note)
        tg.send_message(chat_id, f"Advisor note saved. It will be included in every `/ask` and `/strategy` call.\n\n_{note}_")
        return

    if sub == "clear":
        database.put_user_config(user_id, "advisor_note", "")
        tg.send_message(chat_id, "_Advisor note cleared._")
        return

    # /context with no sub → show current note
    cfg = database.get_user_config(user_id, "advisor_note")
    note = (cfg.get("value") or "").strip() if cfg else ""
    if note:
        tg.send_message(chat_id, f"*Current advisor note:*\n_{note}_")
    else:
        tg.send_message(
            chat_id,
            "_No advisor note set._\n\n"
            "Use `/context set <instruction>` to add one. Example:\n"
            "`/context set Ignore reconciliation transactions from 2026-05-17. "
            "Those were manual balance corrections, not real trades.`",
        )


_FUND_RESERVED = frozenset({"create", "seed", "alloc", "rename", "delete"})
_FUND_SLUG_RE = re.compile(r"^[a-z0-9_-]+$")
_FUND_ALLOC_RE = re.compile(
    r"^([\d,.]+)(?:\s+at\s+([\d,.]+))?(?:\s+(.+))?$",
    re.IGNORECASE,
)


def _cmd_funds(chat_id: int, user_id: int) -> None:
    funds = database.list_funds(user_id)
    if not funds:
        tg.send_message(
            chat_id,
            "_No virtual funds yet._\n\n"
            "Create one with:\n`/fund create <slug> <full name>`\n"
            "Example: `/fund create pension Ivan's pension`",
        )
        return
    prices = _live_prices(user_id, ["BTC"], "USD")
    btc_price = prices.get("BTC", Decimal("0"))
    state = _replay_state(user_id)
    total_btc = sum(state.balances.get("BTC", {}).values(), Decimal("0"))
    tg.send_message(chat_id, tg.format_funds(funds, btc_price, total_btc))


def _cmd_fund(text: str, chat_id: int, user_id: int) -> None:
    parts = text.strip().split(None, 3)

    if len(parts) == 1:
        _cmd_funds(chat_id, user_id)
        return

    sub = parts[1].lower()

    if sub == "create":
        if len(parts) < 4:
            tg.send_message(
                chat_id,
                "_Usage: `/fund create <slug> <full name>`\n"
                "Example: `/fund create pension Ivan's pension`_",
            )
            return
        slug = parts[2].lower()
        name = parts[3].strip()
        if not _FUND_SLUG_RE.match(slug):
            tg.send_message(chat_id, "_Slug must be lowercase letters, digits, `-` or `_` only._")
            return
        if slug in _FUND_RESERVED:
            tg.send_message(chat_id, f"_`{slug}` is a reserved word. Choose a different slug._")
            return
        if database.get_fund(user_id, slug):
            tg.send_message(chat_id, f"_Fund `{slug}` already exists. Use `/fund rename {slug} <name>` to rename it._")
            return
        database.put_fund(user_id, slug, name)
        tg.send_message(
            chat_id,
            f"Fund *{name}* (`{slug}`) created.\n\n"
            f"Seed historical totals: `/fund seed {slug} <usd> <btc>`\n"
            f"Add first allocation: `/fund alloc {slug} <usd>`",
        )
        return

    if sub == "seed":
        if len(parts) < 4:
            tg.send_message(
                chat_id,
                "_Usage: `/fund seed <slug> <usd> <btc>`\n"
                "Example: `/fund seed pension 5000 0.05`_",
            )
            return
        slug = parts[2].lower()
        fund = database.get_fund(user_id, slug)
        if not fund:
            tg.send_message(chat_id, f"_Fund `{slug}` not found. Create it first: `/fund create {slug} <name>`_")
            return
        rest = parts[3].split()
        if len(rest) < 2:
            tg.send_message(chat_id, "_Provide both USD and BTC amounts._")
            return
        try:
            usd = Decimal(rest[0].replace(",", ""))
            btc = Decimal(rest[1].replace(",", ""))
        except Exception:
            tg.send_message(chat_id, "_Could not parse amounts._")
            return
        database.seed_fund_totals(user_id, slug, usd, btc)
        tg.send_message(
            chat_id,
            f"Fund `{slug}` seeded:\n  Invested: ${tg._fmt(usd)} · {tg._fmt(btc)} BTC",
        )
        return

    if sub == "alloc":
        if len(parts) < 4:
            tg.send_message(
                chat_id,
                "_Usage: `/fund alloc <slug> <usd> [at <rate>]`\n"
                "Example: `/fund alloc pension 200` or `/fund alloc pension 200 at 77023`_",
            )
            return
        slug = parts[2].lower()
        fund = database.get_fund(user_id, slug)
        if not fund:
            tg.send_message(chat_id, f"_Fund `{slug}` not found._")
            return
        m = _FUND_ALLOC_RE.match(parts[3].strip())
        if not m:
            tg.send_message(chat_id, "_Could not parse. Example: `/fund alloc pension 200 at 77023`_")
            return
        try:
            usd = Decimal(m.group(1).replace(",", ""))
        except Exception:
            tg.send_message(chat_id, "_Invalid USD amount._")
            return
        note = (m.group(3) or "").strip()
        if m.group(2):
            try:
                rate = Decimal(m.group(2).replace(",", ""))
            except Exception:
                tg.send_message(chat_id, "_Invalid rate._")
                return
        else:
            prices = _live_prices(user_id, ["BTC"], "USD")
            rate = prices.get("BTC", Decimal("0"))
            if not rate:
                tg.send_message(
                    chat_id,
                    "_Could not fetch live BTC price. Provide rate manually:\n"
                    "`/fund alloc <slug> <usd> at <rate>`_",
                )
                return
        btc = (usd / rate).quantize(Decimal("0.00000001"))
        now_iso = datetime.now(timezone.utc).isoformat()
        database.update_fund_totals(user_id, slug, usd, btc, now_iso)
        database.put_fund_contrib(user_id, slug, {
            "usd_amount": usd,
            "btc_amount": btc,
            "btc_rate": rate,
            "note": note,
            "timestamp": now_iso,
        })
        tg.send_message(
            chat_id,
            f"Allocated to *{fund['name']}*:\n"
            f"  ${tg._fmt(usd)} → {tg._fmt(btc)} BTC @ ${tg._fmt(rate)}/BTC"
            + (f"\n  _{note}_" if note else ""),
        )
        return

    if sub == "rename":
        if len(parts) < 4:
            tg.send_message(chat_id, "_Usage: `/fund rename <slug> <new name>`_")
            return
        slug = parts[2].lower()
        new_name = parts[3].strip()
        if not database.get_fund(user_id, slug):
            tg.send_message(chat_id, f"_Fund `{slug}` not found._")
            return
        database.rename_fund(user_id, slug, new_name)
        tg.send_message(chat_id, f"Fund `{slug}` renamed to *{new_name}*.")
        return

    if sub == "delete":
        if len(parts) < 3:
            tg.send_message(chat_id, "_Usage: `/fund delete <slug>`_")
            return
        slug = parts[2].lower()
        confirm = (parts[3].strip().lower() if len(parts) >= 4 else "")
        fund = database.get_fund(user_id, slug)
        if not fund:
            tg.send_message(chat_id, f"_Fund `{slug}` not found._")
            return
        if confirm != "confirm":
            tg.send_message(
                chat_id,
                f"*Delete fund `{slug}` ({fund['name']})?*\n"
                f"This removes the fund and all contribution history.\n\n"
                f"Confirm: `/fund delete {slug} confirm`",
            )
            return
        database.delete_fund(user_id, slug)
        tg.send_message(chat_id, f"Fund `{slug}` deleted.")
        return

    # /fund <slug> — detail view
    slug = sub
    if slug in _FUND_RESERVED:
        tg.send_message(chat_id, "_Unknown command. Try `/funds` or `/help`._")
        return
    fund = database.get_fund(user_id, slug)
    if not fund:
        tg.send_message(
            chat_id,
            f"_Fund `{slug}` not found. Use `/funds` to list or `/fund create {slug} <name>` to create._",
        )
        return
    prices = _live_prices(user_id, ["BTC"], "USD")
    btc_price = prices.get("BTC", Decimal("0"))
    contribs = database.list_fund_contribs(user_id, slug, limit=5)
    tg.send_message(chat_id, tg.format_fund_detail(fund, btc_price, contribs))


def _cmd_wipe(text: str, chat_id: int, user_id: int) -> None:
    parts = text.strip().split()
    if len(parts) >= 2 and parts[1].lower() == "confirm":
        count = database.delete_all_user_data(user_id)
        tg.send_message(chat_id, f"Done — {count} records deleted. Your ledger is now empty.")
        return
    tg.send_message(
        chat_id,
        "*Warning: this will permanently delete ALL your transaction history.*\n\n"
        "Type `/wipe confirm` to proceed.",
    )


def _cmd_freeform(text: str, chat_id: int, user_id: int, base_currency: str) -> None:
    now_iso = datetime.now(timezone.utc).isoformat()
    try:
        parsed = llm_parser.get_parser().parse(text, now_iso)
    except llm_parser.LowConfidenceError as e:
        tg.send_message(chat_id, f"_I'm not sure I understood (confidence {e.partial.get('confidence', 0):.2f}). Could you clarify?_")
        return
    except llm_parser.ParseError as e:
        tg.send_message(chat_id, f"_Could not parse: {e}_")
        return

    op = parsed.get("operation")
    if op == "QUERY":
        tg.send_message(chat_id, "_Heard a question. Try /balance, /pnl, /history, /fees, /unrealized, /stats, or /help._")
        return

    if op == "SET_BALANCE":
        _reconcile(chat_id, user_id, parsed.get("asset"), parsed.get("destination"),
                   parsed.get("amount"), parsed.get("raw_text", text))
        return

    timestamp = parsed.get("timestamp") or now_iso
    tx = {
        "operation": op,
        "asset": parsed.get("asset"),
        "amount": parsed.get("amount", 0),
        "price": parsed.get("price", 0),
        "quote_asset": parsed.get("quote_asset"),
        "total_quote_value": parsed.get("total_quote_value"),
        "source": parsed.get("source"),
        "destination": parsed.get("destination"),
        "fee_amount": parsed.get("fee_amount", 0),
        "fee_asset": parsed.get("fee_asset"),
        "timestamp": timestamp,
        "raw_text": parsed.get("raw_text", text),
        "imported": False,
    }
    database.put_transaction(user_id, tx)
    asset = tx["asset"] or "?"
    amount = tx["amount"]
    quote_str = ""
    if tx.get("price") and tx.get("quote_asset"):
        quote_str = f" @ {tx['price']} {tx['quote_asset']}"

    strategy_hint = ""
    if op in ("P2P_BUY", "SPOT_BUY", "P2P_SELL", "SPOT_SELL"):
        cfg = database.get_user_config(user_id, "strategy")
        if cfg and cfg.get("value"):
            strategy_hint = "\n\n_Run /strategy to check your next step._"

    tg.send_message(chat_id, f"Logged: *{op}* `{amount}` {asset}{quote_str}{strategy_hint}")


# ---------------------------------------------------------------------------
# Price fetch (CoinGecko free tier, cached in DynamoDB)
# ---------------------------------------------------------------------------


def _live_prices(user_id: int, assets: list, vs_currency: str) -> dict:
    """Return {asset: Decimal price} for assets we know how to look up. Skips missing."""
    prices: dict = {}
    to_fetch: list = []
    vs_lower = vs_currency.lower()
    for asset in assets:
        if asset == vs_currency:
            prices[asset] = Decimal("1")
            continue
        cached = database.get_cached_price(user_id, asset)
        if cached and cached.get("quote") == vs_currency:
            prices[asset] = Decimal(str(cached["price"]))
            continue
        if asset in COINGECKO_IDS:
            to_fetch.append(asset)

    if to_fetch:
        ids = ",".join(COINGECKO_IDS[a] for a in to_fetch)
        try:
            resp = requests.get(
                "https://api.coingecko.com/api/v3/simple/price",
                params={"ids": ids, "vs_currencies": vs_lower},
                timeout=6,
            )
            data = resp.json() if resp.ok else {}
        except requests.RequestException:
            data = {}
        for asset in to_fetch:
            cg_id = COINGECKO_IDS[asset]
            value = data.get(cg_id, {}).get(vs_lower)
            if value is None:
                continue
            prices[asset] = Decimal(str(value))
            try:
                database.put_cached_price(user_id, asset, float(value), vs_currency, ttl_seconds=300)
            except Exception:  # noqa: BLE001
                logger.warning("Failed to cache price for %s", asset)
    return prices
