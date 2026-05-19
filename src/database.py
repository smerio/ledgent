"""DynamoDB single-table data access for the crypto ledger.

Item shapes
-----------
Transaction:
    PK     = USER#<telegram_id>
    SK     = TX#<iso_ts>#<ulid>
    GSI1PK = USER#<telegram_id>#ASSET#<asset>
    GSI1SK = TX#<iso_ts>#<ulid>
    type   = "tx"
    operation, asset, amount, price, quote_asset, source, destination,
    fee_amount, fee_asset, raw_text, imported, timestamp, ...

FIFO lot (cost-basis):
    PK     = USER#<telegram_id>
    SK     = LOT#<asset>#<ulid>
    GSI1PK = USER#<telegram_id>#ASSET#<asset>
    GSI1SK = LOT#<acquired_at>#<ulid>
    type   = "lot"
    lot_id, asset, amount_remaining, cost_per_unit, quote_asset, acquired_at, location

Forex lot (USDT/stablecoin vs fiat):
    PK     = USER#<telegram_id>
    SK     = FXLOT#<quote>#<ulid>
    type   = "fxlot"
    fx_lot_id, stable_asset, fiat_asset, stable_amount_remaining, fiat_per_stable, acquired_at

Price cache:
    PK     = USER#<telegram_id>
    SK     = PRICE#<asset>
    type   = "price"
    price, quote, fetched_at, ttl (epoch seconds)
"""
from __future__ import annotations

import logging
import os
import time
from decimal import Decimal
from typing import Any, Iterable

import boto3
from boto3.dynamodb.conditions import Key
from ulid import ULID

logger = logging.getLogger("database")



_TABLE_NAME = os.environ.get("DYNAMODB_TABLE_NAME", "crypto-ledger")
_dynamodb = boto3.resource("dynamodb")
_table = _dynamodb.Table(_TABLE_NAME)


def _user_pk(user_id: int | str) -> str:
    return f"USER#{user_id}"


def _to_decimal(value: Any) -> Any:
    """DynamoDB does not accept floats — convert recursively."""
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {k: _to_decimal(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_to_decimal(v) for v in value]
    return value


# ---------------------------------------------------------------------------
# Transactions
# ---------------------------------------------------------------------------


def put_transaction(user_id: int | str, tx: dict) -> dict:
    """Write a transaction item. `tx` must contain at least operation/asset/amount/timestamp."""
    ulid = str(ULID())
    timestamp = tx["timestamp"]
    asset = tx.get("asset", "UNKNOWN")
    item = {
        "PK": _user_pk(user_id),
        "SK": f"TX#{timestamp}#{ulid}",
        "GSI1PK": f"{_user_pk(user_id)}#ASSET#{asset}",
        "GSI1SK": f"TX#{timestamp}#{ulid}",
        "type": "tx",
        "tx_id": ulid,
        **_to_decimal(tx),
    }
    _table.put_item(Item=item)
    return item


def query_transactions(
    user_id: int | str,
    *,
    asset: str | None = None,
    since_iso: str | None = None,
    limit: int | None = None,
    descending: bool = False,
) -> list[dict]:
    """Return transactions for a user, optionally filtered by asset and date range."""
    if asset:
        key_cond = Key("GSI1PK").eq(f"{_user_pk(user_id)}#ASSET#{asset}")
        if since_iso:
            key_cond = key_cond & Key("GSI1SK").gte(f"TX#{since_iso}")
        kwargs = {
            "IndexName": "ByAssetAndDate",
            "KeyConditionExpression": key_cond,
            "ScanIndexForward": not descending,
        }
    else:
        key_cond = Key("PK").eq(_user_pk(user_id)) & Key("SK").begins_with("TX#")
        kwargs = {
            "KeyConditionExpression": key_cond,
            "ScanIndexForward": not descending,
        }
    if limit:
        kwargs["Limit"] = limit

    items: list[dict] = []
    while True:
        resp = _table.query(**kwargs)
        items.extend(resp.get("Items", []))
        if "LastEvaluatedKey" not in resp or (limit and len(items) >= limit):
            break
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]

    if asset is None and since_iso is not None:
        items = [i for i in items if i.get("timestamp", "") >= since_iso]
    if limit:
        items = items[:limit]
    return items


