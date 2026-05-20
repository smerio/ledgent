"""Telegram Bot API helpers and message formatters."""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from decimal import Decimal
from typing import Iterable

import requests


_API = "https://api.telegram.org/bot{token}/{method}"


def send_message(chat_id: int | str, text: str, parse_mode: str = "Markdown") -> None:
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    payload = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True,
    }
    if parse_mode:
        payload["parse_mode"] = parse_mode

    resp = requests.post(
        _API.format(token=token, method="sendMessage"),
        json=payload,
        timeout=10,
    )
    data = resp.json()
    if not data.get("ok"):
        # If Markdown parsing fails, fallback to plain text so the user at least gets the message!
        if parse_mode == "Markdown" and "parse" in data.get("description", "").lower():
            logging.getLogger(__name__).warning("Telegram Markdown parsing failed, retrying in plain text...")
            fallback_payload = payload.copy()
            fallback_payload.pop("parse_mode", None)
            resp = requests.post(
                _API.format(token=token, method="sendMessage"),
                json=fallback_payload,
                timeout=10,
            )
            data = resp.json()
            if data.get("ok"):
                return
        logging.getLogger(__name__).error("Telegram sendMessage failed: %s", data)


def _fmt(n) -> str:
    """Format a Decimal/number compactly: 8 sig digits, no trailing zeros."""
    if not isinstance(n, Decimal):
        n = Decimal(str(n))
    # Quantize tiny values to 8 places, large values to 2 places.
    if abs(n) < Decimal("1"):
        s = f"{n:.8f}"
    elif abs(n) < Decimal("100"):
        s = f"{n:.4f}"
    else:
        s = f"{n:.2f}"
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s


def format_balance(balances: dict, base_currency: str, valuations: dict | None = None) -> str:
    """Render a /balance reply.

    `balances` is asset → {location: Decimal}.
    `valuations` is asset → Decimal (per-unit value in base_currency), optional.
    """
    if not balances:
        return "_No transactions logged yet._"
    lines = ["*Balances*"]
    total_value = Decimal("0")
    for asset in sorted(balances):
        per_loc = balances[asset]
        total = sum(per_loc.values(), Decimal("0"))
        if total == 0 and not any(v != 0 for v in per_loc.values()):
            continue
        lines.append(f"\n*{asset}*  `{_fmt(total)}`")
        for loc in sorted(per_loc):
            v = per_loc[loc]
            if v == 0:
                continue
            lines.append(f"  • {loc}: `{_fmt(v)}`")
        if valuations and asset in valuations:
            v = total * valuations[asset]
            total_value += v
            lines.append(f"  _≈ {_fmt(v)} {base_currency}_")
    if valuations:
        lines.append(f"\n*Total ≈ {_fmt(total_value)} {base_currency}*")
    return "\n".join(lines)


def format_pnl(
    pnl_total_usd: Decimal,
    pnl_asset_usd: Decimal,
    pnl_fx_usd: Decimal,
    fees_in_base: Decimal,
    base_currency: str,
    fees_by_asset: dict,
) -> str:
    net = pnl_total_usd - fees_in_base
    lines = [
        f"*Realized PnL ≈ {_fmt(pnl_total_usd)} {base_currency}*",
        f"  • Asset price changes: `{_fmt(pnl_asset_usd)}` {base_currency}",
        f"  • FX rate movements:   `{_fmt(pnl_fx_usd)}` {base_currency}",
        f"  • Fees paid (≈):       `{_fmt(fees_in_base)}` {base_currency}",
        f"\n*Net ≈ {_fmt(net)} {base_currency}*",
    ]
    if fees_by_asset:
        lines.append("\n_Fees by asset:_")
        for asset in sorted(fees_by_asset):
            lines.append(f"  • {asset}: `{_fmt(fees_by_asset[asset])}`")
    return "\n".join(lines)


def format_unrealized(unrealized: Decimal, base_currency: str, breakdown: dict | None = None) -> str:
    lines = [f"*Unrealized PnL ≈ {_fmt(unrealized)} {base_currency}*"]
    if breakdown:
        lines.append("")
        for asset in sorted(breakdown):
            lines.append(f"  • {asset}: `{_fmt(breakdown[asset])}`")
    return "\n".join(lines)


