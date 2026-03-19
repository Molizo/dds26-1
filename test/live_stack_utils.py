import base64
import contextlib
import json
import os
import shutil
import subprocess
import time
import unittest
import urllib.parse
import uuid

import requests

from common.models import ParticipantCommand, ParticipantReply, decode_reply, encode_command


REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DOCKER_COMPOSE_CMD = ["docker", "compose"]
GATEWAY_BASE_URL = "http://127.0.0.1:8000"
RABBITMQ_API_BASE_URL = "http://127.0.0.1:15672/api"
RABBITMQ_AUTH = ("guest", "guest")


def _quote_queue_name(queue_name: str) -> str:
    return urllib.parse.quote(queue_name, safe="")


def run_compose(*args: str) -> None:
    result = subprocess.run(
        [*DOCKER_COMPOSE_CMD, *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"docker compose {' '.join(args)} failed:\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )


def wait_until(
    predicate,
    timeout_seconds: float,
    message: str,
    interval_seconds: float = 0.25,
):
    deadline = time.monotonic() + timeout_seconds
    last_value = None
    while time.monotonic() < deadline:
        last_value = predicate()
        if last_value:
            return last_value
        time.sleep(interval_seconds)
    raise AssertionError(f"{message} (last={last_value!r})")


def wait_gateway_ready(timeout_seconds: float = 45.0) -> None:
    def _route_ready() -> bool:
        try:
            response = requests.get(
                f"{GATEWAY_BASE_URL}/stock/find/does-not-exist",
                timeout=2,
            )
            return 400 <= response.status_code < 500
        except requests.RequestException:
            return False

    wait_until(
        _route_ready,
        timeout_seconds=timeout_seconds,
        message="Timed out waiting for gateway + stock route readiness",
        interval_seconds=0.5,
    )


def rabbitmq_api(method: str, path: str, json_body: dict | None = None) -> requests.Response:
    response = requests.request(
        method=method,
        url=f"{RABBITMQ_API_BASE_URL}{path}",
        auth=RABBITMQ_AUTH,
        json=json_body,
        timeout=10,
    )
    response.raise_for_status()
    return response


def queue_info(queue_name: str) -> dict:
    return rabbitmq_api("GET", f"/queues/%2F/{_quote_queue_name(queue_name)}").json()


def queue_consumers(queue_name: str) -> int:
    return int(queue_info(queue_name).get("consumers", 0))


def queue_message_count(queue_name: str) -> int:
    payload = queue_info(queue_name)
    return int(payload.get("messages", 0))


def wait_for_queue_consumers(
    queue_name: str,
    *,
    expected: int | None = None,
    minimum: int | None = None,
    timeout_seconds: float,
) -> None:
    if expected is None and minimum is None:
        raise ValueError("Either expected or minimum must be provided")

    def _ready() -> bool:
        count = queue_consumers(queue_name)
        if expected is not None:
            return count == expected
        return count >= int(minimum)

    target = f"== {expected}" if expected is not None else f">= {minimum}"
    wait_until(
        _ready,
        timeout_seconds=timeout_seconds,
        message=f"Timed out waiting for {queue_name} consumers {target}",
    )


def wait_for_queue_messages(
    queue_name: str,
    minimum: int,
    timeout_seconds: float,
) -> None:
    wait_until(
        lambda: queue_message_count(queue_name) >= minimum,
        timeout_seconds=timeout_seconds,
        message=f"Timed out waiting for {queue_name} messages >= {minimum}",
    )


def declare_queue(
    queue_name: str,
    *,
    durable: bool = False,
    auto_delete: bool = True,
    arguments: dict | None = None,
) -> None:
    rabbitmq_api(
        "PUT",
        f"/queues/%2F/{_quote_queue_name(queue_name)}",
        json_body={
            "durable": durable,
            "auto_delete": auto_delete,
            "arguments": arguments or {},
        },
    )


def delete_queue(queue_name: str) -> None:
    response = requests.delete(
        f"{RABBITMQ_API_BASE_URL}/queues/%2F/{_quote_queue_name(queue_name)}",
        auth=RABBITMQ_AUTH,
        timeout=10,
    )
    if response.status_code not in {204, 404}:
        response.raise_for_status()


def purge_queue(queue_name: str) -> None:
    rabbitmq_api("DELETE", f"/queues/%2F/{_quote_queue_name(queue_name)}/contents")


def get_queue_messages(
    queue_name: str,
    *,
    count: int = 20,
    requeue: bool,
) -> list[dict]:
    ackmode = "ack_requeue_true" if requeue else "ack_requeue_false"
    return rabbitmq_api(
        "POST",
        f"/queues/%2F/{_quote_queue_name(queue_name)}/get",
        json_body={
            "count": count,
            "ackmode": ackmode,
            "encoding": "base64",
            "truncate": 500000,
        },
    ).json()


def publish_base64(queue_name: str, payload_base64: str) -> None:
    routed = rabbitmq_api(
        "POST",
        "/exchanges/%2F/amq.default/publish",
        json_body={
            "properties": {},
            "routing_key": queue_name,
            "payload": payload_base64,
            "payload_encoding": "base64",
        },
    ).json().get("routed", False)
    if not routed:
        raise AssertionError(f"RabbitMQ refused to route message to queue {queue_name}")


def publish_command(queue_name: str, command: ParticipantCommand) -> None:
    publish_base64(
        queue_name,
        base64.b64encode(encode_command(command)).decode(),
    )


def decode_reply_messages(messages: list[dict]) -> list[ParticipantReply]:
    replies: list[ParticipantReply] = []
    for message in messages:
        raw_payload = base64.b64decode(message["payload"])
        replies.append(decode_reply(raw_payload))
    return replies


def wait_for_replies(
    reply_queue: str,
    *,
    expected_count: int,
    timeout_seconds: float = 15.0,
) -> list[ParticipantReply]:
    collected: list[ParticipantReply] = []
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        messages = get_queue_messages(reply_queue, count=50, requeue=False)
        if messages:
            collected.extend(decode_reply_messages(messages))
            if len(collected) >= expected_count:
                return collected
        time.sleep(0.25)
    raise AssertionError(
        f"Timed out waiting for {expected_count} reply message(s) on {reply_queue}; "
        f"received={len(collected)}"
    )


@contextlib.contextmanager
def temporary_reply_queue(prefix: str = "codex.test.reply"):
    queue_name = f"{prefix}.{uuid.uuid4().hex[:12]}"
    declare_queue(queue_name, durable=False, auto_delete=True)
    try:
        yield queue_name
    finally:
        delete_queue(queue_name)


def docker_exec_service_python(service: str, script: str) -> str:
    result = subprocess.run(
        [*DOCKER_COMPOSE_CMD, "exec", "-T", service, "python", "-c", script],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"docker compose exec {service} python failed:\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )
    return result.stdout.strip()


def inspect_order_runtime(order_id: str, tx_id: str) -> dict:
    order_script = f"""
import json
from app import db
from store import read_order_snapshot

order = read_order_snapshot(db, {order_id!r})
print(json.dumps({{
    "order_paid": None if order is None else bool(order.paid),
}}))
""".strip()
    tx_script = f"""
import json
from app import db
from tx_store import get_active_tx_guard, get_commit_fence, get_decision, get_tx

tx = get_tx(db, {tx_id!r})
print(json.dumps({{
    "active_guard": get_active_tx_guard(db, {order_id!r}),
    "decision": get_decision(db, {tx_id!r}),
    "commit_fence": get_commit_fence(db, {order_id!r}),
    "tx_status": None if tx is None else tx.status,
    "tx_retry_count": None if tx is None else tx.retry_count,
}}))
""".strip()
    order_runtime = json.loads(docker_exec_service_python("order-service", order_script))
    tx_runtime = json.loads(docker_exec_service_python("orchestrator-service", tx_script))
    return {**order_runtime, **tx_runtime}


def list_order_transactions(order_ids: list[str]) -> list[dict]:
    script = f"""
import json
import msgspec
from app import db
from coordinator.models import CheckoutTxValue

decoder = msgspec.msgpack.Decoder(CheckoutTxValue)
target_order_ids = set({order_ids!r})
cursor = 0
results = []
while True:
    cursor, keys = db.scan(cursor=cursor, match="tx:*", count=200)
    for key in keys:
        raw = db.get(key)
        if raw is None:
            continue
        tx = decoder.decode(raw)
        if tx.order_id not in target_order_ids:
            continue
        results.append({{
            "tx_id": tx.tx_id,
            "order_id": tx.order_id,
            "status": tx.status,
            "decision": tx.decision,
            "retry_count": tx.retry_count,
            "updated_at": tx.updated_at,
        }})
    if cursor == 0:
        break
print(json.dumps(results))
""".strip()
    return json.loads(docker_exec_service_python("orchestrator-service", script))


def get_active_tx_guards(order_ids: list[str]) -> dict[str, str | None]:
    script = f"""
import json
from app import db

order_ids = {order_ids!r}
result = {{}}
for order_id in order_ids:
    raw = db.get(f"order_active_tx:{{order_id}}")
    result[order_id] = raw.decode() if raw is not None else None
print(json.dumps(result))
""".strip()
    return json.loads(docker_exec_service_python("orchestrator-service", script))


class LiveStackTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if shutil.which("docker") is None:
            raise unittest.SkipTest("docker CLI not found; these tests require Docker deployment")
        try:
            wait_gateway_ready(timeout_seconds=10.0)
            requests.get(
                f"{RABBITMQ_API_BASE_URL}/overview",
                auth=RABBITMQ_AUTH,
                timeout=3,
            ).raise_for_status()
        except requests.RequestException as exc:
            raise unittest.SkipTest(
                "Gateway/RabbitMQ not reachable. Start the stack with `docker compose up --build` first."
            ) from exc
