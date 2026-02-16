import logging

from shared_messaging.codec import decode_message, encode_message
from shared_messaging.consumer import ConsumerDecision, validate_for_consumer
from shared_messaging.contracts import MessageMetadata
from shared_messaging.logging import get_correlation_logger
from shared_messaging.rabbitmq import get_rabbitmq_connection_parameters


def build_rabbitmq_parameters():
    return get_rabbitmq_connection_parameters()


def decode_internal_message(raw_message: bytes | str):
    return decode_message(raw_message)


def encode_internal_message(message_type: str, metadata: MessageMetadata, payload: object) -> bytes:
    return encode_message(message_type, metadata, payload)


def decide_message_action(raw_message: bytes | str) -> ConsumerDecision:
    return validate_for_consumer(raw_message)


def get_message_logger(logger: logging.Logger, metadata: MessageMetadata) -> logging.LoggerAdapter:
    return get_correlation_logger(logger, metadata)
