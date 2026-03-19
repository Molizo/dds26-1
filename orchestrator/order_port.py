"""HTTP-backed OrderPort for the standalone orchestrator."""
from typing import Optional

import requests

from coordinator.ports import OrderSnapshot


class HttpOrderPort:
    def __init__(
        self,
        order_service_url: str,
        session: requests.Session,
        timeout_seconds: float = 5.0,
    ):
        self._order_service_url = order_service_url.rstrip("/")
        self._session = session
        self._timeout_seconds = timeout_seconds

    def read_order(self, order_id: str) -> Optional[OrderSnapshot]:
        try:
            response = self._session.get(
                f"{self._order_service_url}/internal/orders/{order_id}",
                timeout=self._timeout_seconds,
            )
        except requests.exceptions.RequestException:
            return None
        if response.status_code != 200:
            return None

        payload = response.json()
        return OrderSnapshot(
            order_id=payload["order_id"],
            user_id=payload["user_id"],
            total_cost=int(payload["total_cost"]),
            paid=bool(payload["paid"]),
            items=[(item_id, int(quantity)) for item_id, quantity in payload["items"]],
        )

    def mark_paid(self, order_id: str, tx_id: str) -> bool:
        try:
            response = self._session.post(
                f"{self._order_service_url}/internal/orders/{order_id}/mark_paid",
                json={"tx_id": tx_id},
                timeout=self._timeout_seconds,
            )
        except requests.exceptions.RequestException:
            return False
        return response.status_code == 200
