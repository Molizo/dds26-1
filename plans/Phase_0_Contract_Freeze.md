# Phase 0 Contract Freeze and Work Breakdown

## Purpose
This document is the Phase 0 implementation artifact referenced by `plans/Final_Redis_RabbitMQ_Implementation_Plan.md`.
It freezes external behavior, captures Saga state/failure transitions, and defines execution order for Phases 1-8.

## Date and Scope
- Date: 2026-02-16
- Scope: Phase 0 only (documentation/specification; no runtime behavior changes)

## Non-Negotiables (Frozen)
1. External endpoints and methods remain unchanged:
   - `/orders/*`, `/stock/*`, `/payment/*`
2. Existing benchmark-visible response JSON fields remain unchanged.
3. Success/failure class semantics remain unchanged:
   - Success is `2xx`
   - Failure is `4xx`
4. Redis remains the system of record.
5. Internal workflow remains at-least-once with idempotent handlers.
6. Checkout remains externally synchronous in behavior.

## Frozen External Contract

### Order Service
| Route | Method | Success | Failure | Response Shape |
|---|---|---|---|---|
| `/orders/create/{user_id}` | `POST` | `2xx` | `4xx` | JSON with `order_id` |
| `/orders/find/{order_id}` | `GET` | `2xx` | `4xx` | JSON with `order_id`, `paid`, `items`, `user_id`, `total_cost` |
| `/orders/addItem/{order_id}/{item_id}/{quantity}` | `POST` | `2xx` | `4xx` | Text response; no schema change |
| `/orders/checkout/{order_id}` | `POST` | `200` on committed checkout | `4xx` on stock/payment/timeout/request failures | Text response; no schema change |
| `/orders/batch_init/{n}/{n_items}/{n_users}/{item_price}` | `POST` | `2xx` | `4xx` | JSON with `msg` |

### Stock Service
| Route | Method | Success | Failure | Response Shape |
|---|---|---|---|---|
| `/stock/item/create/{price}` | `POST` | `2xx` | `4xx` | JSON with `item_id` |
| `/stock/find/{item_id}` | `GET` | `2xx` | `4xx` | JSON with `stock`, `price` |
| `/stock/subtract/{item_id}/{amount}` | `POST` | `2xx` | `4xx` | Text response; no schema change |
| `/stock/add/{item_id}/{amount}` | `POST` | `2xx` | `4xx` | Text response; no schema change |
| `/stock/batch_init/{n}/{starting_stock}/{item_price}` | `POST` | `2xx` | `4xx` | JSON with `msg` |

### Payment Service
| Route | Method | Success | Failure | Response Shape |
|---|---|---|---|---|
| `/payment/create_user` | `POST` | `2xx` | `4xx` | JSON with `user_id` |
| `/payment/find_user/{user_id}` | `GET` | `2xx` | `4xx` | JSON with `user_id`, `credit` |
| `/payment/pay/{user_id}/{amount}` | `POST` | `2xx` | `4xx` | Text response; no schema change |
| `/payment/add_funds/{user_id}/{amount}` | `POST` | `2xx` | `4xx` | Text response; no schema change |
| `/payment/batch_init/{n}/{starting_money}` | `POST` | `2xx` | `4xx` | JSON with `msg` |

## Checkout Behavior Freeze
1. API endpoint remains `POST /orders/checkout/{order_id}`.
2. Internal implementation migrates to Saga orchestration, but external call behavior remains synchronous.
3. Timeout decision (frozen):
   - If Saga does not reach a terminal state within bounded wait, endpoint returns `400`.
4. Initial timeout default for implementation:
   - `CHECKOUT_WAIT_TIMEOUT_MS = 3000` (tunable in later performance phase).
5. Failure reasons all map to `4xx`:
   - Insufficient stock
   - Insufficient credit
   - Saga timeout
   - Internal dependency/request failure

## Saga Transition Matrix (Frozen for Implementation)

### State Set
- `PENDING`
- `RESERVING_STOCK`
- `STOCK_RESERVED`
- `STOCK_REJECTED`
- `CHARGING_PAYMENT`
- `PAYMENT_CHARGED`
- `PAYMENT_REJECTED`
- `RELEASING_STOCK`
- `COMPENSATED`
- `COMMITTING_ORDER`
- `COMPLETED`
- `FAILED`