# ---------------------------------------------------------------------------
# FIFO lots (asset cost-basis)
# ---------------------------------------------------------------------------


def put_lot(user_id: int | str, lot: dict) -> dict:
    ulid = str(ULID())
    asset = lot["asset"]
    acquired_at = lot["acquired_at"]
    item = {
        "PK": _user_pk(user_id),
        "SK": f"LOT#{asset}#{ulid}",
        "GSI1PK": f"{_user_pk(user_id)}#ASSET#{asset}",
        "GSI1SK": f"LOT#{acquired_at}#{ulid}",
        "type": "lot",
        "lot_id": ulid,
        **_to_decimal(lot),
    }
    _table.put_item(Item=item)
    return item


def query_open_lots(user_id: int | str, asset: str) -> list[dict]:
    """Return open lots (amount_remaining > 0) for an asset, oldest first."""
    key_cond = (
        Key("GSI1PK").eq(f"{_user_pk(user_id)}#ASSET#{asset}")
        & Key("GSI1SK").begins_with("LOT#")
    )
    resp = _table.query(
        IndexName="ByAssetAndDate",
        KeyConditionExpression=key_cond,
        ScanIndexForward=True,
    )
    return [i for i in resp.get("Items", []) if Decimal(str(i.get("amount_remaining", 0))) > 0]


def update_lot_remaining(user_id: int | str, sk: str, new_remaining: Decimal) -> None:
    _table.update_item(
        Key={"PK": _user_pk(user_id), "SK": sk},
        UpdateExpression="SET amount_remaining = :r",
        ExpressionAttributeValues={":r": _to_decimal(new_remaining)},
    )


# ---------------------------------------------------------------------------
# Forex lots (stablecoin vs fiat)
# ---------------------------------------------------------------------------


def put_fx_lot(user_id: int | str, lot: dict) -> dict:
    ulid = str(ULID())
    item = {
        "PK": _user_pk(user_id),
        "SK": f"FXLOT#{lot['fiat_asset']}#{ulid}",
        "type": "fxlot",
        "fx_lot_id": ulid,
        **_to_decimal(lot),
    }
    _table.put_item(Item=item)
    return item


def query_open_fx_lots(user_id: int | str, fiat_asset: str) -> list[dict]:
    key_cond = Key("PK").eq(_user_pk(user_id)) & Key("SK").begins_with(f"FXLOT#{fiat_asset}#")
    resp = _table.query(KeyConditionExpression=key_cond, ScanIndexForward=True)
    items = [
        i for i in resp.get("Items", [])
        if Decimal(str(i.get("stable_amount_remaining", 0))) > 0
    ]
    items.sort(key=lambda x: x.get("acquired_at", ""))
    return items


def update_fx_lot_remaining(user_id: int | str, sk: str, new_remaining: Decimal) -> None:
    _table.update_item(
        Key={"PK": _user_pk(user_id), "SK": sk},
        UpdateExpression="SET stable_amount_remaining = :r",
        ExpressionAttributeValues={":r": _to_decimal(new_remaining)},
    )


# ---------------------------------------------------------------------------
# Price cache
# ---------------------------------------------------------------------------


def get_cached_price(user_id: int | str, asset: str) -> dict | None:
    resp = _table.get_item(Key={"PK": _user_pk(user_id), "SK": f"PRICE#{asset}"})
    item = resp.get("Item")
    if not item:
        return None
    if int(item.get("ttl", 0)) < int(time.time()):
        return None
    return item


def put_cached_price(user_id: int | str, asset: str, price: float, quote: str, ttl_seconds: int = 300) -> None:
    _table.put_item(Item={
        "PK": _user_pk(user_id),
        "SK": f"PRICE#{asset}",
        "type": "price",
        "asset": asset,
        "quote": quote,
        "price": _to_decimal(price),
        "fetched_at": int(time.time()),
        "ttl": int(time.time()) + ttl_seconds,
    })


# ---------------------------------------------------------------------------
# Bulk import support
# ---------------------------------------------------------------------------


