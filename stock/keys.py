from config import ITEM_KEY_PREFIX, TX_KEY_PREFIX, TX_LOCK_KEY_PREFIX


def item_key(item_id: str) -> str:
    return f"{ITEM_KEY_PREFIX}{item_id}"


def tx_key(tx_id: str, item_id: str) -> str:
    return f"{TX_KEY_PREFIX}{tx_id}:{item_id}"


def tx_lock_key(tx_id: str, item_id: str) -> str:
    return f"{TX_LOCK_KEY_PREFIX}{tx_id}:{item_id}"

