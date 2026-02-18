# Worker Crash Recovery Runbook

## Purpose
Recover quickly from order/stock/payment worker crashes while preserving consistency.

## Trigger Conditions
1. Worker CrashLoopBackOff.
2. Sudden drop in consumer throughput with queue backlog growth.
3. Increased checkout timeout/failure rate tied to one worker domain.

## Detection
1. Check deployment and pod status for:
   - `order-worker-deployment`
   - `stock-worker-deployment`
   - `payment-worker-deployment`
   - `order-reconciliation-worker-deployment`
2. Review recent logs for connection errors, payload rejections, and retry exhaustion.

## Recovery Procedure
1. Stabilize broker and Redis connectivity first.
2. Restart only impacted worker deployment.
3. Confirm consumer loop resumes and queue lag starts decreasing.
4. If crashes repeat:
   - reduce `WORKER_PREFETCH_COUNT` temporarily,
   - inspect failing message type and retry path,
   - verify DLQ forwarding behavior.
5. Ensure reconciliation worker runs after recovery to converge any stale in-flight sagas.

## Post-Recovery Validation
1. Queue depth trends toward baseline.
2. Checkout success/timeout ratios normalize.
3. No data consistency regressions from redelivery:
   - duplicate deliveries are deduped via inbox/effects keys.

## Escalation
1. If the same crash signature recurs across restarts, pause replay amplification and route repeat offenders to parking queue until fix is deployed.

