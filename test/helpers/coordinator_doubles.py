from common.constants import (
    CMD_COMMIT,
    CMD_HOLD,
    CMD_RELEASE,
    SVC_PAYMENT,
    SVC_STOCK,
    TERMINAL_STATUSES,
)
from common.models import OrderSnapshot, ParticipantReply
from coordinator.service import CoordinatorService


def make_snapshot(
    order_id="order-1",
    user_id="user-1",
    total_cost=100,
    paid=False,
    items=None,
):
    if items is None:
        items = [("item-1", 2), ("item-2", 3)]
    return OrderSnapshot(
        order_id=order_id,
        user_id=user_id,
        total_cost=total_cost,
        paid=paid,
        items=items,
    )


def stock_reply(tx_id, ok=True, error=None, command=CMD_HOLD):
    return ParticipantReply(
        tx_id=tx_id,
        service=SVC_STOCK,
        command=command,
        ok=ok,
        error=error,
    )


def payment_reply(tx_id, ok=True, error=None, command=CMD_HOLD):
    return ParticipantReply(
        tx_id=tx_id,
        service=SVC_PAYMENT,
        command=command,
        ok=ok,
        error=error,
    )


_DEFAULT_SNAPSHOT = object()


class MockOrderPort:
    def __init__(self, snapshot=_DEFAULT_SNAPSHOT):
        self._snapshot = make_snapshot() if snapshot is _DEFAULT_SNAPSHOT else snapshot
        self.mark_paid_calls = []
        self.read_order_calls = []

    def read_order(self, order_id):
        self.read_order_calls.append(order_id)
        return self._snapshot

    def mark_paid(self, order_id, tx_id):
        self.mark_paid_calls.append((order_id, tx_id))
        return True


class MockTxStore:
    def __init__(self):
        self.txs = {}
        self.decisions = {}
        self.fences = {}
        self.guards = {}

    def create_tx(self, tx):
        self.txs[tx.tx_id] = tx

    def update_tx(self, tx):
        self.txs[tx.tx_id] = tx

    def get_tx(self, tx_id):
        return self.txs.get(tx_id)

    def get_stale_non_terminal_txs(self, stale_before_ms, batch_limit=50):
        return [
            tx for tx in self.txs.values()
            if tx.status not in TERMINAL_STATUSES and tx.updated_at <= stale_before_ms
        ]

    def set_decision(self, tx_id, decision):
        self.decisions[tx_id] = decision

    def set_decision_and_update_tx(self, tx_id, decision, tx):
        self.decisions[tx_id] = decision
        self.txs[tx.tx_id] = tx

    def get_decision(self, tx_id):
        return self.decisions.get(tx_id)

    def set_commit_fence(self, order_id, tx_id):
        self.fences[order_id] = tx_id

    def set_decision_fence_and_update_tx(self, tx_id, decision, order_id, tx):
        self.decisions[tx_id] = decision
        self.fences[order_id] = tx_id
        self.txs[tx.tx_id] = tx

    def get_commit_fence(self, order_id):
        return self.fences.get(order_id)

    def clear_commit_fence(self, order_id):
        self.fences.pop(order_id, None)

    def acquire_active_tx_guard(self, order_id, tx_id, ttl):
        if order_id in self.guards:
            return False
        self.guards[order_id] = tx_id
        return True

    def get_active_tx_guard(self, order_id):
        return self.guards.get(order_id)

    def clear_active_tx_guard(self, order_id):
        self.guards.pop(order_id, None)

    def clear_active_tx_guard_if_owned(self, order_id, tx_id):
        if self.guards.get(order_id) not in {None, tx_id}:
            return False
        self.guards.pop(order_id, None)
        return True

    def refresh_active_tx_guard(self, order_id, ttl):
        return order_id in self.guards


def make_coordinator(
    *,
    order_port=None,
    tx_store=None,
    hold_replies=None,
    commit_replies=None,
    release_replies=None,
):
    if order_port is None:
        order_port = MockOrderPort()
    if tx_store is None:
        tx_store = MockTxStore()

    coordinator = CoordinatorService(
        rabbitmq_url="amqp://test",
        order_port=order_port,
        tx_store=tx_store,
    )

    call_count = {"holds": 0, "commits": 0, "releases": 0}

    def mock_wait(tx_id, timeout):
        if call_count["holds"] == 0 and hold_replies is not None:
            call_count["holds"] = 1
            return hold_replies
        if call_count["commits"] == 0 and commit_replies is not None:
            call_count["commits"] = 1
            return commit_replies
        if call_count["releases"] == 0 and release_replies is not None:
            call_count["releases"] = 1
            return release_replies
        return []

    return coordinator, order_port, tx_store, mock_wait
