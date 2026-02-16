# Final Implementation Plan: Redis + RabbitMQ Saga

## Purpose
This document captures the final implementation plan and all decisions made in discussion for re-architecting this project to be highly scalable, event-driven, and fault-tolerant while preserving the external API contract.

This plan is intentionally phased by technology changes and allows temporary breakage between phases, as agreed.

## Final Decisions and Constraints We Agreed On
1. Keep external HTTP interface unchanged by the end:
   - Same routes under `/orders/*`, `/stock/*`, `/payment/*`.
   - Same response shape expectations.
   - Same success/failure semantics (2xx for success, 4xx for failure).
2. Use RabbitMQ as the internal event bus for transactional workflow.
3. Keep Redis as the system of record for this project (no Postgres migration).
4. Assume database is always available and does not crash or limit throughput.
5. Services can crash at any time, including mid-message handling.
6. Delivery model: at-least-once processing with strict idempotency.
7. Checkout remains externally synchronous in behavior (immediate 200/4xx), but implemented through event-driven internals with bounded waiting.
8. No versioned schemas in this implementation plan.
9. No feature flags in this implementation plan.
10. Migration is phased and may temporarily break runtime behavior between phases.

## Design Goals Mapped to Assignment Criteria
1. Scalability and elasticity:
   - Queue-based decoupling with horizontally scalable workers.
   - Separate scaling for API pods and worker pods.
2. Consistency:
   - Eventual consistency across services.
   - Atomic local state transitions in Redis via Lua scripts.
   - Idempotent command handling and compensation.
3. Availability:
   - Durable queues with retries and DLQ.
   - Replicated workers; no single-process transaction dependency.
4. Fault tolerance:
   - ACK only after state commit.
   - Replay-safe processing on redelivery.
   - Reconciliation worker for stale/incomplete sagas.
5. Performance:
   - Remove synchronous service-to-service checkout fanout.
   - Tune prefetch/concurrency for throughput and latency.
6. Event-driven architecture:
   - Checkout implemented as command/event Saga over RabbitMQ.

## Anti-Patterns Explicitly Avoided (from Compared Projects)
1. No blocking pseudo-RPC loops over RabbitMQ in HTTP request threads.
2. No publish-before-persisted-pending-state race.
3. No duplicate-item overwrite bug during checkout aggregation (must sum quantities by `item_id`).
4. No benchmark-incompatible requirement for manual sleep/status polling for normal checkout behavior.
5. No shared mutable RabbitMQ channel pattern across concurrent request handling.

## Target Architecture (Final State)
1. API ingress (NGINX/Ingress) still exposes the same external endpoints.
2. Order service has:
   - API process for endpoints.
   - Orchestration worker process for Saga transitions.
3. Stock service has:
   - API process for non-transactional endpoints.
   - Worker process for transactional commands (`reserve/release`).
4. Payment service has:
   - API process for non-transactional endpoints.
   - Worker process for transactional commands (`charge/refund`).
5. RabbitMQ manages command/event flow, retries, and dead-letter routing.
6. Redis stores domain state, saga state, and idempotency markers.

## Internal Message Set (Unversioned)
1. `CheckoutRequested`
2. `ReserveStock`
3. `StockReserved`
4. `StockRejected`
5. `ChargePayment`
6. `PaymentCharged`
7. `PaymentRejected`
8. `ReleaseStock`
9. `OrderCommitted`
10. `OrderFailed`

Mandatory metadata in each message:
1. `message_id`
2. `saga_id`
3. `order_id`
4. `step`
5. `attempt`
6. `timestamp`
7. `correlation_id`
8. `causation_id`

## Redis Data Model (Final)
1. `order:{order_id}`:
   - `paid`, `items`, `user_id`, `total_cost`, `checkout_status`
2. `saga:{saga_id}`:
   - `state`, `order_id`, `last_step`, `updated_at`, retry counters
3. `inbox:{service}:{message_id}`:
   - processed-message marker for dedupe
4. `effects:{service}:{entity_id}:{step}`:
   - applied-effect marker
5. Optional queue-support keys for reconciliation scheduling.

All critical stock/payment transitions use Lua scripts for atomic check-update-mark operations.

## Queue Topology (Final)
1. `checkout.command`
2. `stock.command`
3. `payment.command`
4. `order.command`
5. `checkout.events`
6. Retry queues per domain (for delayed retries)
7. DLQ per domain and command class

Durability and policies:
1. Durable queues/exchanges.
2. Message persistence enabled.
3. Retry with bounded attempts and exponential backoff.
4. Poison messages routed to DLQ for manual or controlled replay.

## Implementation Phases

### Phase 0: Contract Freeze and Work Breakdown
1. Freeze final external contract and non-negotiables listed above.
2. Produce transition matrix for Saga states and failure handling.
3. Write run-order checklist for all phases.

Expected temporary state:
1. No behavior change yet.

### Phase 1: RabbitMQ Infrastructure in Helm/K8s
1. Add RabbitMQ chart and values.
2. Configure durable queues, retry queues, DLQs, and policies.
3. Add worker deployment templates for order/stock/payment.
4. Add baseline observability for queue depth and consumer health.

Expected temporary state:
1. App still uses old checkout path.
2. Broker exists but may be unused initially.

### Phase 2: Internal Contracts and Libraries
1. Add shared message serialization/parsing module used by all services.
2. Add strict validation for required message metadata.
3. Add correlation logging helpers (`saga_id`, `order_id`, `message_id`).

Expected temporary state:
1. Workers may start but process test/no-op messages only.

