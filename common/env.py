import logging
import os


def get_positive_int_env(name: str, default: int, logger: logging.Logger) -> int:
    value = os.environ.get(name)
    if value is None:
        return default
    try:
        parsed = int(value)
        if parsed > 0:
            return parsed
    except ValueError:
        pass
    logger.warning("Invalid %s=%r, using default=%d", name, value, default)
    return default
