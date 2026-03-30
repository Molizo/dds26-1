import atexit
import logging
import os
import uuid

import redis
import requests
from flask import Flask, Response, abort, jsonify

from common.constants import (
    ORCHESTRATOR_CMD_ACQUIRE_MUTATION_GUARD,
    ORCHESTRATOR_CMD_CHECKOUT,
    ORCHESTRATOR_CMD_RELEASE_MUTATION_GUARD,
)
from common.models import InternalReply
import domain_service
from orchestrator_client import MutationGuardAcquireResult, OrchestratorClient
from store import OrderLookupResult, OrderValue

DB_ERROR_STR = "DB error"
REQ_ERROR_STR = "Requests error"
REQUEST_TIMEOUT_SECONDS = 5.0
RPC_TIMEOUT_SECONDS = 5.0
CHECKOUT_RPC_TIMEOUT_SECONDS = 45.0
REQUEST_RETRY_COUNT = 3
ORDER_UPDATE_RETRY_COUNT = 10
MUTATION_GUARD_RETRY_COUNT = 20
MUTATION_GUARD_RETRY_DELAY_SECONDS = 0.05

STOCK_SERVICE_URL = os.environ["STOCK_SERVICE_URL"]
RABBITMQ_URL = os.environ.get("RABBITMQ_URL", "amqp://guest:guest@rabbitmq:5672/")

app = Flask("order-service")

db: redis.Redis = redis.Redis(
    host=os.environ["REDIS_HOST"],
    port=int(os.environ["REDIS_PORT"]),
    password=os.environ["REDIS_PASSWORD"],
    db=int(os.environ["REDIS_DB"]),
)


def close_db_connection():
    db.close()


atexit.register(close_db_connection)

_session = requests.Session()


def _get_order_service() -> domain_service.OrderService:
    return domain_service.OrderService(
        db,
        order_update_retry_count=ORDER_UPDATE_RETRY_COUNT,
    )


def _get_orchestrator_client() -> OrchestratorClient:
    if not hasattr(_get_orchestrator_client, "_instance"):
        _get_orchestrator_client._instance = OrchestratorClient(
            RABBITMQ_URL,
            rpc_timeout_seconds=RPC_TIMEOUT_SECONDS,
            checkout_timeout_seconds=CHECKOUT_RPC_TIMEOUT_SECONDS,
            guard_retry_count=MUTATION_GUARD_RETRY_COUNT,
            guard_retry_delay_seconds=MUTATION_GUARD_RETRY_DELAY_SECONDS,
            logger=app.logger,
        )
    return _get_orchestrator_client._instance


def _get_order_or_abort(order_id: str) -> OrderValue:
    result = _get_order_service().find_order_result(order_id)
    if result.status == "db_error":
        abort(400, DB_ERROR_STR)
    if result.order is None:
        abort(400, f"Order: {order_id} not found!")
    return result.order


def _lookup_order(order_id: str) -> OrderLookupResult:
    return _get_order_service().find_order_result(order_id)


