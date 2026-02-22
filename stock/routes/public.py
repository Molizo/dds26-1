from flask import Blueprint

from service import (
    add_stock_handler,
    batch_init_stock_handler,
    create_item_handler,
    find_item_handler,
    remove_stock_handler,
)

public_bp = Blueprint("stock_public", __name__)


@public_bp.post("/item/create/<price>")
def create_item(price: int):
    return create_item_handler(price)


@public_bp.post("/batch_init/<n>/<starting_stock>/<item_price>")
def batch_init_stock(n: int, starting_stock: int, item_price: int):
    return batch_init_stock_handler(n, starting_stock, item_price)


@public_bp.get("/find/<item_id>")
def find_item(item_id: str):
    return find_item_handler(item_id)


@public_bp.post("/add/<item_id>/<amount>")
def add_stock(item_id: str, amount: int):
    return add_stock_handler(item_id, amount)


@public_bp.post("/subtract/<item_id>/<amount>")
def remove_stock(item_id: str, amount: int):
    return remove_stock_handler(item_id, amount)

