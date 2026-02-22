import requests


def post_with_timeout(url: str, timeout_seconds: int):
    try:
        return requests.post(url, timeout=timeout_seconds)
    except requests.exceptions.RequestException:
        return None


def get_with_timeout(url: str, timeout_seconds: int):
    try:
        return requests.get(url, timeout=timeout_seconds)
    except requests.exceptions.RequestException:
        return None

