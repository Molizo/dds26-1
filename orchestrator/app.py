import atexit
import logging
import os

import redis
from flask import Flask, jsonify

from common.constants import VALID_PROTOCOLS
from runtime_service import OrchestratorRuntime

CHECKOUT_PROTOCOL = os.environ.get("CHECKOUT_PROTOCOL", "").lower()
if CHECKOUT_PROTOCOL not in VALID_PROTOCOLS:
    raise RuntimeError(
        f"CHECKOUT_PROTOCOL must be one of {sorted(VALID_PROTOCOLS)}, "
        f"got: {CHECKOUT_PROTOCOL!r}"
    )

RABBITMQ_URL = os.environ.get("RABBITMQ_URL", "amqp://guest:guest@rabbitmq:5672/")

app = Flask("orchestrator-service")

db: redis.Redis = redis.Redis(
    host=os.environ["REDIS_HOST"],
    port=int(os.environ["REDIS_PORT"]),
    password=os.environ["REDIS_PASSWORD"],
    db=int(os.environ["REDIS_DB"]),
)


def close_db_connection():
    db.close()


atexit.register(close_db_connection)


def _get_runtime() -> OrchestratorRuntime:
    if not hasattr(_get_runtime, "_instance"):
        _get_runtime._instance = OrchestratorRuntime(
            db,
            rabbitmq_url=RABBITMQ_URL,
            checkout_protocol=CHECKOUT_PROTOCOL,
            logger=app.logger,
        )
    return _get_runtime._instance


@app.get("/internal/tx_decision/<tx_id>")
def tx_decision(tx_id: str):
    decision = _get_runtime().get_decision(tx_id)
    if decision is None:
        return jsonify({"decision": "unknown"})
    return jsonify({"decision": decision})


@app.get("/healthz")
def healthz():
    return jsonify({"ok": True})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=True)
else:
    gunicorn_logger = logging.getLogger("gunicorn.error")
    app.logger.handlers = gunicorn_logger.handlers
    app.logger.setLevel(gunicorn_logger.level)