def format_history(transactions: Iterable[dict], limit: int = 10) -> str:
    txs = list(transactions)[-limit:]
    if not txs:
        return "_No transactions yet._"
    lines = [f"*Last {len(txs)} transactions*"]
    for t in txs:
        op = t.get("operation", "?")
        asset = t.get("asset", "?")
        amount = t.get("amount", 0)
        quote = t.get("quote_asset") or ""
        price = t.get("price") or 0
        ts = (t.get("timestamp") or "")[:10]
        suffix = f" @ {_fmt(price)} {quote}" if quote and Decimal(str(price)) > 0 else ""
        lines.append(f"`{ts}`  `{op}`  {_fmt(amount)} {asset}{suffix}")
    return "\n".join(lines)


def format_fees(fees: dict) -> str:
    if not fees:
        return "_No fees recorded._"
    lines = ["*Total fees paid*"]
    for asset in sorted(fees):
        lines.append(f"  • {asset}: `{_fmt(fees[asset])}`")
    return "\n".join(lines)


def format_stats(stats: dict) -> str:
    if not stats:
        return "_Not enough data for stats yet._"
    header = "*Investment stats*"
    if stats.get("asset"):
        header += f" — {stats['asset']}"
    lines = [header]
    if "total_buys" in stats:
        lines.append(f"• Total buys logged: `{stats['total_buys']}`")
    if "avg_buy_price" in stats:
        quote = stats.get("quote_asset", "")
        suffix = f" {quote}" if quote else ""
        lines.append(f"• Average buy price: `{_fmt(stats['avg_buy_price'])}`{suffix}")
    elif "asset" not in stats:
        lines.append("_Use `/stats <asset>` (e.g. `/stats BTC`) for a meaningful avg buy price._")
    if "avg_days_between_buys" in stats:
        lines.append(f"• Avg days between buys: `{_fmt(stats['avg_days_between_buys'])}`")
    if "first_tx" in stats:
        lines.append(f"• First transaction: `{stats['first_tx']}`")
    if "last_tx" in stats:
        lines.append(f"• Last transaction: `{stats['last_tx']}`")
    return "\n".join(lines)


def format_funds(funds: list, btc_price: Decimal, total_btc: Decimal) -> str:
    price_str = f"${_fmt(btc_price)}" if btc_price else "price unavailable"
    lines = [f"*Virtual Funds* · BTC {price_str}"]
    allocated_btc = Decimal("0")

    for f in funds:
        slug = f.get("slug", "?")
        name = f.get("name", "?")
        invested_usd = Decimal(str(f.get("invested_usd", 0)))
        invested_btc = Decimal(str(f.get("invested_btc", 0)))
        last_ts = f.get("last_contrib_at", "")
        allocated_btc += invested_btc

        if btc_price and invested_btc:
            current_val = invested_btc * btc_price
            pnl = current_val - invested_usd
            pnl_pct = pnl / invested_usd * 100 if invested_usd else Decimal("0")
            avg_cost = invested_usd / invested_btc
            below_cost = btc_price < avg_cost
            sign = "+" if pnl >= 0 else ""
            val_str = f"now ${_fmt(current_val)}  {sign}${_fmt(pnl)} ({sign}{_fmt(pnl_pct)}%)"
            cost_warn = "  _[below avg cost]_" if below_cost else ""
        else:
            val_str = "_price unavailable_"
            cost_warn = ""

        if last_ts:
            try:
                dt = datetime.fromisoformat(last_ts.replace("Z", "+00:00"))
                days = (datetime.now(timezone.utc) - dt).days
                last_str = "today" if days == 0 else f"{days}d ago"
            except ValueError:
                last_str = last_ts[:10]
        else:
            last_str = "never"

        lines.append(f"\n*{slug}* — {name}{cost_warn}")
        lines.append(f"  ${_fmt(invested_usd)} in · {val_str}")
        lines.append(f"  Last contrib: {last_str}")

    unalloc = total_btc - allocated_btc
    if btc_price and unalloc > Decimal("0.00000001"):
        lines.append(f"\nUnallocated: {_fmt(unalloc)} BTC (${_fmt(unalloc * btc_price)})")
    elif unalloc < Decimal("-0.00000001"):
        lines.append(f"\n_Over-allocated by {_fmt(-unalloc)} BTC — check fund totals._")

    return "\n".join(lines)


