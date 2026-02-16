import os

import pika


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
