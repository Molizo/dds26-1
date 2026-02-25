import logging
import random
import threading

import redis
from msgspec import msgpack

from clients import is_ok_response, payment_internal_post, stock_internal_post
from common.redis_lock import acquire_lock, release_lock, renew_lock
from common.time_utils import now_ms
from config import (
    DECISION_ABORT,
    DECISION_COMMIT,
    ORDER_LOCK_TTL_SECONDS,
    RECOVERY_BATCH_SIZE,
    RECOVERY_LEADER_LOCK_KEY,
    RECOVERY_LEADER_LOCK_TTL_SECONDS,
    RECOVERY_SCAN_INTERVAL_SECONDS,
    RECOVERY_STALE_TX_GRACE_SECONDS,
    STATUS_2PC_ABORTING,
    STATUS_2PC_COMMITTING,
    STATUS_2PC_FINALIZATION_PENDING,
    STATUS_2PC_PREPARED,
    STATUS_2PC_PREPARING,
    STATUS_ABORTED,
    STATUS_COMPLETED,
    STATUS_FAILED_NEEDS_RECOVERY,
    STATUS_INIT,
    STATUS_SAGA_COMPENSATING,
    STATUS_SAGA_PAYMENT_CHARGED,
    STATUS_SAGA_STOCK_RESERVED,
    TX_KEY_PREFIX,
)
from flows.saga import compensate_saga
from models import CheckoutTxValue, OrderValue
from store import (
    active_tx_key,
    append_pair_once,
    commit_fence_key,
    contains_pair,
    db,
    lock_key,
    persist_order,
    try_save_tx,
)

_RECOVERY_LOGGER = logging.getLogger("order.recovery")
_RECOVERY_THREAD: threading.Thread | None = None
_RECOVERY_STOP_EVENT = threading.Event()
_ALWAYS_RECOVER_STATUSES = {
    STATUS_FAILED_NEEDS_RECOVERY,
    STATUS_2PC_FINALIZATION_PENDING,
}
_STALE_RECOVERABLE_STATUSES = {
    STATUS_INIT,
    STATUS_SAGA_STOCK_RESERVED,
    STATUS_SAGA_PAYMENT_CHARGED,
    STATUS_SAGA_COMPENSATING,
    STATUS_2PC_PREPARING,
    STATUS_2PC_PREPARED,
    STATUS_2PC_ABORTING,
    STATUS_2PC_COMMITTING,
}


def _is_fully_terminal(tx_entry: CheckoutTxValue) -> bool:
    return tx_entry.status in {STATUS_COMPLETED, STATUS_ABORTED}


def _is_stale(tx_entry: CheckoutTxValue, now_epoch_ms: int) -> bool:
    age_ms = max(0, now_epoch_ms - tx_entry.updated_at_ms)
    return age_ms >= (RECOVERY_STALE_TX_GRACE_SECONDS * 1000)


def _is_recovery_candidate(tx_entry: CheckoutTxValue, now_epoch_ms: int) -> bool:
    if tx_entry.status in _ALWAYS_RECOVER_STATUSES:
        return True
    if tx_entry.status in _STALE_RECOVERABLE_STATUSES:
        return _is_stale(tx_entry, now_epoch_ms)
    return False


def _save_tx_best_effort(tx_entry: CheckoutTxValue):
    try_save_tx(tx_entry)


def _set_failed(tx_entry: CheckoutTxValue, message: str):
    tx_entry.status = STATUS_FAILED_NEEDS_RECOVERY
    tx_entry.last_error = message
    _save_tx_best_effort(tx_entry)


def _fetch_order(order_id: str) -> OrderValue | None:
    try:
        entry = db.get(order_id)
    except redis.exceptions.RedisError:
        return None
    if entry is None:
        return None
    try:
        return msgpack.decode(entry, type=OrderValue)
    except Exception:
        return None


def _clear_active_tx_best_effort(order_id: str):
    try:
        db.delete(active_tx_key(order_id))
    except redis.exceptions.RedisError:
        pass


def _clear_commit_fence_best_effort(order_id: str):
    try:
        db.delete(commit_fence_key(order_id))
    except redis.exceptions.RedisError:
        pass


def _has_commit_fence(order_id: str) -> bool:
    try:
        return bool(db.exists(commit_fence_key(order_id)))
    except redis.exceptions.RedisError:
        return False


def _recover_saga_tx(tx_entry: CheckoutTxValue, order_entry: OrderValue | None):
    if order_entry is not None and order_entry.paid:
        tx_entry.status = STATUS_COMPLETED
        tx_entry.last_error = None
        _save_tx_best_effort(tx_entry)
        return

    tx_entry.status = STATUS_SAGA_COMPENSATING
    _save_tx_best_effort(tx_entry)
    try:
        compensated = compensate_saga(tx_entry)
    except Exception as exc:  # broad catch to keep worker alive
        _set_failed(tx_entry, f"Saga recovery failed: {exc}")
        return

    if compensated:
        tx_entry.status = STATUS_ABORTED
        tx_entry.last_error = None
        _save_tx_best_effort(tx_entry)
    else:
        _set_failed(tx_entry, "Saga recovery compensation incomplete")