### Phase 3: Redis Idempotency and Atomicity Layer
1. Add inbox/effects keys and retention policy.
2. Implement Lua scripts for:
   - reserve stock
   - release stock
   - charge payment
   - refund payment
3. Add reusable processing helper:
   - check dedupe
   - apply atomic effect
   - persist state
   - emit next event
   - ACK

Expected temporary state:
1. Legacy checkout still active.
2. New atomic primitives can be validated in isolation.

### Phase 4: Worker Runtime per Service
1. Add RabbitMQ consumer loops to stock/payment/order workers.
2. Implement ack discipline:
   - ACK only after Redis mutation and saga/effect marker commit.
3. Implement retry and DLQ behavior.
4. Ensure service restart safely resumes consumer flow.

Expected temporary state:
1. Both old and new runtime pieces coexist.
2. Some transactional paths may be incomplete.

### Phase 5: Checkout Orchestration Cutover
1. Replace synchronous checkout fanout with Saga orchestration:
   - `POST /orders/checkout/{order_id}`:
     - aggregate item quantities by `item_id` (sum duplicates)
     - write `PENDING` saga state first
     - publish `CheckoutRequested`
     - bounded wait for terminal state
     - return 200/4xx accordingly
2. Order worker drives transitions:
   - reserve stock -> charge payment -> commit order
   - on payment failure -> release stock -> mark failed

Expected temporary state:
1. Checkout may be partially unstable during cutover window.

### Phase 6: Reconciliation and Crash Recovery
1. Add reconciliation worker:
   - scan stale `PENDING` sagas
   - replay missing step or compensation
2. Add startup recovery hooks:
   - continue/reconcile incomplete sagas after worker restart
3. Add replay tooling for DLQ with idempotent safeguards.

Expected temporary state:
1. Core flow works; resilience hardening still ongoing.

### Phase 7: Performance and Scalability Tuning
1. Separate autoscaling for API and worker deployments.
2. Tune:
   - RabbitMQ prefetch
   - consumer concurrency
   - retry backoff parameters
3. Remove internal checkout-path REST chaining and gateway hairpin paths.
4. Run progressive load stages (10k, 25k, 50k concurrent).

Expected temporary state:
1. Functional behavior stable; performance still being optimized.

### Phase 8: Final Hardening and Compliance
1. Complete end-to-end failure testing and consistency verification.
2. Ensure all external endpoints behave per assignment expectations.
3. Publish operational runbooks:
   - stuck saga
   - DLQ drain
   - worker crash recovery
4. Lock deployment manifests and documentation.

Expected final state:
1. Contract-compatible API.
2. Event-driven internal architecture.
3. High fault tolerance for service crashes mid-handling.

## Failure Model and Handling Rules
1. Service dies before ACK:
   - Message is redelivered.
   - Inbox/effects dedupe prevents duplicate business effects.
2. Service dies after commit but before publish:
   - Reconciliation detects stale saga state and resumes next step.
3. Compensation message retried multiple times:
   - Compensation handlers are idempotent and safe to replay.
4. Payment dies after receiving rollback but before commit:
   - No ACK before commit means redelivery.
   - Handler checks dedupe key and applies refund exactly once effect.

## Test Plan and Acceptance Criteria
1. Correctness:
   - No negative stock caused by duplicate deliveries.
   - No double charge on duplicate checkout/retries.
2. Crash tolerance:
   - Kill stock/payment/order worker during active checkout.
   - System converges to consistent terminal state.
3. Replay safety:
   - Force redelivery and ensure no duplicate effect.
4. Throughput and latency:
   - Measure p95/p99 and queue backlog at staged concurrency.
5. Contract compatibility:
   - Existing API tests pass without endpoint changes.
6. DLQ handling:
   - Poison message routes to DLQ and replay tool works safely.

## Out of Scope (Given Agreed Constraints)
1. Database crash recovery design (explicitly out, because DB is assumed always available and stable).
2. Schema version negotiation (not required).
3. Feature-flagged migration toggles (not required).
4. Full “exactly-once delivery” semantics (at-least-once + idempotent effects chosen instead).

## Definition of Done
1. External API remains unchanged and benchmark-compatible.
2. Checkout path is fully event-driven through RabbitMQ.
3. Service crashes mid-transaction are recoverable without money/stock inconsistency.
4. Idempotency and reconciliation are implemented and validated.
5. Performance/scaling tests demonstrate efficient elasticity under varying load.

## Plan Update Protocol (Required During Implementation)
Use this protocol whenever unexpected issues arise or implementation opinion changes.

### Replan Triggers
1. A phase cannot meet its exit criteria with the planned approach.
2. New failure behavior appears in tests/chaos runs/load tests.
3. New evidence shows a material scalability or fault-tolerance gap.
4. The team intentionally changes an earlier architectural decision.

### Mandatory Update Steps
1. Add a change-log entry under `## Plan Change Log` with:
   - date,
   - phase impacted,
   - issue observed,
   - decision made,
   - rationale and tradeoff.
2. Update affected phase sections:
   - tasks,
   - expected temporary state,
   - exit criteria.
3. Update `Test Plan and Acceptance Criteria` to cover the new risk.
4. Record whether assumptions changed; if yes, update `Final Decisions and Constraints We Agreed On`.

### Guardrails While Replanning
1. Do not silently change external API behavior.
2. Keep 2xx/4xx semantics unless explicitly re-approved.
3. Keep the "DB always available/stable" assumption unless explicitly re-approved.
4. Prefer minimal safe deltas to get back to a working migration path.

## Plan Change Log
1. 2026-02-16: Added explicit replanning protocol and change-log requirements for handling unexpected issues and design-opinion changes.
