#!/bin/sh
set -eu

COOKIE_FILE="/var/lib/rabbitmq/.erlang.cookie"

if [ -n "${RABBITMQ_ERLANG_COOKIE:-}" ]; then
    printf '%s' "${RABBITMQ_ERLANG_COOKIE}" > "${COOKIE_FILE}"
fi

chmod 400 "${COOKIE_FILE}"

wait_for_local_rabbitmq() {
    attempt=0
    until rabbitmq-diagnostics -q ping >/dev/null 2>&1; do
        attempt=$((attempt + 1))
        if [ "${attempt}" -ge 30 ]; then
            echo "RabbitMQ did not become ready in time" >&2
            exit 1
        fi
        sleep 2
    done
}

join_cluster_if_needed() {
    if rabbitmqctl cluster_status 2>/dev/null | grep -q "rabbit@${JOIN_CLUSTER_HOST}"; then
        echo "Node already joined to rabbit@${JOIN_CLUSTER_HOST}"
        return
    fi

    rabbitmqctl stop_app
    rabbitmqctl reset

    attempt=0
    until rabbitmqctl join_cluster "rabbit@${JOIN_CLUSTER_HOST}" >/dev/null 2>&1; do
        attempt=$((attempt + 1))
        if [ "${attempt}" -ge 30 ]; then
            echo "Failed to join cluster rabbit@${JOIN_CLUSTER_HOST}" >&2
            exit 1
        fi
        sleep 2
    done

    rabbitmqctl start_app
}

if [ -z "${JOIN_CLUSTER_HOST:-}" ]; then
    exec /usr/local/bin/docker-entrypoint.sh rabbitmq-server
fi

/usr/local/bin/docker-entrypoint.sh rabbitmq-server -detached
wait_for_local_rabbitmq
join_cluster_if_needed
rabbitmqctl stop

while rabbitmq-diagnostics -q ping >/dev/null 2>&1; do
    sleep 1
done

exec /usr/local/bin/docker-entrypoint.sh rabbitmq-server