def _recover_2pc_commit(tx_entry: CheckoutTxValue, order_entry: OrderValue | None):
    tx_entry.decision = DECISION_COMMIT
    tx_entry.status = STATUS_2PC_COMMITTING
    _save_tx_best_effort(tx_entry)

    try:
        db.set(commit_fence_key(tx_entry.order_id), tx_entry.tx_id)
    except redis.exceptions.RedisError:
        _set_failed(tx_entry, "Could not persist commit fence during recovery")
        return

    for item_id, quantity in tx_entry.items_snapshot:
        pair = (item_id, quantity)
        if contains_pair(tx_entry.stock_committed, pair):
            continue
        stock_commit = stock_internal_post(f"/internal/tx/commit_subtract/{tx_entry.tx_id}/{item_id}")
        if not is_ok_response(stock_commit):
            _set_failed(tx_entry, f"Commit recovery blocked on stock participant: {item_id}")
            return
        append_pair_once(tx_entry.stock_committed, pair)
        _save_tx_best_effort(tx_entry)

    if not tx_entry.payment_committed:
        payment_commit = payment_internal_post(f"/internal/tx/commit_pay/{tx_entry.tx_id}")
        if not is_ok_response(payment_commit):
            _set_failed(tx_entry, "Commit recovery blocked on payment participant")
            return
        tx_entry.payment_committed = True
        _save_tx_best_effort(tx_entry)

    if order_entry is None:
        _set_failed(tx_entry, "Order not found during commit recovery")
        return

    if not order_entry.paid:
        order_entry.paid = True
        if not persist_order(tx_entry.order_id, order_entry):
            tx_entry.status = STATUS_2PC_FINALIZATION_PENDING
            tx_entry.last_error = "Order DB write failed during commit recovery"
            _save_tx_best_effort(tx_entry)
            return

    _clear_commit_fence_best_effort(tx_entry.order_id)
    tx_entry.status = STATUS_COMPLETED
    tx_entry.last_error = None
    _save_tx_best_effort(tx_entry)


def _recover_2pc_abort(tx_entry: CheckoutTxValue):
    tx_entry.decision = DECISION_ABORT
    tx_entry.status = STATUS_2PC_ABORTING
    _save_tx_best_effort(tx_entry)

    all_aborted = True
    if tx_entry.payment_prepared and not tx_entry.payment_reversed:
        payment_abort = payment_internal_post(f"/internal/tx/abort_pay/{tx_entry.tx_id}")
        if is_ok_response(payment_abort):
            tx_entry.payment_reversed = True
            _save_tx_best_effort(tx_entry)
        else:
            all_aborted = False

    for item_id, _quantity in reversed(tx_entry.stock_prepared):
        pair = (item_id, _quantity)
        if contains_pair(tx_entry.stock_compensated, pair):
            continue
        stock_abort = stock_internal_post(f"/internal/tx/abort_subtract/{tx_entry.tx_id}/{item_id}")
        if is_ok_response(stock_abort):
            append_pair_once(tx_entry.stock_compensated, pair)
            _save_tx_best_effort(tx_entry)
        else:
            all_aborted = False

    if all_aborted:
        _clear_commit_fence_best_effort(tx_entry.order_id)
        tx_entry.status = STATUS_ABORTED
        tx_entry.last_error = None
        _save_tx_best_effort(tx_entry)
    else:
        _set_failed(tx_entry, "2PC recovery abort incomplete")


def recover_tx(tx_entry: CheckoutTxValue):
    order_entry = _fetch_order(tx_entry.order_id)
    if _is_fully_terminal(tx_entry):
        return

    if tx_entry.protocol == "saga":
        _recover_saga_tx(tx_entry, order_entry)
    elif tx_entry.protocol == "2pc":
        has_fence = _has_commit_fence(tx_entry.order_id)
        should_commit = (
            tx_entry.decision == DECISION_COMMIT
            or has_fence
            or tx_entry.status in {STATUS_2PC_COMMITTING, STATUS_2PC_FINALIZATION_PENDING}
        )
        if order_entry is not None and order_entry.paid:
            tx_entry.decision = DECISION_COMMIT
            tx_entry.status = STATUS_COMPLETED
            tx_entry.last_error = None
            _save_tx_best_effort(tx_entry)
            _clear_commit_fence_best_effort(tx_entry.order_id)
        elif should_commit:
            _recover_2pc_commit(tx_entry, order_entry)
        else:
            _recover_2pc_abort(tx_entry)
    else:
        _set_failed(tx_entry, f"Unknown protocol during recovery: {tx_entry.protocol}")

    if tx_entry.status in {
        STATUS_COMPLETED,
        STATUS_ABORTED,
        STATUS_2PC_FINALIZATION_PENDING,
    }:
        _clear_active_tx_best_effort(tx_entry.order_id)


