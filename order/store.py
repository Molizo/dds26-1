"""Order-domain persistence only.

The order service owns order records and the paid marker. Coordinator transaction
state moved to orchestrator-owned storage in Phase 2.
"""
import logging
from dataclasses import dataclass
from typing import Optional

import msgspec
import redis

import lua_scripts
from common.models import OrderSnapshot
from msgspec import msgpack
from redis.commands.core import Script

logger = logging.getLogger(__name__)


class OrderValue(msgspec.Struct):
    paid: bool
    items: list[tuple[str, int]]
    user_id: str
    total_cost: int


_order_encoder = msgspec.msgpack.Encoder()
_order_decoder = msgspec.msgpack.Decoder(OrderValue)
_mark_paid_script: Optional[Script] = None


@dataclass(frozen=True)
class AddItemResult:
    status: str
    total_cost: int | None = None


def get_order(db: redis.Redis, order_id: str) -> Optional[OrderValue]:
    """Return the raw OrderValue or None if not found."""
    try:
        raw = db.get(order_id)
    except redis.exceptions.RedisError as exc:
        logger.error("Redis error reading order %s: %s", order_id, exc)
        return None
    if raw is None:
        return None
    return _order_decoder.decode(raw)


def read_order_snapshot(db: redis.Redis, order_id: str) -> Optional[OrderSnapshot]:
    """Return the current order snapshot for internal orchestrator calls."""
    order = get_order(db, order_id)
    if order is None:
        return None

    paid = order.paid
    if not paid:
        try:
            paid = db.exists(f"order_paid:{order_id}") == 1
        except redis.exceptions.RedisError:
            pass

    return OrderSnapshot(
        order_id=order_id,
        user_id=order.user_id,
        total_cost=order.total_cost,
        paid=paid,
        items=list(order.items),
    )


def _get_mark_paid_script(db: redis.Redis) -> Script:
    global _mark_paid_script
    if _mark_paid_script is None:
        _mark_paid_script = db.register_script(lua_scripts.MARK_ORDER_PAID)
    return _mark_paid_script


def mark_order_paid(db: redis.Redis, order_id: str) -> bool:
    """Atomically and idempotently mark an order as paid."""
    try:
        raw = db.get(order_id)
        if raw is None:
            return False

        script = _get_mark_paid_script(db)
        script(keys=[f"order_paid:{order_id}"])

        order = _order_decoder.decode(raw)
        if order.paid:
            return True

        db.set(
            order_id,
            _order_encoder.encode(
                OrderValue(
                    paid=True,
                    items=order.items,
                    user_id=order.user_id,
                    total_cost=order.total_cost,
                )
            ),
        )
        return True
    except redis.exceptions.RedisError as exc:
        logger.error("Redis error marking order paid %s: %s", order_id, exc)
        return False


def add_item_to_order(
    db: redis.Redis,
    order_id: str,
    item_id: str,
    quantity: int,
    price: int,
    *,
    retry_count: int,
) -> AddItemResult:
    order_paid_key = f"order_paid:{order_id}"

    for _ in range(retry_count):
        try:
            with db.pipeline() as pipe:
                pipe.watch(order_id, order_paid_key)
                raw_order = pipe.get(order_id)
                if raw_order is None:
                    pipe.unwatch()
                    return AddItemResult(status="not_found")

                order = msgpack.decode(raw_order, type=OrderValue)
                if order.paid or pipe.exists(order_paid_key) == 1:
                    pipe.unwatch()
                    return AddItemResult(status="already_paid")

                order.items.append((item_id, quantity))
                order.total_cost += quantity * price

                pipe.multi()
                pipe.set(order_id, msgpack.encode(order))
                pipe.execute()
                return AddItemResult(status="ok", total_cost=order.total_cost)
        except redis.WatchError:
            continue
        except redis.exceptions.RedisError as exc:
            logger.error("Redis error adding item order=%s item=%s: %s", order_id, item_id, exc)
            return AddItemResult(status="db_error")

    return AddItemResult(status="conflict")
