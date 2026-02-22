from flask import Response, abort

from clients import is_ok_response, payment_internal_post, stock_internal_post
from config import (
    DB_ERROR_STR,
    DECISION_ABORT,
    DECISION_COMMIT,
    REQ_ERROR_STR,
    STATUS_2PC_ABORTING,
    STATUS_2PC_COMMITTING,
    STATUS_2PC_FINALIZATION_PENDING,
    STATUS_2PC_PREPARED,
    STATUS_2PC_PREPARING,
    STATUS_ABORTED,
    STATUS_COMPLETED,
)
from models import CheckoutTxValue, OrderValue
from store import (
    append_pair_once,
    contains_pair,
    mark_tx_failed,
    persist_order,
    save_tx,
    set_commit_fence,
    try_save_tx,
)


def abort_2pc(tx_entry: CheckoutTxValue, user_message: str):
    tx_entry.decision = DECISION_ABORT
    tx_entry.status = STATUS_2PC_ABORTING
    tx_entry.last_error = user_message
    save_tx(tx_entry)

    all_aborted = True

    if tx_entry.payment_prepared and not tx_entry.payment_reversed:
        payment_abort = payment_internal_post(f"/internal/tx/abort_pay/{tx_entry.tx_id}")
        if is_ok_response(payment_abort):
            tx_entry.payment_reversed = True
            save_tx(tx_entry)
        else:
            all_aborted = False

    for item_id, quantity in reversed(tx_entry.stock_prepared):
        pair = (item_id, quantity)
        if contains_pair(tx_entry.stock_compensated, pair):
            continue
        stock_abort = stock_internal_post(f"/internal/tx/abort_subtract/{tx_entry.tx_id}/{item_id}")
        if is_ok_response(stock_abort):
            tx_entry.stock_compensated.append(pair)
            save_tx(tx_entry)
        else:
            all_aborted = False

    if all_aborted:
        tx_entry.status = STATUS_ABORTED
        tx_entry.last_error = user_message
        save_tx(tx_entry)
    else:
        mark_tx_failed(tx_entry, f"2PC abort incomplete: {user_message}")

    abort(400, user_message)


def run_2pc_checkout(order_id: str, order_entry: OrderValue, tx_entry: CheckoutTxValue) -> Response:
    tx_entry.status = STATUS_2PC_PREPARING
    save_tx(tx_entry)

    for item_id, quantity in tx_entry.items_snapshot:
        stock_prepare = stock_internal_post(f"/internal/tx/prepare_subtract/{tx_entry.tx_id}/{item_id}/{quantity}")
        if stock_prepare is None:
            append_pair_once(tx_entry.stock_prepared, (item_id, quantity))
            save_tx(tx_entry)
            abort_2pc(tx_entry, REQ_ERROR_STR)
        if stock_prepare.status_code != 200:
            abort_2pc(tx_entry, f"Out of stock on item_id: {item_id}")

        append_pair_once(tx_entry.stock_prepared, (item_id, quantity))
        save_tx(tx_entry)

    payment_prepare = payment_internal_post(
        f"/internal/tx/prepare_pay/{tx_entry.tx_id}/{order_entry.user_id}/{order_entry.total_cost}"
    )
    if payment_prepare is None:
        tx_entry.payment_prepared = True
        save_tx(tx_entry)
        abort_2pc(tx_entry, REQ_ERROR_STR)
    if payment_prepare.status_code != 200:
        abort_2pc(tx_entry, "User out of credit")

    tx_entry.payment_prepared = True
    tx_entry.status = STATUS_2PC_PREPARED
    tx_entry.last_error = None
    save_tx(tx_entry)

    tx_entry.decision = DECISION_COMMIT
    tx_entry.status = STATUS_2PC_COMMITTING
    save_tx(tx_entry)
    set_commit_fence(order_id, tx_entry.tx_id)

    for item_id, quantity in tx_entry.stock_prepared:
        stock_commit = stock_internal_post(f"/internal/tx/commit_subtract/{tx_entry.tx_id}/{item_id}")
        if stock_commit is None:
            mark_tx_failed(tx_entry, f"Stock commit request failed on item_id: {item_id}")
            abort(400, REQ_ERROR_STR)
        if stock_commit.status_code != 200:
            mark_tx_failed(tx_entry, f"Stock commit failed on item_id: {item_id}")
            abort(400, f"Stock commit failed on item_id: {item_id}")

        append_pair_once(tx_entry.stock_committed, (item_id, quantity))
        save_tx(tx_entry)

    payment_commit = payment_internal_post(f"/internal/tx/commit_pay/{tx_entry.tx_id}")
    if payment_commit is None:
        mark_tx_failed(tx_entry, "Payment commit request failed")
        abort(400, REQ_ERROR_STR)
    if payment_commit.status_code != 200:
        mark_tx_failed(tx_entry, "Payment commit failed")
        abort(400, "Payment commit failed")

    tx_entry.payment_committed = True
    save_tx(tx_entry)

    order_entry.paid = True
    if not persist_order(order_id, order_entry):
        tx_entry.status = STATUS_2PC_FINALIZATION_PENDING
        tx_entry.last_error = "Order DB write failed after 2PC commit"
        try_save_tx(tx_entry)
        abort(400, DB_ERROR_STR)

    from store import clear_commit_fence  # local to avoid import cycle

    clear_commit_fence(order_id)
    tx_entry.status = STATUS_COMPLETED
    tx_entry.last_error = None
    save_tx(tx_entry)
    return Response("Checkout successful", status=200)