def recover_tx_by_id(tx_id: str) -> str:
    try:
        encoded = db.get(f"{TX_KEY_PREFIX}{tx_id}")
    except redis.exceptions.RedisError:
        return "db_error"
    if encoded is None:
        return "not_found"
    try:
        tx_entry = msgpack.decode(encoded, type=CheckoutTxValue)
    except Exception:
        return "decode_error"
    _recover_tx_under_lock(tx_entry)
    try:
        refreshed = db.get(f"{TX_KEY_PREFIX}{tx_id}")
    except redis.exceptions.RedisError:
        return "db_error"
    if refreshed is None:
        return "not_found"
    try:
        refreshed_entry = msgpack.decode(refreshed, type=CheckoutTxValue)
    except Exception:
        return "decode_error"
    return refreshed_entry.status


def _iter_recoverable_txs(limit: int) -> list[CheckoutTxValue]:
    candidates: list[CheckoutTxValue] = []
    cursor = 0
    now_epoch_ms = now_ms()

    while len(candidates) < limit:
        try:
            cursor, keys = db.scan(cursor=cursor, match=f"{TX_KEY_PREFIX}*", count=limit)
        except redis.exceptions.RedisError:
            break

        for key in keys:
            try:
                encoded = db.get(key)
            except redis.exceptions.RedisError:
                continue
            if encoded is None:
                continue
            try:
                tx_entry = msgpack.decode(encoded, type=CheckoutTxValue)
            except Exception:
                continue
            if tx_entry.status in {STATUS_COMPLETED, STATUS_ABORTED}:
                continue
            if not _is_recovery_candidate(tx_entry, now_epoch_ms):
                continue
            candidates.append(tx_entry)
            if len(candidates) >= limit:
                break

        if cursor == 0:
            break

    return candidates


def _recover_tx_under_lock(tx_entry: CheckoutTxValue):
    lock_token = None
    try:
        lock_token = acquire_lock(db, lock_key(tx_entry.order_id), ORDER_LOCK_TTL_SECONDS)
    except redis.exceptions.RedisError:
        return
    if lock_token is None:
        return

    try:
        try:
            fresh_encoded = db.get(f"{TX_KEY_PREFIX}{tx_entry.tx_id}")
        except redis.exceptions.RedisError:
            return
        if fresh_encoded is None:
            return

        try:
            fresh_tx = msgpack.decode(fresh_encoded, type=CheckoutTxValue)
        except Exception:
            return
        if _is_fully_terminal(fresh_tx):
            return
        if not _is_recovery_candidate(fresh_tx, now_ms()):
            return
        recover_tx(fresh_tx)
    finally:
        try:
            release_lock(db, lock_key(tx_entry.order_id), lock_token)
        except redis.exceptions.RedisError:
            pass


def run_recovery_pass(limit: int = RECOVERY_BATCH_SIZE) -> int:
    candidates = _iter_recoverable_txs(limit)
    for tx_entry in candidates:
        _recover_tx_under_lock(tx_entry)
    return len(candidates)


def _recovery_worker_loop():
    leader_token: str | None = None
    while not _RECOVERY_STOP_EVENT.is_set():
        try:
            if leader_token is None:
                leader_token = acquire_lock(
                    db,
                    RECOVERY_LEADER_LOCK_KEY,
                    RECOVERY_LEADER_LOCK_TTL_SECONDS,
                )
            else:
                keep_leader = renew_lock(
                    db,
                    RECOVERY_LEADER_LOCK_KEY,
                    leader_token,
                    RECOVERY_LEADER_LOCK_TTL_SECONDS,
                )
                if not keep_leader:
                    leader_token = None

            if leader_token is not None:
                recovered = run_recovery_pass(RECOVERY_BATCH_SIZE)
                if recovered > 0:
                    _RECOVERY_LOGGER.debug("Recovery pass processed %d transactions", recovered)
        except redis.exceptions.RedisError as exc:
            _RECOVERY_LOGGER.warning("Recovery worker Redis error: %s", exc)
            leader_token = None
        except Exception as exc:  # broad catch keeps background recovery alive
            _RECOVERY_LOGGER.warning("Recovery worker error: %s", exc)

        sleep_for = RECOVERY_SCAN_INTERVAL_SECONDS + random.uniform(0.0, 0.5)
        _RECOVERY_STOP_EVENT.wait(sleep_for)

    if leader_token is not None:
        try:
            release_lock(db, RECOVERY_LEADER_LOCK_KEY, leader_token)
        except redis.exceptions.RedisError:
            pass


def start_recovery_worker():
    global _RECOVERY_THREAD
    if _RECOVERY_THREAD is not None and _RECOVERY_THREAD.is_alive():
        return
    _RECOVERY_STOP_EVENT.clear()
    _RECOVERY_THREAD = threading.Thread(
        target=_recovery_worker_loop,
        name="order-recovery-worker",
        daemon=True,
    )
    _RECOVERY_THREAD.start()


def stop_recovery_worker():
    _RECOVERY_STOP_EVENT.set()
