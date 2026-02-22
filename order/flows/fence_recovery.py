from clients import is_ok_response, payment_internal_post, stock_internal_post
from config import (
    DECISION_COMMIT,
    STATUS_2PC_FINALIZATION_PENDING,
    STATUS_COMPLETED,
    STATUS_FAILED_NEEDS_RECOVERY,
)
from models import OrderValue
from store import (
    append_pair_once,
    clear_commit_fence,
    contains_pair,
    get_commit_fence_tx_id,
    get_tx,
    persist_order,
    try_save_tx,
)


def recover_fenced_2pc_commit(order_id: str, order_entry: OrderValue) -> str:
    tx_id = get_commit_fence_tx_id(order_id)
    if tx_id is None:
        return "cleared"

    tx_entry = get_tx(tx_id)
    if tx_entry is None or tx_entry.order_id != order_id or tx_entry.protocol != "2pc":
        clear_commit_fence(order_id)
        return "cleared"

    if tx_entry.decision != DECISION_COMMIT:
        clear_commit_fence(order_id)
        return "cleared"

    for item_id, quantity in tx_entry.items_snapshot:
        pair = (item_id, quantity)
        if contains_pair(tx_entry.stock_committed, pair):
            continue
        stock_commit = stock_internal_post(f"/internal/tx/commit_subtract/{tx_entry.tx_id}/{item_id}")
        if not is_ok_response(stock_commit):
            tx_entry.status = STATUS_FAILED_NEEDS_RECOVERY
            tx_entry.last_error = "Commit recovery blocked on stock participant"
            try_save_tx(tx_entry)
            return "blocked"
        append_pair_once(tx_entry.stock_committed, pair)
        try_save_tx(tx_entry)

    if not tx_entry.payment_committed:
        payment_commit = payment_internal_post(f"/internal/tx/commit_pay/{tx_entry.tx_id}")
        if not is_ok_response(payment_commit):
            tx_entry.status = STATUS_FAILED_NEEDS_RECOVERY
            tx_entry.last_error = "Commit recovery blocked on payment participant"
            try_save_tx(tx_entry)
            return "blocked"
        tx_entry.payment_committed = True
        try_save_tx(tx_entry)

    order_entry.paid = True
    if not persist_order(order_id, order_entry):
        tx_entry.status = STATUS_2PC_FINALIZATION_PENDING
        tx_entry.last_error = "Order DB write failed during fence recovery"
        try_save_tx(tx_entry)
        return "blocked"

    clear_commit_fence(order_id)
    tx_entry.status = STATUS_COMPLETED
    tx_entry.last_error = None
    try_save_tx(tx_entry)
    return "finalized"

