from flask import Response, abort

from clients import is_ok_response, payment_internal_post, stock_internal_post
from config import (
    DB_ERROR_STR,
    STATUS_ABORTED,
    STATUS_COMPLETED,
    STATUS_SAGA_COMPENSATING,
    STATUS_SAGA_PAYMENT_CHARGED,
    STATUS_SAGA_STOCK_RESERVED,
)
from models import CheckoutTxValue, OrderValue
from store import (
    append_pair_once,
    contains_pair,
    mark_tx_failed,
    persist_order,
    save_tx,
)


def compensate_saga(tx_entry: CheckoutTxValue) -> bool:
    all_compensated = True

    if tx_entry.payment_committed and not tx_entry.payment_reversed:
        payment_refund = payment_internal_post(f"/internal/tx/saga/refund/{tx_entry.tx_id}")
        if is_ok_response(payment_refund):
            tx_entry.payment_reversed = True
            save_tx(tx_entry)
        else:
            all_compensated = False

    for item_id, quantity in reversed(tx_entry.stock_prepared):
        pair = (item_id, quantity)
        if contains_pair(tx_entry.stock_compensated, pair):
            continue
        stock_release = stock_internal_post(f"/internal/tx/saga/release/{tx_entry.tx_id}/{item_id}")
        if is_ok_response(stock_release):
            tx_entry.stock_compensated.append(pair)
            save_tx(tx_entry)
        else:
            all_compensated = False

    return all_compensated


def run_saga_checkout(order_id: str, order_entry: OrderValue, tx_entry: CheckoutTxValue) -> Response:
    for item_id, quantity in tx_entry.items_snapshot:
        stock_reserve = stock_internal_post(f"/internal/tx/saga/reserve/{tx_entry.tx_id}/{item_id}/{quantity}")
        if not is_ok_response(stock_reserve):
            tx_entry.status = STATUS_SAGA_COMPENSATING
            tx_entry.last_error = f"Out of stock on item_id: {item_id}"
            save_tx(tx_entry)
            if compensate_saga(tx_entry):
                tx_entry.status = STATUS_ABORTED
                save_tx(tx_entry)
            else:
                mark_tx_failed(tx_entry, f"Saga stock compensation incomplete for item: {item_id}")
            abort(400, f"Out of stock on item_id: {item_id}")

        pair = (item_id, quantity)
        append_pair_once(tx_entry.stock_prepared, pair)
        append_pair_once(tx_entry.stock_committed, pair)
        tx_entry.status = STATUS_SAGA_STOCK_RESERVED
        tx_entry.last_error = None
        save_tx(tx_entry)

    payment_pay = payment_internal_post(
        f"/internal/tx/saga/pay/{tx_entry.tx_id}/{order_entry.user_id}/{order_entry.total_cost}"
    )
    if not is_ok_response(payment_pay):
        tx_entry.status = STATUS_SAGA_COMPENSATING
        tx_entry.last_error = "User out of credit"
        save_tx(tx_entry)
        if compensate_saga(tx_entry):
            tx_entry.status = STATUS_ABORTED
            save_tx(tx_entry)
        else:
            mark_tx_failed(tx_entry, "Saga compensation incomplete after payment failure")
        abort(400, "User out of credit")

    tx_entry.payment_committed = True
    tx_entry.status = STATUS_SAGA_PAYMENT_CHARGED
    tx_entry.last_error = None
    save_tx(tx_entry)

    order_entry.paid = True
    if not persist_order(order_id, order_entry):
        tx_entry.status = STATUS_SAGA_COMPENSATING
        tx_entry.last_error = "Order DB write failed while marking order paid"
        save_tx(tx_entry)
        if compensate_saga(tx_entry):
            tx_entry.status = STATUS_ABORTED
            save_tx(tx_entry)
        else:
            mark_tx_failed(tx_entry, "Saga compensation incomplete after order DB failure")
        abort(400, DB_ERROR_STR)

    tx_entry.status = STATUS_COMPLETED
    tx_entry.last_error = None
    save_tx(tx_entry)
    return Response("Checkout successful", status=200)

