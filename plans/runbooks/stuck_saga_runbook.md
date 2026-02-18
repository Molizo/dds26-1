# Stuck Saga Runbook

## Purpose
Resolve checkout sagas that remain in non-terminal states longer than expected.

## Trigger Conditions
1. Elevated checkout timeout rate (`POST /orders/checkout/{order_id}` returning 4xx timeout).
2. Growing count of non-terminal `saga:*` records older than the stale threshold.
3. Queue backlog rises while terminal events (`OrderCommitted`/`OrderFailed`) flatten.

## Immediate Checks
1. Verify order worker pods are healthy.
2. Verify RabbitMQ queue depth for:
   - `checkout.command.q`
   - `stock.command.q`
   - `payment.command.q`
   - `order.command.q`
3. Verify reconciliation worker pod is running.

## Triage Steps
1. Confirm stale saga volume:
   - Inspect representative `saga:{saga_id}` entries and `updated_at_ms`.
2. Identify dominant stuck state:
   - `RESERVING_STOCK`, `CHARGING_PAYMENT`, `RELEASING_STOCK`, `COMMITTING_ORDER`.
3. Correlate with queue depth:
   - If backlog high: scale relevant worker deployment first.
   - If backlog low but stale high: verify reconciliation worker logs and leader lock.

## Remediation
1. Scale workers handling blocked phase:
   - stock path: `stock-worker-deployment`
   - payment path: `payment-worker-deployment`
   - orchestration path: `order-worker-deployment`
2. Restart only unhealthy worker pods; avoid broad restarts unless needed.
3. Ensure reconciliation worker is active; restart it if leader lock is orphaned.
4. If stale sagas persist after worker recovery, run a controlled reconciliation cycle and monitor terminal-state convergence.

## Validation
1. Stale non-terminal saga count trends down.
2. `checkout` timeout error rate drops.
3. No integrity regressions:
   - no duplicate charges,
   - no negative stock,
   - no inconsistent paid/failed order markers.

