"""Coordinator transaction record definition.

CheckoutTxValue is the canonical durable record per checkout transaction,
stored as msgpack in Redis under key tx:{tx_id}.

This module must NOT import Flask — the coordinator is transport-agnostic
and must remain extractable into a standalone service in Phase 2.
"""
import time
from typing import Optional

import msgspec


class CheckoutTxValue(msgspec.Struct):
    tx_id: str
    order_id: str
    # Copied from order at tx creation so the coordinator does not need a
    # second read during command publishing.
    user_id: str
    total_cost: int
    # "saga" or "2pc"
    protocol: str
    # See common.constants STATUS_* values
    status: str
    # "commit" or "abort" once the coordinator has decided; None before
    decision: Optional[str]
    # Aggregated, deterministic item snapshot: [(item_id, quantity), ...]
    items_snapshot: list[tuple[str, int]]

    # Participant progress flags — updated incrementally as replies arrive
    # (not deferred to the status transition) so recovery can inspect
    # partial progress after a coordinator crash mid-HOLDING.
    stock_held: bool
    stock_committed: bool
    stock_released: bool
    payment_held: bool
    payment_committed: bool
    payment_released: bool

    last_error: Optional[str]
    created_at: int       # epoch ms
    updated_at: int       # epoch ms
    retry_count: int


def make_tx(
    tx_id: str,
    order_id: str,
    user_id: str,
    total_cost: int,
    protocol: str,
    items_snapshot: list[tuple[str, int]],
    status: str,
) -> CheckoutTxValue:
    """Create a new CheckoutTxValue in INIT state."""
    now = int(time.time() * 1000)
    return CheckoutTxValue(
        tx_id=tx_id,
        order_id=order_id,
        user_id=user_id,
        total_cost=total_cost,
        protocol=protocol,
        status=status,
        decision=None,
        items_snapshot=items_snapshot,
        stock_held=False,
        stock_committed=False,
        stock_released=False,
        payment_held=False,
        payment_committed=False,
        payment_released=False,
        last_error=None,
        created_at=now,
        updated_at=now,
        retry_count=0,
    )
