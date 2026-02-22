from flask import Blueprint

from service import (
    add_credit_handler,
    batch_init_users_handler,
    create_user_handler,
    find_user_handler,
    remove_credit_handler,
)

public_bp = Blueprint("payment_public", __name__)


@public_bp.post("/create_user")
def create_user():
    return create_user_handler()


@public_bp.post("/batch_init/<n>/<starting_money>")
def batch_init_users(n: int, starting_money: int):
    return batch_init_users_handler(n, starting_money)


@public_bp.get("/find_user/<user_id>")
def find_user(user_id: str):
    return find_user_handler(user_id)


@public_bp.post("/add_funds/<user_id>/<amount>")
def add_credit(user_id: str, amount: int):
    return add_credit_handler(user_id, amount)


@public_bp.post("/pay/<user_id>/<amount>")
def remove_credit(user_id: str, amount: int):
    return remove_credit_handler(user_id, amount)

