from typing import cast

import msgspec

from shared_messaging.contracts import (
    MESSAGE_PAYLOAD_TYPES,
    REQUIRED_METADATA_FIELDS,
    DecodedMessage,
    MessageMetadata,
)
from shared_messaging.errors import (
    MessageDecodeError,
    MessageValidationError,
    UnsupportedMessageTypeError,
)
from shared_messaging.types import MessageType


def encode_message(message_type: str, metadata: MessageMetadata, payload: object) -> bytes:
    payload_type = MESSAGE_PAYLOAD_TYPES.get(message_type)
    if payload_type is None:
        raise UnsupportedMessageTypeError(f"Unsupported message_type: {message_type}")
    if not isinstance(payload, payload_type):
        raise MessageValidationError(
            f"Payload type mismatch for {message_type}: expected {payload_type.__name__}"
        )

    body = {
        "message_type": message_type,
        **msgspec.to_builtins(metadata),
        "payload": msgspec.to_builtins(payload),
    }
    return msgspec.json.encode(body)


def decode_message(raw_message: bytes | str) -> DecodedMessage:
    try:
        data = msgspec.json.decode(raw_message)
    except msgspec.DecodeError as exc:
        raise MessageDecodeError(f"Invalid JSON message: {exc}") from exc

    if not isinstance(data, dict):
        raise MessageValidationError("Message must be a JSON object")

    message_type_raw = data.get("message_type")
    if not isinstance(message_type_raw, str):
        raise MessageValidationError("Field 'message_type' must be a string")

    payload_type = MESSAGE_PAYLOAD_TYPES.get(message_type_raw)
    if payload_type is None:
        raise UnsupportedMessageTypeError(f"Unsupported message_type: {message_type_raw}")

    missing = [field for field in REQUIRED_METADATA_FIELDS if field not in data]
    if missing:
        missing_fields = ", ".join(sorted(missing))
        raise MessageValidationError(f"Missing required metadata fields: {missing_fields}")

    metadata_data = {field: data[field] for field in REQUIRED_METADATA_FIELDS}
    try:
        metadata = msgspec.convert(metadata_data, type=MessageMetadata, strict=True)
    except msgspec.ValidationError as exc:
        raise MessageValidationError(f"Invalid metadata: {exc}") from exc

    if "payload" not in data:
        raise MessageValidationError("Missing required field: payload")

    try:
        payload = msgspec.convert(data["payload"], type=payload_type, strict=True)
    except msgspec.ValidationError as exc:
        raise MessageValidationError(f"Invalid payload for {message_type_raw}: {exc}") from exc

    return DecodedMessage(
        message_type=cast(MessageType, message_type_raw),
        metadata=metadata,
        payload=payload,
    )
