from flask import Blueprint

from service import (
    internal_abort_subtract_handler,
    internal_commit_subtract_handler,
    internal_prepare_subtract_handler,
    internal_saga_release_handler,
    internal_saga_reserve_handler,
)

internal_bp = Blueprint("stock_internal", __name__)


@internal_bp.post("/internal/tx/saga/reserve/<tx_id>/<item_id>/<amount>")
def internal_saga_reserve(tx_id: str, item_id: str, amount: int):
    return internal_saga_reserve_handler(tx_id, item_id, amount)


@internal_bp.post("/internal/tx/saga/release/<tx_id>/<item_id>")
def internal_saga_release(tx_id: str, item_id: str):
    return internal_saga_release_handler(tx_id, item_id)


@internal_bp.post("/internal/tx/prepare_subtract/<tx_id>/<item_id>/<amount>")
def internal_prepare_subtract(tx_id: str, item_id: str, amount: int):
    return internal_prepare_subtract_handler(tx_id, item_id, amount)


@internal_bp.post("/internal/tx/commit_subtract/<tx_id>/<item_id>")
def internal_commit_subtract(tx_id: str, item_id: str):
    return internal_commit_subtract_handler(tx_id, item_id)


@internal_bp.post("/internal/tx/abort_subtract/<tx_id>/<item_id>")
def internal_abort_subtract(tx_id: str, item_id: str):
    return internal_abort_subtract_handler(tx_id, item_id)

