from dataclasses import dataclass
from typing import Callable

from shared_messaging.redis_atomic import (
    ATOMIC_APPLIED,
    ATOMIC_BUSINESS_REJECT,
    ATOMIC_DUPLICATE,
    ATOMIC_MISSING_ENTITY,
    AtomicResult,
)

PROCESS_ACTION_ACK = "ack"
PROCESS_ACTION_RETRY = "retry"

PROCESS_STATUS_APPLIED = "applied"
PROCESS_STATUS_DUPLICATE = "duplicate"
PROCESS_STATUS_BUSINESS_REJECT = "business_reject"
PROCESS_STATUS_INFRA_ERROR = "infra_error"


@dataclass(frozen=True)
class ProcessOutcome:
    action: str
    status: str
    reason: str | None = None


def process_idempotent_step(
    *,
    apply_effect: Callable[[], AtomicResult],
    on_applied: Callable[[], None] | None = None,
    on_duplicate: Callable[[], None] | None = None,
    on_rejected: Callable[[AtomicResult], None] | None = None,
) -> ProcessOutcome:
    try:
        result = apply_effect()
    except Exception as exc:
        return ProcessOutcome(
            action=PROCESS_ACTION_RETRY,
            status=PROCESS_STATUS_INFRA_ERROR,
            reason=str(exc),
        )

    try:
        if result.status == ATOMIC_APPLIED:
            if on_applied is not None:
                on_applied()
            return ProcessOutcome(action=PROCESS_ACTION_ACK, status=PROCESS_STATUS_APPLIED)

        if result.status == ATOMIC_DUPLICATE:
            if on_duplicate is not None:
                on_duplicate()
            return ProcessOutcome(action=PROCESS_ACTION_ACK, status=PROCESS_STATUS_DUPLICATE)

        if result.status in (ATOMIC_BUSINESS_REJECT, ATOMIC_MISSING_ENTITY):
            if on_rejected is not None:
                on_rejected(result)
            return ProcessOutcome(
                action=PROCESS_ACTION_ACK,
                status=PROCESS_STATUS_BUSINESS_REJECT,
                reason=result.status,
            )
    except Exception as exc:
        return ProcessOutcome(
            action=PROCESS_ACTION_RETRY,
            status=PROCESS_STATUS_INFRA_ERROR,
            reason=str(exc),
        )

    return ProcessOutcome(
        action=PROCESS_ACTION_RETRY,
        status=PROCESS_STATUS_INFRA_ERROR,
        reason=f"unknown atomic result status: {result.status}",
    )
