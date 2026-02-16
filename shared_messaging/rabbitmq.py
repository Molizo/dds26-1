import os

import pika


COMMAND_EXCHANGES: tuple[tuple[str, str], ...] = (
    ("checkout.command", "direct"),
    ("checkout.retry", "direct"),
    ("checkout.dlx", "direct"),
    ("stock.command", "direct"),
    ("stock.retry", "direct"),
    ("stock.dlx", "direct"),
    ("payment.command", "direct"),
    ("payment.retry", "direct"),
    ("payment.dlx", "direct"),
    ("order.command", "direct"),
    ("order.retry", "direct"),
    ("order.dlx", "direct"),
    ("checkout.events", "topic"),
)

QUEUES: tuple[tuple[str, dict[str, object]], ...] = (
    (
        "checkout.command.q",
        {
            "x-dead-letter-exchange": "checkout.retry",
            "x-dead-letter-routing-key": "checkout.retry",
        },
    ),
    (
        "checkout.command.retry.q",
        {
            "x-message-ttl": 5000,
            "x-dead-letter-exchange": "checkout.command",
            "x-dead-letter-routing-key": "checkout.command",
        },
    ),
    ("checkout.command.dlq", {}),
    (
        "stock.command.q",
        {
            "x-dead-letter-exchange": "stock.retry",
            "x-dead-letter-routing-key": "stock.retry",
        },
    ),
    (
        "stock.command.retry.q",
        {
            "x-message-ttl": 5000,
            "x-dead-letter-exchange": "stock.command",
            "x-dead-letter-routing-key": "stock.command",
        },
    ),
    ("stock.command.dlq", {}),
    (
        "payment.command.q",
        {
            "x-dead-letter-exchange": "payment.retry",
            "x-dead-letter-routing-key": "payment.retry",
        },
    ),
    (
        "payment.command.retry.q",
        {
            "x-message-ttl": 5000,
            "x-dead-letter-exchange": "payment.command",
            "x-dead-letter-routing-key": "payment.command",
        },
    ),
    ("payment.command.dlq", {}),
    (
        "order.command.q",
        {
            "x-dead-letter-exchange": "order.retry",
            "x-dead-letter-routing-key": "order.retry",
        },
    ),
    (
        "order.command.retry.q",
        {
            "x-message-ttl": 5000,
            "x-dead-letter-exchange": "order.command",
            "x-dead-letter-routing-key": "order.command",
        },
    ),
    ("order.command.dlq", {}),
)

BINDINGS: tuple[tuple[str, str, str], ...] = (
    ("checkout.command", "checkout.command.q", "checkout.command"),
    ("checkout.retry", "checkout.command.retry.q", "checkout.retry"),
    ("checkout.dlx", "checkout.command.dlq", "checkout.dlq"),
    ("stock.command", "stock.command.q", "stock.command"),
    ("stock.retry", "stock.command.retry.q", "stock.retry"),
    ("stock.dlx", "stock.command.dlq", "stock.dlq"),
    ("payment.command", "payment.command.q", "payment.command"),
    ("payment.retry", "payment.command.retry.q", "payment.retry"),
    ("payment.dlx", "payment.command.dlq", "payment.dlq"),
    ("order.command", "order.command.q", "order.command"),
    ("order.retry", "order.command.retry.q", "order.retry"),
    ("order.dlx", "order.command.dlq", "order.dlq"),
)


def get_rabbitmq_connection_parameters() -> pika.ConnectionParameters:
    host = os.environ.get("RABBITMQ_HOST", "localhost")
    port = int(os.environ.get("RABBITMQ_PORT", "5672"))
    username = os.environ.get("RABBITMQ_USER", "dds")
    password = os.environ.get("RABBITMQ_PASSWORD", "dds-rabbit")
    vhost = os.environ.get("RABBITMQ_VHOST", "/")
    credentials = pika.PlainCredentials(username, password)

    return pika.ConnectionParameters(
        host=host,
        port=port,
        virtual_host=vhost,
        credentials=credentials,
        heartbeat=30,
        blocked_connection_timeout=30,
    )


def ensure_default_topology(channel) -> None:
    for exchange_name, exchange_type in COMMAND_EXCHANGES:
        channel.exchange_declare(
            exchange=exchange_name,
            exchange_type=exchange_type,
            durable=True,
            auto_delete=False,
        )

    for queue_name, arguments in QUEUES:
        channel.queue_declare(
            queue=queue_name,
            durable=True,
            auto_delete=False,
            arguments=arguments,
        )

    for exchange_name, queue_name, routing_key in BINDINGS:
        channel.queue_bind(
            exchange=exchange_name,
            queue=queue_name,
            routing_key=routing_key,
        )
