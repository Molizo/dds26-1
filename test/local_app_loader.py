import importlib.util
import os
import sys


REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
ORDER_DIR = os.path.join(REPO_ROOT, "order")
STOCK_DIR = os.path.join(REPO_ROOT, "stock")
PAYMENT_DIR = os.path.join(REPO_ROOT, "payment")


if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)


def _load_module(module_name: str, file_path: str):
    spec = importlib.util.spec_from_file_location(module_name, file_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load module spec for {file_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def load_order_app():
    os.environ.setdefault("CHECKOUT_PROTOCOL", "saga")
    os.environ.setdefault("STOCK_SERVICE_URL", "http://stock-service")
    os.environ.setdefault("ORCHESTRATOR_SERVICE_URL", "http://orchestrator-service")
    os.environ.setdefault("REDIS_HOST", "localhost")
    os.environ.setdefault("REDIS_PORT", "6379")
    os.environ.setdefault("REDIS_PASSWORD", "")
    os.environ.setdefault("REDIS_DB", "0")

    store_module = _load_module("_test_order_store", os.path.join(ORDER_DIR, "store.py"))

    original_store = sys.modules.get("store")
    sys.modules["store"] = store_module
    try:
        app_module = _load_module("_test_order_app", os.path.join(ORDER_DIR, "app.py"))
    finally:
        if original_store is None:
            sys.modules.pop("store", None)
        else:
            sys.modules["store"] = original_store

    return app_module, store_module


def load_stock_app():
    os.environ.setdefault("REDIS_HOST", "localhost")
    os.environ.setdefault("REDIS_PORT", "6379")
    os.environ.setdefault("REDIS_PASSWORD", "")
    os.environ.setdefault("REDIS_DB", "0")

    lua_module = _load_module("_test_stock_lua_scripts", os.path.join(STOCK_DIR, "lua_scripts.py"))
    original_lua = sys.modules.get("lua_scripts")
    sys.modules["lua_scripts"] = lua_module
    try:
        service_module = _load_module("_test_stock_service", os.path.join(STOCK_DIR, "service.py"))
        original_service = sys.modules.get("service")
        sys.modules["service"] = service_module
        try:
            app_module = _load_module("_test_stock_app", os.path.join(STOCK_DIR, "app.py"))
        finally:
            if original_service is None:
                sys.modules.pop("service", None)
            else:
                sys.modules["service"] = original_service
    finally:
        if original_lua is None:
            sys.modules.pop("lua_scripts", None)
        else:
            sys.modules["lua_scripts"] = original_lua

    return app_module, service_module


def load_payment_app():
    os.environ.setdefault("REDIS_HOST", "localhost")
    os.environ.setdefault("REDIS_PORT", "6379")
    os.environ.setdefault("REDIS_PASSWORD", "")
    os.environ.setdefault("REDIS_DB", "0")

    lua_module = _load_module("_test_payment_lua_scripts", os.path.join(PAYMENT_DIR, "lua_scripts.py"))
    original_lua = sys.modules.get("lua_scripts")
    sys.modules["lua_scripts"] = lua_module
    try:
        service_module = _load_module("_test_payment_service", os.path.join(PAYMENT_DIR, "service.py"))
        original_service = sys.modules.get("service")
        sys.modules["service"] = service_module
        try:
            app_module = _load_module("_test_payment_app", os.path.join(PAYMENT_DIR, "app.py"))
        finally:
            if original_service is None:
                sys.modules.pop("service", None)
            else:
                sys.modules["service"] = original_service
    finally:
        if original_lua is None:
            sys.modules.pop("lua_scripts", None)
        else:
            sys.modules["lua_scripts"] = original_lua

    return app_module, service_module
