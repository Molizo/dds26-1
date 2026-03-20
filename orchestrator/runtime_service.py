import logging

import redis

from common.constants import (
    ACTIVE_TX_GUARD_TTL,
    ORDER_MUTATION_GUARD_TTL,
    ORCHESTRATOR_CMD_ACQUIRE_MUTATION_GUARD,
    ORCHESTRATOR_CMD_CHECKOUT,
    ORCHESTRATOR_CMD_RELEASE_MUTATION_GUARD,
    STATUS_ABORTED,
    STATUS_COMPLETED,
    TERMINAL_STATUSES,
)
from common.models import InternalCommand, InternalReply
from common.result import CheckoutResult
from coordinator.service import CoordinatorService
from order_port import RabbitMqOrderPort
import tx_store

DB_ERROR_STR = "DB error"


class RedisTxStoreAdapter:
    """Redis-backed TxStorePort owned by the orchestrator runtime."""

    def __init__(self, db: redis.Redis) -> None:
        self._db = db

    def create_tx(self, tx):
        tx_store.create_tx(self._db, tx)

    def update_tx(self, tx):
        tx_store.update_tx(self._db, tx)

    def get_tx(self, tx_id):
        return tx_store.get_tx(self._db, tx_id)

    def get_stale_non_terminal_txs(self, stale_before_ms, batch_limit=50):
        return tx_store.get_stale_non_terminal_txs(self._db, stale_before_ms, batch_limit)

    def set_decision(self, tx_id, decision):
        tx_store.set_decision(self._db, tx_id, decision)

    def set_decision_and_update_tx(self, tx_id, decision, tx):
        tx_store.set_decision_and_update_tx(self._db, tx_id, decision, tx)

    def get_decision(self, tx_id):
        return tx_store.get_decision(self._db, tx_id)

    def set_commit_fence(self, order_id, tx_id):
        tx_store.set_commit_fence(self._db, order_id, tx_id)

    def set_decision_fence_and_update_tx(self, tx_id, decision, order_id, tx):
        tx_store.set_decision_fence_and_update_tx(self._db, tx_id, decision, order_id, tx)

    def get_commit_fence(self, order_id):
        return tx_store.get_commit_fence(self._db, order_id)

    def clear_commit_fence(self, order_id):
        tx_store.clear_commit_fence(self._db, order_id)

    def acquire_active_tx_guard(self, order_id, tx_id, ttl):
        return tx_store.acquire_active_tx_guard(self._db, order_id, tx_id, ttl)

    def get_active_tx_guard(self, order_id):
        return tx_store.get_active_tx_guard(self._db, order_id)

    def clear_active_tx_guard(self, order_id):
        tx_store.clear_active_tx_guard(self._db, order_id)

    def clear_active_tx_guard_if_owned(self, order_id, tx_id):
        return tx_store.clear_active_tx_guard_if_owned(self._db, order_id, tx_id)

    def refresh_active_tx_guard(self, order_id, ttl):
        return tx_store.refresh_active_tx_guard(self._db, order_id, ttl)

    def acquire_mutation_guard(self, order_id, lease_id, ttl):
        return tx_store.acquire_mutation_guard(self._db, order_id, lease_id, ttl)

    def get_mutation_guard(self, order_id):
        return tx_store.get_mutation_guard(self._db, order_id)

    def release_mutation_guard(self, order_id, lease_id):
        return tx_store.release_mutation_guard(self._db, order_id, lease_id)

    def acquire_recovery_lock(self, tx_id, ttl):
        return tx_store.acquire_recovery_lock(self._db, tx_id, ttl)

    def release_recovery_lock(self, tx_id):
        tx_store.release_recovery_lock(self._db, tx_id)

    def acquire_recovery_leader(self, owner_id, ttl):
        return tx_store.acquire_recovery_leader(self._db, owner_id, ttl)

    def release_recovery_leader(self, owner_id):
        tx_store.release_recovery_leader(self._db, owner_id)


