import random
import uuid

import redis
from msgspec import msgpack

from store import AddItemResult, OrderValue, add_item_to_order, get_order


class OrderService:
    def __init__(self, db: redis.Redis, *, order_update_retry_count: int) -> None:
        self._db = db
        self._order_update_retry_count = order_update_retry_count

    def create_order(self, user_id: str) -> str | None:
        order_id = str(uuid.uuid4())
        value = msgpack.encode(OrderValue(paid=False, items=[], user_id=user_id, total_cost=0))
        try:
            self._db.set(order_id, value)
        except redis.exceptions.RedisError:
            return None
        return order_id

    def batch_init_orders(self, n: int, n_items: int, n_users: int, item_price: int) -> bool:
        def generate_entry() -> OrderValue:
            user_id = random.randint(0, max(0, n_users - 1))
            item1_id = random.randint(0, max(0, n_items - 1))
            item2_id = random.randint(0, max(0, n_items - 1))
            return OrderValue(
                paid=False,
                items=[(f"{item1_id}", 1), (f"{item2_id}", 1)],
                user_id=f"{user_id}",
                total_cost=2 * item_price,
            )

        kv_pairs: dict[str, bytes] = {f"{i}": msgpack.encode(generate_entry()) for i in range(n)}
        try:
            self._db.mset(kv_pairs)
        except redis.exceptions.RedisError:
            return False
        return True

    def find_order(self, order_id: str) -> OrderValue | None:
        return get_order(self._db, order_id)

    def add_item(self, order_id: str, item_id: str, quantity: int, price: int) -> AddItemResult:
        return add_item_to_order(
            self._db,
            order_id,
            item_id,
            quantity,
            price,
            retry_count=self._order_update_retry_count,
        )