### Transition Rules
| From State | Trigger/Event | Guard/Condition | To State | Required Side Effect | Retry / Failure Rule |
|---|---|---|---|---|---|
| `PENDING` | `CheckoutRequested` accepted | order exists, not paid | `RESERVING_STOCK` | publish `ReserveStock` | redelivery safe via inbox dedupe |
| `RESERVING_STOCK` | `StockReserved` | stock reserved for all items | `STOCK_RESERVED` | publish `ChargePayment` | duplicate `StockReserved` ignored by effects key |
| `RESERVING_STOCK` | `StockRejected` | any item reserve failed | `STOCK_REJECTED` | publish `OrderFailed` | retries stay in failed branch |
| `STOCK_RESERVED` | `ChargePayment` dispatched | command accepted | `CHARGING_PAYMENT` | wait for payment result event | redelivery safe via dedupe |
| `CHARGING_PAYMENT` | `PaymentCharged` | charge committed | `PAYMENT_CHARGED` | publish `OrderCommitted` | duplicate charge result ignored |
| `CHARGING_PAYMENT` | `PaymentRejected` | insufficient credit or payment reject | `PAYMENT_REJECTED` | publish `ReleaseStock` | retries remain compensation path |
| `PAYMENT_REJECTED` | `ReleaseStock` dispatched | command accepted | `RELEASING_STOCK` | wait release confirmation | redelivery safe via dedupe |
| `RELEASING_STOCK` | release application confirmed | release committed | `COMPENSATED` | publish `OrderFailed` | duplicate release ignored |
| `PAYMENT_CHARGED` | `OrderCommitted` | order state write succeeds | `COMMITTING_ORDER` | set `order.paid=true`, `checkout_status=COMPLETED` | if crash before publish, reconciliation resumes |
| `COMMITTING_ORDER` | commit finalized | state persisted | `COMPLETED` | terminal success marker | terminal idempotent |
| `STOCK_REJECTED` | `OrderFailed` | failure persisted | `FAILED` | set terminal failed reason `stock_rejected` | terminal idempotent |
| `COMPENSATED` | `OrderFailed` | failure persisted | `FAILED` | set terminal failed reason `payment_rejected` | terminal idempotent |
| any non-terminal | timeout exceeded | `now - updated_at > CHECKOUT_WAIT_TIMEOUT_MS` | `FAILED` | set terminal failed reason `timeout` | no further business effects |

### Idempotency and Atomicity Rules
1. Every handler checks `inbox:{service}:{message_id}` before applying business effect.
2. Every business effect writes an effects marker:
   - `effects:{service}:{entity_id}:{step}`
3. ACK only after:
   - Redis state mutation committed
   - inbox/effects marker committed
   - next event publish persisted (or outbox entry persisted for replay)
4. Duplicate deliveries:
   - Must return success path for broker ACK behavior without repeating stock/payment impact.

## Phase Run-Order Checklist (Phases 1-8)

### Phase 1: RabbitMQ Infrastructure in Helm/K8s
- Entry criteria:
  - This Phase 0 document merged.
- Tasks:
  - Add RabbitMQ chart/values and durable topology.
  - Add retry queues and DLQ routing policies.
  - Add worker deployment manifests for order/stock/payment.
- Exit criteria:
  - RabbitMQ reachable from all services.
  - All required queues/exchanges present and durable.

### Phase 2: Internal Contracts and Libraries
- Entry criteria:
  - RabbitMQ infrastructure operational.
- Tasks:
  - Add shared message model and serializer.
  - Enforce required metadata fields.
  - Add correlation logging helpers.
- Exit criteria:
  - Producer/consumer stubs exchange valid test messages.
  - Invalid messages are rejected deterministically.

### Phase 3: Redis Idempotency and Atomicity Layer
- Entry criteria:
  - Shared message model integrated in services.
- Tasks:
  - Add inbox/effects key strategy.
  - Implement Lua scripts for reserve/release/charge/refund.
  - Add reusable process-and-ack helper contract.
- Exit criteria:
  - Duplicate message replay does not duplicate stock/payment effects.
  - Atomic check-update-mark behavior verified.

### Phase 4: Worker Runtime per Service
- Entry criteria:
  - Atomic primitives available.
- Tasks:
  - Implement consumer loops and dispatch.
  - Implement ACK discipline and retry handling.
  - Implement DLQ routing behavior.
- Exit criteria:
  - Worker restart and redelivery paths converge safely.
  - Poison messages land in DLQ.

### Phase 5: Checkout Orchestration Cutover
- Entry criteria:
  - Worker loops stable for command flow.
- Tasks:
  - Replace sync checkout internals with Saga orchestration.
  - Aggregate quantities by `item_id`.
  - Persist `PENDING` before publish.
  - Wait bounded time and map terminal result to `200` or `4xx`.
- Exit criteria:
  - External checkout endpoint unchanged.
  - Known success/failure scenarios pass with Saga backend.

### Phase 6: Reconciliation and Crash Recovery
- Entry criteria:
  - Saga cutover active.
- Tasks:
  - Add stale `PENDING` scanner.
  - Add startup recovery for incomplete saga flow.
  - Add safe DLQ replay tooling.
- Exit criteria:
  - Crash mid-flow converges to consistent terminal state.
  - Replay tooling is idempotent.

### Phase 7: Performance and Scalability Tuning
- Entry criteria:
  - Core reliability behavior stable.
- Tasks:
  - Tune prefetch/concurrency/retry backoff.
  - Scale API vs worker deployments independently.
  - Remove internal gateway hairpin dependencies in checkout path.
- Exit criteria:
  - Latency and queue backlog are acceptable at staged load.
  - No consistency regressions under load.

### Phase 8: Final Hardening and Compliance
- Entry criteria:
  - Performance baseline captured.
- Tasks:
  - Execute crash/failure matrix validation.
  - Validate all contract compatibility requirements.
  - Publish operational runbooks.
- Exit criteria:
  - Assignment contract and consistency requirements met.
  - Deployment and runbook documents finalized.

## Phase 0 Acceptance Checklist
1. Contract table covers all currently exposed routes in `order/app.py`, `stock/app.py`, and `payment/app.py`.
2. Status-class behavior is frozen as `2xx` success and `4xx` failure.
3. Timeout failure policy is frozen as checkout `400`.
4. Saga transitions and compensation paths are explicit.
5. Phase ordering has entry and exit criteria for each phase.
