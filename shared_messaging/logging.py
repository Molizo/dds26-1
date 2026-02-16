import logging

from shared_messaging.contracts import MessageMetadata


def get_correlation_logger(logger: logging.Logger, metadata: MessageMetadata) -> logging.LoggerAdapter:
    return logging.LoggerAdapter(
        logger,
        {
            "saga_id": metadata.saga_id,
            "order_id": metadata.order_id,
            "message_id": metadata.message_id,
            "correlation_id": metadata.correlation_id,
            "causation_id": metadata.causation_id,
        },
    )
