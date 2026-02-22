from config import TX_KEY_PREFIX, TX_LOCK_KEY_PREFIX, USER_KEY_PREFIX


def user_key(user_id: str) -> str:
    return f"{USER_KEY_PREFIX}{user_id}"


def tx_key(tx_id: str) -> str:
    return f"{TX_KEY_PREFIX}{tx_id}"


def tx_lock_key(tx_id: str) -> str:
    return f"{TX_LOCK_KEY_PREFIX}{tx_id}"