def format_fund_detail(fund: dict, btc_price: Decimal, contribs: list) -> str:
    slug = fund.get("slug", "?")
    name = fund.get("name", "?")
    invested_usd = Decimal(str(fund.get("invested_usd", 0)))
    invested_btc = Decimal(str(fund.get("invested_btc", 0)))

    lines = [f"*{slug}* — {name}"]
    lines.append(f"Invested: ${_fmt(invested_usd)} · {_fmt(invested_btc)} BTC")

    if btc_price and invested_btc:
        current_val = invested_btc * btc_price
        pnl = current_val - invested_usd
        pnl_pct = pnl / invested_usd * 100 if invested_usd else Decimal("0")
        avg_cost = invested_usd / invested_btc
        below_cost = btc_price < avg_cost
        sign = "+" if pnl >= 0 else ""
        lines.append(f"Current:  ${_fmt(current_val)} · {sign}${_fmt(pnl)} ({sign}{_fmt(pnl_pct)}%)")
        lines.append(f"Avg cost: ${_fmt(avg_cost)}/BTC  ·  now ${_fmt(btc_price)}/BTC")
        if below_cost:
            lines.append("_Below avg cost — potential loss if sold at current price._")
    else:
        lines.append("_Live price unavailable._")

    if contribs:
        lines.append(f"\n*Last {len(contribs)} contributions:*")
        for c in contribs:
            ts = (c.get("timestamp") or "")[:10]
            c_usd = Decimal(str(c.get("usd_amount", 0)))
            c_btc = Decimal(str(c.get("btc_amount", 0)))
            c_rate = Decimal(str(c.get("btc_rate", 0)))
            note = (c.get("note") or "").strip()
            note_str = f" — _{note}_" if note else ""
            lines.append(f"  `{ts}`  ${_fmt(c_usd)} · {_fmt(c_btc)} BTC @ ${_fmt(c_rate)}{note_str}")
    else:
        lines.append("\n_No contributions logged. Use `/fund seed` or `/fund alloc`._")

    lines.append(f"\n`/fund alloc {slug} <usd>` to add a contribution")
    return "\n".join(lines)


HELP_TEXT = (
    "*Portfolio*\n"
    "/balance — assets grouped by location\n"
    "/pnl — realized PnL in USD (asset + FX components, minus fees)\n"
    "/unrealized — unrealized PnL at live prices\n"
    "/history [N] — last N transactions (default 10)\n"
    "/fees — total fees paid, by asset\n"
    "/stats [asset] — DCA frequency; pass an asset for avg buy price\n"
    "/sim sell <amount> <asset> — exact FIFO sell simulation with per-lot breakdown\n\n"
    "*Advisor*\n"
    "/ask <question> — ask anything about your portfolio or strategy\n"
    "/strategy — check what your next strategy step should be\n"
    "/strategy set <text> — save your personal DCA/trading strategy\n"
    "/strategy clear — remove saved strategy\n"
    "/context — view persistent advisor instruction\n"
    "/context set <text> — add note injected into every /ask and /strategy\n"
    "/context clear — remove advisor note\n\n"
    "*Alerts*\n"
    "/alert <asset> > <price> — alert when asset crosses above price\n"
    "/alert <asset> < <price> — alert when asset crosses below price\n"
    "/alert <asset> <pct>% — alert on relative percent movement (e.g. 5%)\n"
    "/alert clear <asset> — remove active alerts for an asset\n"
    "/alerts — list all active custom price alerts\n\n"
    "*Funds*\n"
    "/funds — overview of all virtual funds with P&L\n"
    "/fund <slug> — detail view + contribution history\n"
    "/fund create <slug> <name> — create a new fund\n"
    "/fund seed <slug> <usd> <btc> — set historical totals (bootstrap)\n"
    "/fund alloc <slug> <usd> [at <rate>] — record a monthly allocation\n"
    "/fund rename <slug> <name> — rename a fund\n"
    "/fund delete <slug> — remove a fund (asks for confirmation)\n\n"
    "*Manage*\n"
    "/set <asset> <location> <amount> — reconcile a position to an exact value\n"
    "/wipe — delete all transaction history (asks for confirmation)\n"
    "/help — this message\n\n"
    "*Natural language*\n"
    "Just describe operations in plain text:\n"
    "  `bought 1205 usdt for 82.99 rub on bybit p2p`\n"
    "  `transferred 0.01 btc from binance to ledger, fee 0.0001`\n"
    "  `sold 1000 usdt for 871 eur`\n"
    "  `ledger btc = 1.23456789` _(reconcile a balance)_\n"
)


