import uuid


LOCK_RELEASE_SCRIPT = """
if redis.call("GET", KEYS[1]) == ARGV[1] then
    return redis.call("DEL", KEYS[1])
else
    return 0
end
"""

LOCK_RENEW_SCRIPT = """
if redis.call("GET", KEYS[1]) == ARGV[1] then
    return redis.call("EXPIRE", KEYS[1], ARGV[2])
else
    return 0
end
"""


def acquire_lock(db, key: str, ttl_seconds: int) -> str | None:
    token = str(uuid.uuid4())
    acquired = db.set(key, token, nx=True, ex=ttl_seconds)
    if not acquired:
        return None
    return token


def release_lock(db, key: str, token: str):
    db.eval(LOCK_RELEASE_SCRIPT, 1, key, token)


def renew_lock(db, key: str, token: str, ttl_seconds: int) -> bool:
    result = db.eval(LOCK_RENEW_SCRIPT, 1, key, token, ttl_seconds)
    if isinstance(result, bytes):
        result = int(result.decode())
    return int(result) == 1
