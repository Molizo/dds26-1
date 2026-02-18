# DLQ Drain Runbook

## Purpose
Safely replay dead-lettered messages without duplicating business effects.

## Trigger Conditions
1. Rising depth in domain DLQs:
   - `checkout.command.dlq`
   - `stock.command.dlq`
   - `payment.command.dlq`
   - `order.command.dlq`
2. Repeated processing errors in worker logs for the same message types.

## Preconditions
1. Root cause is identified or mitigated (otherwise replay will re-fail).
2. `dlq-replay-worker` is healthy.
3. Replay rate and max attempts are confirmed:
   - `DLQ_REPLAY_RATE_PER_SEC`
   - `DLQ_REPLAY_MAX_ATTEMPTS`

## Drain Procedure
1. Verify queue health and consumer status.
2. Start with low replay rate if the system is already under load.
3. Allow replay worker to drain DLQ messages automatically.
4. Messages exceeding bounded replay attempts are quarantined to `dlq.parking.q`.

## Safety Guardrails
1. Do not bulk purge DLQs unless explicitly approved.
2. Do not manually replay from parking queue without first classifying message contract and source queue metadata.
3. Keep replay bounded and observe error rate after each increment.

## Validation
1. DLQ depth decreases without causing cascading retries.
2. Parking queue growth is understood and classified.
3. Idempotency guarantees remain intact:
   - no duplicate charge/refund,
   - no duplicate reserve/release side effects.