def format_alerts_list(alerts: list[dict]) -> str:
    """Render a /alerts reply."""
    if not alerts:
        return "_No active price alerts set._\n\nUse `/alert <asset> > <price>` or `/alert <asset> 5%` to create one."
    lines = ["*Active Price Alerts*"]
    by_asset: dict[str, list[dict]] = {}
    for a in alerts:
        asset = a.get("asset", "?").upper()
        by_asset.setdefault(asset, []).append(a)
    for asset in sorted(by_asset):
        lines.append(f"\n*{asset}*")
        for a in sorted(by_asset[asset], key=lambda x: x.get("created_at", "")):
            cond = a.get("condition", "")
            target = Decimal(str(a.get("target", 0)))
            baseline = a.get("baseline_price")
            alert_id = a.get("alert_id", "")
            
            if cond == ">":
                lines.append(f"  • Crosses above `${_fmt(target)}`  `[ID: {alert_id[:4]}]`")
            elif cond == "<":
                lines.append(f"  • Crosses below `${_fmt(target)}`  `[ID: {alert_id[:4]}]`")
            elif cond == "%":
                base_str = f" (base: `${_fmt(baseline)}`)" if baseline else ""
                lines.append(f"  • Moves by `{_fmt(target)}%`{base_str}  `[ID: {alert_id[:4]}]`")
    return "\n".join(lines)


def format_custom_alert_triggered(
    asset: str,
    condition: str,
    target: Decimal,
    current_price: Decimal,
    baseline_price: Decimal | None = None,
) -> str:
    """Format triggered custom price alert."""
    title = "🔔 *Price Alert Triggered!*"
    if condition == ">":
        details = f"*{asset}* crossed above your target of `${_fmt(target)}`.\n• Current Price: `${_fmt(current_price)} USD`"
    elif condition == "<":
        details = f"*{asset}* crossed below your target of `${_fmt(target)}`.\n• Current Price: `${_fmt(current_price)} USD`"
    elif condition == "%":
        change = ((current_price - Decimal(str(baseline_price))) / Decimal(str(baseline_price)) * 100) if baseline_price else Decimal("0")
        sign = "+" if change >= 0 else ""
        details = f"*{asset}* moved by your target of `{_fmt(target)}%`.\n• Current Price: `${_fmt(current_price)} USD` ({sign}{_fmt(change)}% from `${_fmt(baseline_price)}`)"
    else:
        details = f"*{asset}* met your alert condition.\n• Current Price: `${_fmt(current_price)} USD`"
    return f"{title}\n\n{details}"


def format_volatility_alert(asset: str, change_24h: Decimal, current_price: Decimal) -> str:
    """Format volatility warning message."""
    emoji = "🔴" if change_24h < 0 else "🟢"
    sign = "+" if change_24h >= 0 else ""
    return (
        f"⚠️ *{asset} Volatility Alert* {emoji}\n\n"
        f"*{asset}* has moved by *{sign}{_fmt(change_24h)}%* in the last 24 hours!\n"
        f"• Current Price: `${_fmt(current_price)} USD`"
    )

