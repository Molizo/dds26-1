"""Redis key helpers shared across services/workers."""

from typing import Final

IDEMPOTENCY_NAMESPACE_VERSION: Final[str] = "v1"
INBOX_TTL_SEC: Final[int] = 7 * 24 * 60 * 60
EFFECT_TTL_SEC: Final[int] = 30 * 24 * 60 * 60


def inbox_key(service: str, message_id: str) -> str:
    return f"inbox:{service}:{message_id}"


def effect_key(service: str, entity_id: str, step: str) -> str:
    return f"effects:{service}:{entity_id}:{step}"


def stock_hash_key(item_id: str) -> str:
    return f"stock:{item_id}"


def payment_hash_key(user_id: str) -> str:
    return f"payment:{user_id}"


def recovery_leader_lock_key() -> str:
    return "recovery:leader"


def recovery_step_lock_key(saga_id: str, step: str) -> str:
    return f"recovery:step-lock:{saga_id}:{step}"


def dlq_replay_leader_lock_key() -> str:
    return "dlq-replay:leader"


def dlq_replay_attempt_key(original_message_id: str) -> str:
    return f"dlq:replay-attempts:{original_message_id}"
