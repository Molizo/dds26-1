import time

import requests

ORDER_URL = STOCK_URL = PAYMENT_URL = "http://127.0.0.1:8000"
DEFAULT_TIMEOUT_SECONDS = 3
DEFAULT_RETRIES = 20
DEFAULT_RETRY_DELAY_SECONDS = 0.2


def _request_with_retry(
    method: str,
    url: str,
    retries: int = DEFAULT_RETRIES,
    retry_delay_seconds: float = DEFAULT_RETRY_DELAY_SECONDS,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
) -> requests.Response:
    last_error = None
    for attempt in range(retries):
        try:
            return requests.request(method, url, timeout=timeout_seconds)
        except requests.exceptions.RequestException as exc:
            last_error = exc
            if attempt < retries - 1:
                time.sleep(retry_delay_seconds)
    raise RuntimeError(f"Request failed after {retries} retries: {url}") from last_error


def _status_code(value: int | requests.Response) -> int:
    return value if isinstance(value, int) else value.status_code


def status_code_is_success(status_code: int) -> bool:
    return 200 <= status_code < 300


def status_code_is_failure(status_code: int) -> bool:
    return 400 <= status_code < 500


def assert_success(value: int | requests.Response):
    status_code = _status_code(value)
    if not status_code_is_success(status_code):
        raise AssertionError(f"Expected 2xx status code, got {status_code}")


def assert_failure(value: int | requests.Response):
    status_code = _status_code(value)
    if not status_code_is_failure(status_code):
        raise AssertionError(f"Expected 4xx status code, got {status_code}")


def wait_for_gateway(retries: int = DEFAULT_RETRIES):
    url = f"{PAYMENT_URL}/payment/find_user/not-a-real-user-id"
    response = _request_with_retry("GET", url, retries=retries)
    if response.status_code >= 500:
        raise RuntimeError(f"Gateway unhealthy, status code: {response.status_code}")


########################################################################################################################
#   STOCK MICROSERVICE FUNCTIONS
########################################################################################################################
def create_item(price: int) -> dict:
    response = _request_with_retry("POST", f"{STOCK_URL}/stock/item/create/{price}")
    return response.json()


def find_item(item_id: str) -> dict:
    response = _request_with_retry("GET", f"{STOCK_URL}/stock/find/{item_id}")
    return response.json()


def add_stock(item_id: str, amount: int) -> int:
    response = _request_with_retry("POST", f"{STOCK_URL}/stock/add/{item_id}/{amount}")
    return response.status_code


def subtract_stock(item_id: str, amount: int) -> int:
    response = _request_with_retry("POST", f"{STOCK_URL}/stock/subtract/{item_id}/{amount}")
    return response.status_code


def create_item_with_stock(price: int, stock: int) -> dict:
    item = create_item(price)
    assert_success(add_stock(item["item_id"], stock))
    return item


########################################################################################################################
#   PAYMENT MICROSERVICE FUNCTIONS
########################################################################################################################
def payment_pay(user_id: str, amount: int) -> int:
    response = _request_with_retry("POST", f"{PAYMENT_URL}/payment/pay/{user_id}/{amount}")
    return response.status_code


def create_user() -> dict:
    response = _request_with_retry("POST", f"{PAYMENT_URL}/payment/create_user")
    return response.json()


def find_user(user_id: str) -> dict:
    response = _request_with_retry("GET", f"{PAYMENT_URL}/payment/find_user/{user_id}")
    return response.json()


def add_credit_to_user(user_id: str, amount: int) -> int:
    response = _request_with_retry("POST", f"{PAYMENT_URL}/payment/add_funds/{user_id}/{amount}")
    return response.status_code


def create_user_with_credit(amount: int) -> dict:
    user = create_user()
    assert_success(add_credit_to_user(user["user_id"], amount))
    return user


########################################################################################################################
#   ORDER MICROSERVICE FUNCTIONS
########################################################################################################################
def create_order(user_id: str) -> dict:
    response = _request_with_retry("POST", f"{ORDER_URL}/orders/create/{user_id}")
    return response.json()


def add_item_to_order(order_id: str, item_id: str, quantity: int) -> int:
    response = _request_with_retry("POST", f"{ORDER_URL}/orders/addItem/{order_id}/{item_id}/{quantity}")
    return response.status_code


def find_order(order_id: str) -> dict:
    response = _request_with_retry("GET", f"{ORDER_URL}/orders/find/{order_id}")
    return response.json()


def get_order_state(order_id: str) -> dict:
    return find_order(order_id)


def checkout_order(order_id: str) -> requests.Response:
    return _request_with_retry("POST", f"{ORDER_URL}/orders/checkout/{order_id}")


def checkout(order_id: str) -> requests.Response:
    return checkout_order(order_id)


def create_order_with_lines(user_id: str, lines: list[tuple[str, int]]) -> dict:
    order = create_order(user_id)
    order_id = order["order_id"]
    for item_id, quantity in lines:
        assert_success(add_item_to_order(order_id, item_id, quantity))
    return order
