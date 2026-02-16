from typing import Mapping

from msgspec import Struct

from shared_messaging.codec import decode_message
from shared_messaging.contracts import DecodedMessage
from shared_messaging.errors import MessageContractError


class ConsumerDecision(Struct, kw_only=True):
    action: str
    reason: str | None = None
    message: DecodedMessage | None = None


RETRY_DESTINATION_RETRY = "retry"
RETRY_DESTINATION_DLQ = "dlq"


def _as_text(value: object) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            return None
    return None


def _as_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, bytes):
        text = _as_text(value)
        if text is None:
            return None
        try:
            return int(text)
        except ValueError:
            return None
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def retry_count_from_headers(headers: Mapping[str, object] | None, queue_name: str) -> int:
    if headers is None:
        return 0
    x_death = headers.get("x-death")
    if not isinstance(x_death, list):
        return 0

    retries = 0
    for entry in x_death:
        if not isinstance(entry, dict):
            continue
        queue = _as_text(entry.get("queue"))
        count = _as_int(entry.get("count"))
        if queue != queue_name or count is None or count < 0:
            continue
        retries += count
    return retries


def decide_retry_destination(
    headers: Mapping[str, object] | None,
    *,
    queue_name: str,
    max_retries: int,
) -> str:
    if max_retries < 0:
        return RETRY_DESTINATION_DLQ
    retry_count = retry_count_from_headers(headers, queue_name)
    if retry_count >= max_retries:
        return RETRY_DESTINATION_DLQ
    return RETRY_DESTINATION_RETRY


def validate_for_consumer(raw_message: bytes | str) -> ConsumerDecision:
    try:
        decoded = decode_message(raw_message)
    except MessageContractError as exc:
        return ConsumerDecision(action="reject", reason=str(exc))
    return ConsumerDecision(action="ack", message=decoded)
