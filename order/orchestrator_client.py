import logging
import time
import uuid
from dataclasses import dataclass

from common.constants import (
    ORCHESTRATOR_CMD_ACQUIRE_MUTATION_GUARD,
    ORCHESTRATOR_CMD_CHECKOUT,
    ORCHESTRATOR_CMD_RELEASE_MUTATION_GUARD,
    ORCHESTRATOR_COMMANDS_QUEUE,
)
from common.models import InternalCommand, InternalReply, decode_internal_reply, encode_internal_command
from common.rpc import rpc_request_bytes


@dataclass(frozen=True)
class MutationGuardAcquireResult:
    ok: bool
    lease_id: str | None
    status_code: int
    error: str | None


class OrchestratorClient:
    def __init__(
        self,
        rabbitmq_url: str,
        *,
        rpc_timeout_seconds: float,
        checkout_timeout_seconds: float,
        guard_retry_count: int,
        guard_retry_delay_seconds: float,
        logger: logging.Logger,
    ) -> None:
        self._rabbitmq_url = rabbitmq_url
        self._rpc_timeout_seconds = rpc_timeout_seconds
        self._checkout_timeout_seconds = checkout_timeout_seconds
        self._guard_retry_count = guard_retry_count
        self._guard_retry_delay_seconds = guard_retry_delay_seconds
        self._logger = logger

    def checkout(self, order_id: str, tx_id: str) -> InternalReply | None:
        return self._call(
            ORCHESTRATOR_CMD_CHECKOUT,
            order_id,
            tx_id=tx_id,
            timeout_seconds=self._checkout_timeout_seconds,
        )

    def acquire_mutation_guard(self, order_id: str) -> MutationGuardAcquireResult:
        lease_id = str(uuid.uuid4())

        for _ in range(self._guard_retry_count):
            try:
                reply = self._call(
                    ORCHESTRATOR_CMD_ACQUIRE_MUTATION_GUARD,
                    order_id,
                    lease_id=lease_id,
                    timeout_seconds=self._rpc_timeout_seconds,
                )
            except Exception as exc:
                self._logger.warning(
                    "Acquire mutation guard failed order=%s lease=%s: %s",
                    order_id,
                    lease_id,
                    exc,
                )
                time.sleep(self._guard_retry_delay_seconds)
                continue

            if reply is None:
                time.sleep(self._guard_retry_delay_seconds)
                continue
            if reply.ok and reply.status_code == 200:
                return MutationGuardAcquireResult(True, lease_id, 200, None)
            if reply.status_code == 409 and reply.reason in {"mutation_in_progress", "busy"}:
                time.sleep(self._guard_retry_delay_seconds)
                continue
            if reply.status_code == 409 and reply.reason == "checkout_in_progress":
                return MutationGuardAcquireResult(False, None, 409, "Checkout already in progress")
            return MutationGuardAcquireResult(
                False,
                None,
                int(reply.status_code or 400),
                reply.error or "Could not verify checkout state",
            )

        self.release_mutation_guard(order_id, lease_id)
        return MutationGuardAcquireResult(False, None, 400, "Could not verify checkout state")

    def release_mutation_guard(self, order_id: str, lease_id: str) -> None:
        try:
            self._call(
                ORCHESTRATOR_CMD_RELEASE_MUTATION_GUARD,
                order_id,
                lease_id=lease_id,
                timeout_seconds=self._rpc_timeout_seconds,
            )
        except Exception:
            self._logger.exception(
                "Failed releasing order mutation guard order=%s lease=%s",
                order_id,
                lease_id,
            )

    def _call(
        self,
        command: str,
        order_id: str,
        *,
        tx_id: str | None = None,
        lease_id: str | None = None,
        timeout_seconds: float,
    ) -> InternalReply | None:
        request_id = str(uuid.uuid4())
        payload = encode_internal_command(
            InternalCommand(
                request_id=request_id,
                command=command,
                order_id=order_id,
                tx_id=tx_id,
                lease_id=lease_id,
            )
        )
        reply = rpc_request_bytes(
            self._rabbitmq_url,
            ORCHESTRATOR_COMMANDS_QUEUE,
            request_id=request_id,
            body=payload,
            timeout_seconds=timeout_seconds,
        )
        if reply is None:
            return None
        return decode_internal_reply(reply)
