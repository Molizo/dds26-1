"""Coordinator service: execute_checkout and resume_transaction.

Orchestrates SAGA and 2PC checkout protocols using RabbitMQ for parallel
fan-out to stock and payment participants. Protocol logic is inline (not
split into separate protocol classes) to keep the control flow readable.

This module must NOT import Flask — it is transport-agnostic and extractable.
"""
import logging
import uuid
from collections import defaultdict
from typing import Optional

from common.constants import (
    CMD_HOLD, CMD_RELEASE, CMD_COMMIT,
    SVC_STOCK, SVC_PAYMENT,
    STOCK_COMMANDS_QUEUE, PAYMENT_COMMANDS_QUEUE,
    STATUS_INIT, STATUS_HOLDING, STATUS_HELD,
    STATUS_COMMITTING, STATUS_COMPENSATING,
    STATUS_COMPLETED, STATUS_ABORTED,
    STATUS_FAILED_NEEDS_RECOVERY,
    PROTOCOL_SAGA, PROTOCOL_2PC,
    PARTICIPANT_REPLY_TIMEOUT, ACTIVE_TX_GUARD_TTL,
    TERMINAL_STATUSES,
)
from common.models import (
    ParticipantCommand, ParticipantReply,
    StockHoldPayload, PaymentHoldPayload,
    encode_command,
)
from common.messaging import publish_command
from common.result import CheckoutResult
from coordinator.models import CheckoutTxValue, make_tx
from coordinator.messaging import (
    get_reply_queue, register_pending, wait_for_replies, cancel_pending,
)
from coordinator.ports import OrderPort, TxStorePort, OrderSnapshot

logger = logging.getLogger(__name__)


