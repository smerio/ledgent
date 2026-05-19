"""Weekly DCA nudge — invoked by EventBridge Scheduler."""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone

import database
import telegram_utils as tg

logger = logging.getLogger()
logger.setLevel(logging.INFO)


CONSISTENCY_CHECK = (
    "*Consistency Check*\n\n"
    "You haven't logged any investment operations in the past 14 days. "
    "Staying regular with DCA is the edge — want to log one now?"
)


def _soft_nudge(days_ago: int) -> str:
    return (
        f"Last buy was *{days_ago} days* ago. "
        "Consider whether it's time for the next one."
    )


def nudge_handler(event, context):
    user_id = int(os.environ["ALLOWED_TELEGRAM_USER_ID"])
    now = datetime.now(timezone.utc)
    since = (now - timedelta(days=14)).isoformat()
    txs = database.query_transactions(user_id, since_iso=since)
    buys = [t for t in txs if t.get("operation") in ("P2P_BUY", "SPOT_BUY")]

    if not buys:
        tg.send_message(user_id, CONSISTENCY_CHECK)
        return {"status": "consistency_check"}

    latest = max(t.get("timestamp", "") for t in buys)
    try:
        latest_dt = datetime.fromisoformat(latest.replace("Z", "+00:00"))
    except ValueError:
        logger.warning("Could not parse latest timestamp %r", latest)
        return {"status": "skip_parse_error"}

    days_since = (now - latest_dt).days
    if days_since > 7:
        tg.send_message(user_id, _soft_nudge(days_since))
        return {"status": "soft_nudge", "days_since": days_since}

    return {"status": "no_nudge_recent_buy", "days_since": days_since}
