from flask import Blueprint

from service import (
    internal_abort_pay_handler,
    internal_commit_pay_handler,
    internal_prepare_pay_handler,
    internal_saga_pay_handler,
    internal_saga_refund_handler,
)

internal_bp = Blueprint("payment_internal", __name__)


@internal_bp.post("/internal/tx/saga/pay/<tx_id>/<user_id>/<amount>")
def internal_saga_pay(tx_id: str, user_id: str, amount: int):
    return internal_saga_pay_handler(tx_id, user_id, amount)


@internal_bp.post("/internal/tx/saga/refund/<tx_id>")
def internal_saga_refund(tx_id: str):
    return internal_saga_refund_handler(tx_id)


@internal_bp.post("/internal/tx/prepare_pay/<tx_id>/<user_id>/<amount>")
def internal_prepare_pay(tx_id: str, user_id: str, amount: int):
    return internal_prepare_pay_handler(tx_id, user_id, amount)


@internal_bp.post("/internal/tx/commit_pay/<tx_id>")
def internal_commit_pay(tx_id: str):
    return internal_commit_pay_handler(tx_id)


@internal_bp.post("/internal/tx/abort_pay/<tx_id>")
def internal_abort_pay(tx_id: str):
    return internal_abort_pay_handler(tx_id)