class OrchestratorRuntime:
    def __init__(
        self,
        db: redis.Redis,
        *,
        rabbitmq_url: str,
        checkout_protocol: str,
        logger: logging.Logger,
        order_port=None,
        tx_store_adapter=None,
        coordinator=None,
    ) -> None:
        self._db = db
        self._rabbitmq_url = rabbitmq_url
        self._checkout_protocol = checkout_protocol
        self._logger = logger
        self.order_port = order_port or RabbitMqOrderPort(rabbitmq_url)
        self.tx_store = tx_store_adapter or RedisTxStoreAdapter(db)
        self.coordinator = coordinator or CoordinatorService(
            rabbitmq_url=rabbitmq_url,
            order_port=self.order_port,
            tx_store=self.tx_store,
        )

    def get_decision(self, tx_id: str) -> str | None:
        return self.tx_store.get_decision(tx_id)

    def execute_checkout(self, order_id: str, tx_id: str) -> CheckoutResult:
        existing_tx = self.tx_store.get_tx(tx_id)
        if existing_tx is not None:
            return _result_for_existing_tx(existing_tx)

        acquired = self.tx_store.acquire_active_tx_guard(order_id, tx_id, ACTIVE_TX_GUARD_TTL)
        if not acquired:
            existing_tx_id = self.tx_store.get_active_tx_guard(order_id)
            if existing_tx_id and self._maybe_clear_terminal_active_guard(order_id, existing_tx_id):
                acquired = self.tx_store.acquire_active_tx_guard(order_id, tx_id, ACTIVE_TX_GUARD_TTL)
            if not acquired:
                return CheckoutResult.conflict()

        try:
            snapshot = self.order_port.read_order(order_id)
        except Exception:
            self._logger.exception("Order snapshot lookup failed order=%s tx=%s", order_id, tx_id)
            self._clear_active_tx_guard_if_owned(order_id, tx_id)
            return CheckoutResult.fail("order_read_failed")
        if snapshot is None:
            self._clear_active_tx_guard_if_owned(order_id, tx_id)
            return CheckoutResult.fail(f"Order: {order_id} not found!", code=400)
        if snapshot.paid:
            self._clear_active_tx_guard_if_owned(order_id, tx_id)
            return CheckoutResult.paid()

        result = self.coordinator.execute_checkout(snapshot, self._checkout_protocol, tx_id)

        tx_after = self.tx_store.get_tx(tx_id)
        if tx_after is not None and tx_after.status in TERMINAL_STATUSES:
            self._clear_active_tx_guard_if_owned(order_id, tx_id)

        return result

    def acquire_mutation_guard(self, order_id: str, lease_id: str) -> tuple[bool, str | None, int]:
        try:
            acquired = self.tx_store.acquire_mutation_guard(order_id, lease_id, ORDER_MUTATION_GUARD_TTL)
        except redis.exceptions.RedisError:
            return False, DB_ERROR_STR, 400

        if acquired:
            return True, None, 200

        existing_tx_id = self.tx_store.get_active_tx_guard(order_id)
        if existing_tx_id and self._maybe_clear_terminal_active_guard(order_id, existing_tx_id):
            try:
                reacquired = self.tx_store.acquire_mutation_guard(
                    order_id,
                    lease_id,
                    ORDER_MUTATION_GUARD_TTL,
                )
            except redis.exceptions.RedisError:
                return False, DB_ERROR_STR, 400
            if reacquired:
                return True, None, 200
            existing_tx_id = self.tx_store.get_active_tx_guard(order_id)

        if existing_tx_id:
            return False, "checkout_in_progress", 409
        if self.tx_store.get_mutation_guard(order_id):
            return False, "mutation_in_progress", 409
        return False, "mutation_in_progress", 409

    def release_mutation_guard(self, order_id: str, lease_id: str) -> bool:
        try:
            return bool(self.tx_store.release_mutation_guard(order_id, lease_id))
        except redis.exceptions.RedisError:
            return False

    def handle_internal_command(self, cmd: InternalCommand) -> InternalReply:
        if cmd.command == ORCHESTRATOR_CMD_CHECKOUT:
            if not cmd.tx_id:
                return InternalReply(
                    request_id=cmd.request_id,
                    command=cmd.command,
                    ok=False,
                    status_code=400,
                    error="tx_id is required",
                )
            result = self.execute_checkout(cmd.order_id, cmd.tx_id)
            return InternalReply(
                request_id=cmd.request_id,
                command=cmd.command,
                ok=result.success,
                status_code=result.status_code,
                error=result.error,
            )

        if cmd.command == ORCHESTRATOR_CMD_ACQUIRE_MUTATION_GUARD:
            if not cmd.lease_id:
                return InternalReply(
                    request_id=cmd.request_id,
                    command=cmd.command,
                    ok=False,
                    status_code=400,
                    error="lease_id is required",
                )
            acquired, reason, status_code = self.acquire_mutation_guard(cmd.order_id, cmd.lease_id)
            return InternalReply(
                request_id=cmd.request_id,
                command=cmd.command,
                ok=acquired,
                status_code=status_code,
                reason=reason,
                error=None if acquired else reason,
            )

        if cmd.command == ORCHESTRATOR_CMD_RELEASE_MUTATION_GUARD:
            if not cmd.lease_id:
                return InternalReply(
                    request_id=cmd.request_id,
                    command=cmd.command,
                    ok=False,
                    status_code=400,
                    error="lease_id is required",
                )
            released = self.release_mutation_guard(cmd.order_id, cmd.lease_id)
            return InternalReply(
                request_id=cmd.request_id,
                command=cmd.command,
                ok=released,
                status_code=200,
            )

        return InternalReply(
            request_id=cmd.request_id,
            command=cmd.command,
            ok=False,
            status_code=400,
            error="unknown_command",
        )

    def _maybe_clear_terminal_active_guard(self, order_id: str, tx_id: str) -> bool:
        existing_tx = self.tx_store.get_tx(tx_id)
        if existing_tx is None or existing_tx.status not in TERMINAL_STATUSES:
            return False
        return self._clear_active_tx_guard_if_owned(order_id, tx_id)

    def _clear_active_tx_guard_if_owned(self, order_id: str, tx_id: str) -> bool:
        try:
            return bool(self.tx_store.clear_active_tx_guard_if_owned(order_id, tx_id))
        except Exception:
            self._logger.exception(
                "Failed clearing active tx guard order=%s tx=%s",
                order_id,
                tx_id,
            )
            return False


def _result_for_existing_tx(existing_tx) -> CheckoutResult:
    if existing_tx.status == STATUS_COMPLETED:
        return CheckoutResult.ok()
    if existing_tx.status == STATUS_ABORTED:
        return CheckoutResult.fail(existing_tx.last_error or "Checkout failed")
    return CheckoutResult.conflict()
