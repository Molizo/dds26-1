from msgspec import Struct

from shared_messaging.codec import decode_message
from shared_messaging.contracts import DecodedMessage
from shared_messaging.errors import MessageContractError


class ConsumerDecision(Struct, kw_only=True):
    action: str
    reason: str | None = None
    message: DecodedMessage | None = None


def validate_for_consumer(raw_message: bytes | str) -> ConsumerDecision:
    try:
        decoded = decode_message(raw_message)
    except MessageContractError as exc:
        return ConsumerDecision(action="reject", reason=str(exc))
    return ConsumerDecision(action="ack", message=decoded)