def _require_positive_int(value: int | str, field_name: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        abort(400, f"{field_name} must be a positive integer")
    if parsed <= 0:
        abort(400, f"{field_name} must be a positive integer")
    return parsed


def _send_get(url: str) -> requests.Response:
    for attempt in range(REQUEST_RETRY_COUNT):
        try:
            return _session.get(url, timeout=REQUEST_TIMEOUT_SECONDS)
        except requests.exceptions.RequestException as exc:
            app.logger.warning(
                "GET %s attempt %s/%s failed: %s",
                url,
                attempt + 1,
                REQUEST_RETRY_COUNT,
                exc,
            )
    abort(400, REQ_ERROR_STR)


def _call_orchestrator(
    command: str,
    order_id: str,
    *,
    tx_id: str | None = None,
    lease_id: str | None = None,
    timeout_seconds: float = RPC_TIMEOUT_SECONDS,
) -> InternalReply | None:
    client = _get_orchestrator_client()
    if command == ORCHESTRATOR_CMD_CHECKOUT:
        if tx_id is None:
            raise ValueError("tx_id is required for checkout")
        return client.checkout(order_id, tx_id)
    return client._call(
        command,
        order_id,
        tx_id=tx_id,
        lease_id=lease_id,
        timeout_seconds=timeout_seconds,
    )


def _release_mutation_guard(order_id: str, lease_id: str) -> None:
    _get_orchestrator_client().release_mutation_guard(order_id, lease_id)


def _acquire_mutation_guard(order_id: str) -> MutationGuardAcquireResult:
    return _get_orchestrator_client().acquire_mutation_guard(order_id)


@app.post("/create/<user_id>")
def create_order(user_id: str):
    key = _get_order_service().create_order(user_id)
    if key is None:
        return abort(400, DB_ERROR_STR)
    return jsonify({"order_id": key})


@app.post("/batch_init/<n>/<n_items>/<n_users>/<item_price>")
def batch_init_users(n: int, n_items: int, n_users: int, item_price: int):
    n = int(n)
    n_items = int(n_items)
    n_users = int(n_users)
    item_price = int(item_price)

    if not _get_order_service().batch_init_orders(n, n_items, n_users, item_price):
        return abort(400, DB_ERROR_STR)
    return jsonify({"msg": "Batch init for orders successful"})


@app.get("/find/<order_id>")
def find_order(order_id: str):
    order = _get_order_or_abort(order_id)
    return jsonify(
        {
            "order_id": order_id,
            "paid": order.paid,
            "items": order.items,
            "user_id": order.user_id,
            "total_cost": order.total_cost,
        }
    )


def _precheck_order_for_add_item(order_id: str) -> tuple[bool, int | None, str | None]:
    order_paid_key = f"order_paid:{order_id}"
    try:
        lookup = _lookup_order(order_id)
        if lookup.status == "db_error":
            return False, 400, DB_ERROR_STR
        order = lookup.order
        if order is None:
            return False, 400, f"Order: {order_id} not found!"
        if order.paid or db.exists(order_paid_key) == 1:
            return False, 409, "Order already paid"
    except redis.exceptions.RedisError:
        return False, 400, DB_ERROR_STR
    return True, None, None


@app.post("/addItem/<order_id>/<item_id>/<quantity>")
def add_item(order_id: str, item_id: str, quantity: int):
    quantity = _require_positive_int(quantity, "quantity")
    item_reply = _send_get(f"{STOCK_SERVICE_URL}/find/{item_id}")
    if item_reply.status_code != 200:
        abort(400, f"Item: {item_id} does not exist!")

    ok, status_code, error = _precheck_order_for_add_item(order_id)
    if not ok:
        abort(status_code, error)
    item_json: dict = item_reply.json()
    price = int(item_json["price"])
    guard = _acquire_mutation_guard(order_id)
    if not guard.ok or guard.lease_id is None:
        abort(guard.status_code, guard.error or "Could not verify checkout state")

    try:
        result = _get_order_service().add_item(order_id, item_id, quantity, price)
    finally:
        _release_mutation_guard(order_id, guard.lease_id)

    if result.status == "ok":
        return Response(
            f"Item: {item_id} added to: {order_id} price updated to: {result.total_cost}",
            status=200,
        )
    if result.status == "not_found":
        abort(400, f"Order: {order_id} not found!")
    if result.status == "already_paid":
        abort(409, "Order already paid")
    if result.status == "db_error":
        abort(400, DB_ERROR_STR)
    abort(409, "Conflict: could not update order")


@app.post("/checkout/<order_id>")
def checkout(order_id: str):
    order_paid_key = f"order_paid:{order_id}"
    try:
        lookup = _lookup_order(order_id)
        if lookup.status == "db_error":
            abort(400, DB_ERROR_STR)
        order = lookup.order
        if order is None:
            abort(400, f"Order: {order_id} not found!")
        if order.paid or db.exists(order_paid_key) == 1:
            return Response("Checkout successful", status=200)
    except redis.exceptions.RedisError:
        abort(400, DB_ERROR_STR)

    tx_id = str(uuid.uuid4())
    try:
        reply = _call_orchestrator(
            ORCHESTRATOR_CMD_CHECKOUT,
            order_id,
            tx_id=tx_id,
            timeout_seconds=CHECKOUT_RPC_TIMEOUT_SECONDS,
        )
    except Exception:
        app.logger.exception("Checkout RPC failed order=%s tx=%s", order_id, tx_id)
        abort(400, "Checkout unavailable")

    if reply is None:
        abort(400, "Checkout result unavailable")
    if reply.ok:
        return Response("Checkout successful", status=200)
    abort(reply.status_code or 400, reply.error or "Checkout failed")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=True)
else:
    gunicorn_logger = logging.getLogger("gunicorn.error")
    app.logger.handlers = gunicorn_logger.handlers
    app.logger.setLevel(gunicorn_logger.level)
