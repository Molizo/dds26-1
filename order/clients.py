from flask import abort

from common.http_utils import get_with_timeout, post_with_timeout
from config import PAYMENT_INTERNAL_URL, REQUEST_TIMEOUT_SECONDS, REQ_ERROR_STR, STOCK_INTERNAL_URL


def send_post_request(url: str):
    return post_with_timeout(url, REQUEST_TIMEOUT_SECONDS)


def send_get_request(url: str):
    response = get_with_timeout(url, REQUEST_TIMEOUT_SECONDS)
    if response is None:
        abort(400, REQ_ERROR_STR)
    return response


def stock_internal_post(path: str):
    return send_post_request(f"{STOCK_INTERNAL_URL}{path}")


def payment_internal_post(path: str):
    return send_post_request(f"{PAYMENT_INTERNAL_URL}{path}")


def is_ok_response(reply) -> bool:
    return reply is not None and reply.status_code == 200

