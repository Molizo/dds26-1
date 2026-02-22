import os

DB_ERROR_STR = "DB error"

STATE_PREPARED = "PREPARED"
STATE_COMMITTED = "COMMITTED"
STATE_ABORTED = "ABORTED"

ITEM_KEY_PREFIX = "item:"
TX_KEY_PREFIX = "stx:"
TX_LOCK_KEY_PREFIX = "internal_tx_lock:"
TX_LOCK_TTL_SECONDS = int(os.environ.get("TX_LOCK_TTL_SECONDS", "10"))

REDIS_HOST = os.environ["REDIS_HOST"]
REDIS_PORT = int(os.environ["REDIS_PORT"])
REDIS_PASSWORD = os.environ["REDIS_PASSWORD"]
REDIS_DB = int(os.environ["REDIS_DB"])