def batch_put_items(items: Iterable[dict]) -> int:
    """Write items using BatchWriter. Caller is responsible for shape."""
    n = 0
    with _table.batch_writer() as bw:
        for item in items:
            bw.put_item(Item=_to_decimal(item))
            n += 1
    return n


def transaction_exists(user_id: int | str, timestamp: str, operation: str, amount: Decimal, asset: str) -> bool:
    """Best-effort duplicate check for CSV imports — match by (date prefix, op, asset, amount)."""
    date_prefix = timestamp[:10]
    key_cond = Key("PK").eq(_user_pk(user_id)) & Key("SK").begins_with(f"TX#{date_prefix}")
    resp = _table.query(KeyConditionExpression=key_cond)
    target = Decimal(str(amount))
    for i in resp.get("Items", []):
        if (
            i.get("operation") == operation
            and i.get("asset") == asset
            and Decimal(str(i.get("amount", 0))) == target
        ):
            return True
    return False


# ---------------------------------------------------------------------------
# User config (strategy, preferences)
# ---------------------------------------------------------------------------


def get_user_config(user_id: int | str, key: str) -> dict | None:
    resp = _table.get_item(Key={"PK": _user_pk(user_id), "SK": f"CONFIG#{key}"})
    return resp.get("Item")


def put_user_config(user_id: int | str, key: str, value: str) -> None:
    _table.put_item(Item={
        "PK": _user_pk(user_id),
        "SK": f"CONFIG#{key}",
        "type": "config",
        "key": key,
        "value": value,
    })


# ---------------------------------------------------------------------------
# Advisor conversation session (short-lived multi-turn state)
# ---------------------------------------------------------------------------


def get_advisor_session(user_id: int | str) -> list | None:
    """Return active message list or None if no session / TTL expired."""
    resp = _table.get_item(Key={"PK": _user_pk(user_id), "SK": "SESSION#advisor"})
    item = resp.get("Item")
    if not item or int(item.get("ttl", 0)) < int(time.time()):
        return None
    return list(item.get("messages", []))


def put_advisor_session(user_id: int | str, messages: list, ttl_seconds: int = 300) -> None:
    """Save advisor conversation state with a 5-minute TTL."""
    _table.put_item(Item={
        "PK": _user_pk(user_id),
        "SK": "SESSION#advisor",
        "type": "session",
        "messages": messages,
        "ttl": int(time.time()) + ttl_seconds,
    })


def clear_advisor_session(user_id: int | str) -> None:
    _table.delete_item(Key={"PK": _user_pk(user_id), "SK": "SESSION#advisor"})


# ---------------------------------------------------------------------------
# Wipe
# ---------------------------------------------------------------------------


def delete_all_user_data(user_id: int | str) -> int:
    """Delete every DynamoDB item owned by this user. Returns count deleted."""
    pk = _user_pk(user_id)
    items: list[dict] = []
    kwargs: dict = {"KeyConditionExpression": Key("PK").eq(pk)}
    while True:
        resp = _table.query(**kwargs)
        items.extend(resp.get("Items", []))
        if "LastEvaluatedKey" not in resp:
            break
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    with _table.batch_writer() as bw:
        for item in items:
            bw.delete_item(Key={"PK": item["PK"], "SK": item["SK"]})
    return len(items)


# ---------------------------------------------------------------------------
# Virtual funds
# ---------------------------------------------------------------------------


def put_fund(user_id: int | str, slug: str, name: str) -> None:
    _table.put_item(Item={
        "PK": _user_pk(user_id),
        "SK": f"FUND#{slug}",
        "type": "fund",
        "slug": slug,
        "name": name,
        "invested_usd": Decimal("0"),
        "invested_btc": Decimal("0"),
    })


def get_fund(user_id: int | str, slug: str) -> dict | None:
    resp = _table.get_item(Key={"PK": _user_pk(user_id), "SK": f"FUND#{slug}"})
    return resp.get("Item")


def list_funds(user_id: int | str) -> list[dict]:
    key_cond = Key("PK").eq(_user_pk(user_id)) & Key("SK").begins_with("FUND#")
    resp = _table.query(KeyConditionExpression=key_cond, ScanIndexForward=True)
    return resp.get("Items", [])