class CoordinatorService:
    """Stateless coordinator — all state lives in TxStorePort."""

    def __init__(
        self,
        rabbitmq_url: str,
        order_port: OrderPort,
        tx_store: TxStorePort,
    ):
        self._rabbitmq_url = rabbitmq_url
        self._orders = order_port
        self._tx = tx_store

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def execute_checkout(
        self,
        order_id: str,
        protocol: str,
        tx_id: Optional[str] = None,
    ) -> CheckoutResult:
        """Run a full checkout transaction for the given order.

        The caller (route layer) is responsible for:
        - validating the order exists
        - checking the already-paid fast path
        - acquiring the active-tx guard
        - clearing the guard on terminal result
        """
        snapshot = self._orders.read_order(order_id)
        if snapshot is None:
            return CheckoutResult.fail("Order not found", code=400)

        if snapshot.paid:
            return CheckoutResult.paid()

        # Aggregate items: merge duplicate item_ids
        items = _aggregate_items(snapshot.items)

        if tx_id is None:
            tx_id = str(uuid.uuid4())
        tx = make_tx(
            tx_id=tx_id,
            order_id=order_id,
            user_id=snapshot.user_id,
            total_cost=snapshot.total_cost,
            protocol=protocol,
            items_snapshot=items,
            status=STATUS_INIT,
        )
        self._tx.create_tx(tx)

        if protocol == PROTOCOL_SAGA:
            return self._run_saga(tx)
        else:
            return self._run_2pc(tx)

    def resume_transaction(self, tx: CheckoutTxValue) -> CheckoutResult:
        """Resume a non-terminal transaction (called by recovery worker)."""
        if tx.protocol == PROTOCOL_SAGA:
            return self._resume_saga(tx)
        else:
            return self._resume_2pc(tx)

    # ------------------------------------------------------------------
    # SAGA protocol
    # ------------------------------------------------------------------

    def _run_saga(self, tx: CheckoutTxValue) -> CheckoutResult:
        """SAGA: parallel hold → commit or compensate."""
        # --- HOLDING phase: publish hold commands in parallel ---
        tx.status = STATUS_HOLDING
        self._tx.update_tx(tx)

        stock_replies, payment_replies = self._publish_and_wait_holds(tx)
        stock_reply = _find_reply(stock_replies, SVC_STOCK)
        payment_reply = _find_reply(payment_replies, SVC_PAYMENT)

        # Update flags from replies — batch into one persist
        if stock_reply and stock_reply.ok:
            tx.stock_held = True
        if payment_reply and payment_reply.ok:
            tx.payment_held = True

        # --- Decision ---
        if tx.stock_held and tx.payment_held:
            tx.status = STATUS_HELD
            self._tx.update_tx(tx)
            return self._saga_commit(tx)
        else:
            # At least one failed or timed out — compensate
            # held flags + error will be persisted by _saga_compensate's update_tx
            tx.last_error = _hold_error(stock_reply, payment_reply)
            return self._saga_compensate(tx)

    def _saga_commit(self, tx: CheckoutTxValue) -> CheckoutResult:
        """SAGA commit: mark order paid, then send commit commands."""
        # Mark order paid (idempotent) before entering COMMITTING
        if not self._orders.mark_paid(tx.order_id, tx.tx_id):
            tx.last_error = "mark_paid_failed"
            return self._saga_compensate(tx)

        # Persist decision + fence + status, using combined TxStore call
        # when available and a legacy-compatible fallback otherwise.
        tx.status = STATUS_COMMITTING
        tx.decision = "commit"
        self._persist_decision_fence_and_update_tx(tx.tx_id, "commit", tx.order_id, tx)

        # Publish commit commands to finalize participant tx records.
        # Order is already paid; if commits are incomplete, return ok and let
        # recovery retry the commits.
        if self._publish_commits(tx):
            return self._finalize_completed(tx)
        return CheckoutResult.ok()  # order IS paid; recovery will confirm commits

    def _saga_compensate(self, tx: CheckoutTxValue) -> CheckoutResult:
        """SAGA compensate: release all participants that may have succeeded."""
        tx.status = STATUS_COMPENSATING
        tx.decision = "abort"
        self._persist_decision_and_update_tx(tx.tx_id, "abort", tx)

        return self._publish_releases(tx)

    def _resume_saga(self, tx: CheckoutTxValue) -> CheckoutResult:
        """Resume a stale SAGA transaction."""
        logger.info("Resuming SAGA tx=%s status=%s stock_held=%s payment_held=%s",
                     tx.tx_id, tx.status, tx.stock_held, tx.payment_held)
        tx.retry_count += 1

        # Check if order is already paid (crash after mark_paid but before COMMITTING)
        snapshot = self._orders.read_order(tx.order_id)
        order_paid = snapshot.paid if snapshot else False

        if tx.status == STATUS_COMMITTING or order_paid:
            # Forward recovery: order is paid, just need to finalize commits
            if not order_paid:
                if not self._orders.mark_paid(tx.order_id, tx.tx_id):
                    tx.status = STATUS_FAILED_NEEDS_RECOVERY
                    tx.last_error = "mark_paid_failed"
                    self._tx.update_tx(tx)
                    return CheckoutResult.fail("mark_paid_failed")
            if tx.decision != "commit":
                tx.decision = "commit"
                self._tx.set_decision(tx.tx_id, "commit")
            tx.status = STATUS_COMMITTING
            self._tx.update_tx(tx)
            if self._publish_commits(tx):
                return self._finalize_completed(tx)
            return CheckoutResult.ok()

        if tx.status == STATUS_COMPENSATING:
            return self._publish_releases(tx)

        # INIT, HOLDING, HELD, FAILED_NEEDS_RECOVERY
        if tx.stock_held and tx.payment_held:
            # Both held — complete forward
            tx.status = STATUS_HELD
            self._tx.update_tx(tx)
            return self._saga_commit(tx)
        else:
            # Partial or no holds — compensate
            return self._saga_compensate(tx)

    # ------------------------------------------------------------------
    # 2PC protocol
    # ------------------------------------------------------------------

    def _run_2pc(self, tx: CheckoutTxValue) -> CheckoutResult:
        """2PC: parallel prepare → decision → parallel commit/abort."""
        # --- HOLDING (prepare) phase ---
        tx.status = STATUS_HOLDING
        self._tx.update_tx(tx)

        stock_replies, payment_replies = self._publish_and_wait_holds(tx)
        stock_reply = _find_reply(stock_replies, SVC_STOCK)
        payment_reply = _find_reply(payment_replies, SVC_PAYMENT)

        if stock_reply and stock_reply.ok:
            tx.stock_held = True
        if payment_reply and payment_reply.ok:
            tx.payment_held = True

        if tx.stock_held and tx.payment_held:
            tx.status = STATUS_HELD
            self._tx.update_tx(tx)
            return self._2pc_commit(tx)
        else:
            # held flags + error will be persisted by _2pc_abort's update_tx
            tx.last_error = _hold_error(stock_reply, payment_reply)
            return self._2pc_abort(tx)

    def _2pc_commit(self, tx: CheckoutTxValue) -> CheckoutResult:
        """2PC commit: persist decision FIRST, then commits, then mark paid.

        Write ordering:
          1. Persist decision + commit fence + status → COMMITTING
             (combined call when supported, legacy sequence otherwise)
          2. publish commit commands + wait for confirmations
          3. mark_paid   ← must be durable before COMPLETED
          4. _finalize_completed (status → COMPLETED, clear fence)

        Crashing between steps 2–3 or 3–4 is safe: recovery finds the
        commit decision/fence, re-publishes commits, marks paid, and
        finalizes. The order is NOT considered paid until step 3 succeeds,
        so no other checkout can start (the guard is still held).
        """
        tx.status = STATUS_COMMITTING
        tx.decision = "commit"
        self._persist_decision_fence_and_update_tx(tx.tx_id, "commit", tx.order_id, tx)

        if not self._publish_commits(tx):
            # Commits incomplete — FAILED_NEEDS_RECOVERY already set.
            # Order is NOT paid; recovery will re-publish commits.
            return CheckoutResult.fail("commit_confirmation_incomplete")

        # Commits confirmed — now mark order paid, then finalize.
        if not self._orders.mark_paid(tx.order_id, tx.tx_id):
            tx.status = STATUS_FAILED_NEEDS_RECOVERY
            tx.last_error = "mark_paid_failed"
            self._tx.update_tx(tx)
            return CheckoutResult.fail("mark_paid_failed")
        return self._finalize_completed(tx)

    def _2pc_abort(self, tx: CheckoutTxValue) -> CheckoutResult:
        """2PC abort: presumed abort — release all participants."""
        tx.status = STATUS_COMPENSATING
        tx.decision = "abort"
        self._persist_decision_and_update_tx(tx.tx_id, "abort", tx)

        return self._publish_releases(tx)

    def _resume_2pc(self, tx: CheckoutTxValue) -> CheckoutResult:
        """Resume a stale 2PC transaction."""
        logger.info("Resuming 2PC tx=%s status=%s decision=%s",
                     tx.tx_id, tx.status, tx.decision)
        tx.retry_count += 1

        # Check for existing decision
        decision = self._tx.get_decision(tx.tx_id)

        # Check commit fence
        fence = self._tx.get_commit_fence(tx.order_id)
        if fence == tx.tx_id:
            decision = "commit"

        # Check if order already paid
        snapshot = self._orders.read_order(tx.order_id)
        order_paid = snapshot.paid if snapshot else False
        if order_paid:
            decision = "commit"

        if decision == "commit":
            if tx.decision != "commit":
                tx.decision = "commit"
                self._tx.set_decision(tx.tx_id, "commit")
            tx.status = STATUS_COMMITTING
            self._tx.update_tx(tx)
            if not self._publish_commits(tx):
                return CheckoutResult.fail("commit_confirmation_incomplete")
            # Commits confirmed — mark paid (idempotent) then finalize
            if not order_paid:
                if not self._orders.mark_paid(tx.order_id, tx.tx_id):
                    tx.status = STATUS_FAILED_NEEDS_RECOVERY
                    tx.last_error = "mark_paid_failed"
                    self._tx.update_tx(tx)
                    return CheckoutResult.fail("mark_paid_failed")
            return self._finalize_completed(tx)

        # No commit decision → presumed abort
        if tx.status == STATUS_COMPENSATING:
            return self._publish_releases(tx)

        return self._2pc_abort(tx)

    # ------------------------------------------------------------------
    # Tx persistence compatibility helpers
    # ------------------------------------------------------------------

    def _persist_decision_and_update_tx(
        self,
        tx_id: str,
        decision: str,
        tx: CheckoutTxValue,
    ) -> None:
        """Use combined decision+update call when available, else fallback."""
        combined = getattr(self._tx, "set_decision_and_update_tx", None)
        if callable(combined):
            combined(tx_id, decision, tx)
            return

        # Backward-compatible sequence for older TxStore implementations.
        self._tx.set_decision(tx_id, decision)
        self._tx.update_tx(tx)

    def _persist_decision_fence_and_update_tx(
        self,
        tx_id: str,
        decision: str,
        order_id: str,
        tx: CheckoutTxValue,
    ) -> None:
        """Use combined decision+fence+update call when available, else fallback."""
        combined = getattr(self._tx, "set_decision_fence_and_update_tx", None)
        if callable(combined):
            combined(tx_id, decision, order_id, tx)
            return

        # Backward-compatible sequence for older TxStore implementations.
        self._tx.set_decision(tx_id, decision)
        self._tx.set_commit_fence(order_id, tx_id)
        self._tx.update_tx(tx)

    # ------------------------------------------------------------------
    # Shared mechanics: publish and wait
    # ------------------------------------------------------------------

    def _publish_and_wait_holds(
        self, tx: CheckoutTxValue
    ) -> tuple[list[ParticipantReply], list[ParticipantReply]]:
        """Publish hold commands to stock+payment in parallel, wait for replies.

        Returns (all_replies_for_stock_filtering, all_replies_for_payment_filtering).
        Both are from the same reply list, just the full list twice for convenience.
        """
        reply_queue = get_reply_queue()
        register_pending(tx.tx_id, expected_command=CMD_HOLD,
                         expected_services=frozenset({SVC_STOCK, SVC_PAYMENT}))

        try:
            # Build commands
            stock_cmd = ParticipantCommand(
                tx_id=tx.tx_id,
                command=CMD_HOLD,
                reply_to=reply_queue,
                stock_payload=StockHoldPayload(items=tx.items_snapshot),
            )
            payment_cmd = ParticipantCommand(
                tx_id=tx.tx_id,
                command=CMD_HOLD,
                reply_to=reply_queue,
                payment_payload=PaymentHoldPayload(
                    user_id=tx.user_id,
                    amount=tx.total_cost,
                ),
            )

            # Publish both (thread-local connections, so sequential is fine)
            publish_command(self._rabbitmq_url, STOCK_COMMANDS_QUEUE,
                           encode_command(stock_cmd), reply_queue)
            publish_command(self._rabbitmq_url, PAYMENT_COMMANDS_QUEUE,
                           encode_command(payment_cmd), reply_queue)

            # Wait for replies
            replies = wait_for_replies(tx.tx_id, timeout=PARTICIPANT_REPLY_TIMEOUT)
            return replies, replies

        except Exception as exc:
            logger.error("Error publishing hold commands tx=%s: %s", tx.tx_id, exc)
            cancel_pending(tx.tx_id)
            return [], []

    def _publish_commits(self, tx: CheckoutTxValue) -> bool:
        """Publish commit commands and wait for confirmations.

        Updates participant flags and persists the tx record, but does NOT
        finalize (no status change to COMPLETED, no fence clear, no mark_paid).
        Returns True if all needed commits were confirmed, False otherwise.
        Callers are responsible for the correct finalization sequence.
        """
        # Determine which participants still need commits
        need_stock = not tx.stock_committed
        need_payment = not tx.payment_committed
        expected_services = frozenset(
            ({SVC_STOCK} if need_stock else set()) |
            ({SVC_PAYMENT} if need_payment else set())
        )

        if not expected_services:
            return True  # already done

        reply_queue = get_reply_queue()
        register_pending(tx.tx_id, expected_command=CMD_COMMIT,
                         expected_services=expected_services)

        try:
            if need_stock:
                cmd = ParticipantCommand(
                    tx_id=tx.tx_id, command=CMD_COMMIT, reply_to=reply_queue,
                    stock_payload=StockHoldPayload(items=tx.items_snapshot),
                )
                publish_command(self._rabbitmq_url, STOCK_COMMANDS_QUEUE,
                               encode_command(cmd), reply_queue)

            if need_payment:
                cmd = ParticipantCommand(
                    tx_id=tx.tx_id, command=CMD_COMMIT, reply_to=reply_queue,
                    payment_payload=PaymentHoldPayload(
                        user_id=tx.user_id, amount=tx.total_cost),
                )
                publish_command(self._rabbitmq_url, PAYMENT_COMMANDS_QUEUE,
                               encode_command(cmd), reply_queue)

            replies = wait_for_replies(tx.tx_id, timeout=PARTICIPANT_REPLY_TIMEOUT)

        except Exception as exc:
            logger.error("Error publishing commit commands tx=%s: %s", tx.tx_id, exc)
            cancel_pending(tx.tx_id)
            replies = []

        for r in replies:
            if r.service == SVC_STOCK and r.ok:
                tx.stock_committed = True
            elif r.service == SVC_PAYMENT and r.ok:
                tx.payment_committed = True
        self._tx.update_tx(tx)

        if not (tx.stock_committed and tx.payment_committed):
            tx.status = STATUS_FAILED_NEEDS_RECOVERY
            tx.last_error = "commit_confirmation_incomplete"
            self._tx.update_tx(tx)
            return False

        return True

    def _publish_releases(self, tx: CheckoutTxValue) -> CheckoutResult:
        """Publish release/refund commands and wait for confirmations."""
        reply_queue = get_reply_queue()

        # Release all participants that may have succeeded (including timed-out ones)
        need_stock = not tx.stock_released
        need_payment = not tx.payment_released
        expected_services = frozenset(
            ({SVC_STOCK} if need_stock else set()) |
            ({SVC_PAYMENT} if need_payment else set())
        )

        if not expected_services:
            return self._finalize_aborted(tx)

        register_pending(tx.tx_id, expected_command=CMD_RELEASE,
                         expected_services=expected_services)

        try:
            if need_stock:
                cmd = ParticipantCommand(
                    tx_id=tx.tx_id, command=CMD_RELEASE, reply_to=reply_queue,
                    stock_payload=StockHoldPayload(items=tx.items_snapshot),
                )
                publish_command(self._rabbitmq_url, STOCK_COMMANDS_QUEUE,
                               encode_command(cmd), reply_queue)

            if need_payment:
                cmd = ParticipantCommand(
                    tx_id=tx.tx_id, command=CMD_RELEASE, reply_to=reply_queue,
                    payment_payload=PaymentHoldPayload(
                        user_id=tx.user_id, amount=tx.total_cost),
                )
                publish_command(self._rabbitmq_url, PAYMENT_COMMANDS_QUEUE,
                               encode_command(cmd), reply_queue)

            replies = wait_for_replies(tx.tx_id, timeout=PARTICIPANT_REPLY_TIMEOUT)

        except Exception as exc:
            logger.error("Error publishing release commands tx=%s: %s", tx.tx_id, exc)
            cancel_pending(tx.tx_id)
            replies = []

        for r in replies:
            if r.service == SVC_STOCK and r.ok:
                tx.stock_released = True
            elif r.service == SVC_PAYMENT and r.ok:
                tx.payment_released = True
        self._tx.update_tx(tx)

        if tx.stock_released and tx.payment_released:
            return self._finalize_aborted(tx)
        else:
            tx.status = STATUS_FAILED_NEEDS_RECOVERY
            tx.last_error = "release_confirmation_incomplete"
            self._tx.update_tx(tx)
            return CheckoutResult.fail(tx.last_error or "abort_incomplete")

    # ------------------------------------------------------------------
    # Terminal state transitions
    # ------------------------------------------------------------------

    def _finalize_completed(self, tx: CheckoutTxValue) -> CheckoutResult:
        tx.status = STATUS_COMPLETED
        self._tx.update_tx(tx)
        self._tx.clear_commit_fence(tx.order_id)
        logger.info("Tx %s COMPLETED", tx.tx_id)
        return CheckoutResult.ok()

    def _finalize_aborted(self, tx: CheckoutTxValue) -> CheckoutResult:
        tx.status = STATUS_ABORTED
        self._tx.update_tx(tx)
        logger.info("Tx %s ABORTED", tx.tx_id)
        return CheckoutResult.fail(tx.last_error or "aborted")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _aggregate_items(items: list[tuple[str, int]]) -> list[tuple[str, int]]:
    """Merge duplicate item_ids into aggregated (item_id, total_qty) pairs."""
    agg: dict[str, int] = defaultdict(int)
    for item_id, qty in items:
        agg[item_id] += qty
    return sorted(agg.items())  # sorted for determinism


def _find_reply(
    replies: list[ParticipantReply], service: str
) -> Optional[ParticipantReply]:
    """Find the first reply from a given service in a reply list."""
    for r in replies:
        if r.service == service:
            return r
    return None


def _hold_error(
    stock_reply: Optional[ParticipantReply],
    payment_reply: Optional[ParticipantReply],
) -> str:
    """Build a human-readable error from hold replies."""
    parts = []
    if stock_reply is None:
        parts.append("stock_timeout")
    elif not stock_reply.ok:
        parts.append(f"stock:{stock_reply.error}")
    if payment_reply is None:
        parts.append("payment_timeout")
    elif not payment_reply.ok:
        parts.append(f"payment:{payment_reply.error}")
    return "; ".join(parts) if parts else "unknown_hold_failure"
