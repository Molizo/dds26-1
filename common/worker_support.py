from __future__ import annotations

from collections.abc import Callable

from common.messaging import publish_message, publish_reply


def handle_internal_rpc_delivery(
    *,
    channel,
    method,
    properties,
    body: bytes,
    rabbitmq_url: str,
    service_name: str,
    decode_command: Callable[[bytes], object],
    dispatch: Callable[[object], object],
    encode_reply: Callable[[object], bytes],
    logger,
) -> None:
    try:
        cmd = decode_command(body)
    except Exception as exc:
        logger.error("Failed to decode %s command: %s", service_name, exc)
        channel.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
        return

    reply_to = properties.reply_to if properties else None
    correlation_id = properties.correlation_id if properties else None
    if not reply_to or not correlation_id:
        logger.error(
            "Dropping %s command without reply metadata request=%s",
            service_name,
            getattr(cmd, "request_id", "?"),
        )
        channel.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
        return

    reply = dispatch(cmd)
    channel.basic_ack(delivery_tag=method.delivery_tag)

    try:
        publish_message(
            rabbitmq_url,
            reply_to,
            encode_reply(reply),
            correlation_id=correlation_id,
        )
    except Exception as exc:
        logger.error(
            "Failed to publish %s reply request=%s: %s",
            service_name,
            getattr(cmd, "request_id", "?"),
            exc,
        )


def handle_participant_delivery(
    *,
    channel,
    method,
    body: bytes,
    rabbitmq_url: str,
    service_name: str,
    decode_command: Callable[[bytes], object],
    dispatch: Callable[[object], object],
    encode_reply: Callable[[object], bytes],
    logger,
) -> None:
    try:
        cmd = decode_command(body)
    except Exception as exc:
        logger.error("Failed to decode %s command: %s", service_name, exc)
        channel.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
        return

    logger.info(
        "%s cmd tx=%s command=%s",
        service_name,
        getattr(cmd, "tx_id", "?"),
        getattr(cmd, "command", "?"),
    )
    reply = dispatch(cmd)
    channel.basic_ack(delivery_tag=method.delivery_tag)

    try:
        publish_reply(rabbitmq_url, getattr(cmd, "reply_to", ""), encode_reply(reply))
    except Exception as exc:
        logger.error(
            "Failed to publish %s reply tx=%s: %s",
            service_name,
            getattr(cmd, "tx_id", "?"),
            exc,
        )