def update_fund_totals(
    user_id: int | str,
    slug: str,
    usd_delta: Decimal,
    btc_delta: Decimal,
    last_contrib_at: str,
) -> None:
    _table.update_item(
        Key={"PK": _user_pk(user_id), "SK": f"FUND#{slug}"},
        UpdateExpression="SET last_contrib_at = :ts ADD invested_usd :usd, invested_btc :btc",
        ExpressionAttributeValues={
            ":ts": last_contrib_at,
            ":usd": _to_decimal(usd_delta),
            ":btc": _to_decimal(btc_delta),
        },
    )


def seed_fund_totals(
    user_id: int | str,
    slug: str,
    invested_usd: Decimal,
    invested_btc: Decimal,
) -> None:
    """Overwrite fund totals directly (for bootstrapping historical data, no contrib log)."""
    _table.update_item(
        Key={"PK": _user_pk(user_id), "SK": f"FUND#{slug}"},
        UpdateExpression="SET invested_usd = :usd, invested_btc = :btc",
        ExpressionAttributeValues={
            ":usd": _to_decimal(invested_usd),
            ":btc": _to_decimal(invested_btc),
        },
    )


def put_fund_contrib(user_id: int | str, slug: str, contrib: dict) -> None:
    ulid = str(ULID())
    _table.put_item(Item={
        "PK": _user_pk(user_id),
        "SK": f"FUNDLOG#{slug}#{ulid}",
        "type": "fundlog",
        "slug": slug,
        "log_id": ulid,
        **_to_decimal(contrib),
    })


def list_fund_contribs(user_id: int | str, slug: str, limit: int = 5) -> list[dict]:
    """Return the last N contribution log entries for a fund, newest first."""
    key_cond = Key("PK").eq(_user_pk(user_id)) & Key("SK").begins_with(f"FUNDLOG#{slug}#")
    resp = _table.query(
        KeyConditionExpression=key_cond,
        ScanIndexForward=False,
        Limit=limit,
    )
    return resp.get("Items", [])


def rename_fund(user_id: int | str, slug: str, new_name: str) -> None:
    _table.update_item(
        Key={"PK": _user_pk(user_id), "SK": f"FUND#{slug}"},
        UpdateExpression="SET #n = :name",
        ExpressionAttributeNames={"#n": "name"},
        ExpressionAttributeValues={":name": new_name},
    )


def delete_fund(user_id: int | str, slug: str) -> int:
    """Delete the fund record and all its contribution logs. Returns count deleted."""
    pk = _user_pk(user_id)
    items = []
    fund_resp = _table.get_item(Key={"PK": pk, "SK": f"FUND#{slug}"})
    if fund_resp.get("Item"):
        items.append(fund_resp["Item"])
    key_cond = Key("PK").eq(pk) & Key("SK").begins_with(f"FUNDLOG#{slug}#")
    log_resp = _table.query(KeyConditionExpression=key_cond)
    items.extend(log_resp.get("Items", []))
    with _table.batch_writer() as bw:
        for item in items:
            bw.delete_item(Key={"PK": item["PK"], "SK": item["SK"]})
    return len(items)


# ---------------------------------------------------------------------------
# Update dedup lock
# ---------------------------------------------------------------------------


def acquire_update_lock(user_id: int | str, update_id: int | str) -> bool:
    """Try to insert an update ID record to lock it and prevent double-processing.

    Returns True if acquired (first time), False if duplicate.
    """
    if not update_id:
        return True
    try:
        ttl = int(time.time()) + 86400  # 24 hours TTL
        _table.put_item(
            Item={
                "PK": _user_pk(user_id),
                "SK": f"UPDATE#{update_id}",
                "type": "update_lock",
                "ttl": ttl,
            },
            ConditionExpression="attribute_not_exists(PK) AND attribute_not_exists(SK)",
        )
        return True
    except Exception as e:
        if hasattr(e, "response") and e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return False
        logger.warning("Failed to acquire update lock: %s", e)
        return True
