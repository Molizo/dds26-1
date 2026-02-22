# Phase 1 Implementation Plan: Dual-Mode Checkout (Saga + App-Level 2PC)

## Summary

This plan delivers Phase 1 of `assignment.md` by keeping the same three services (`order`, `payment`, `stock`) and adding a startup protocol toggle:

- `CHECKOUT_PROTOCOL=saga`
- `CHECKOUT_PROTOCOL=2pc`

The system is restarted when switching modes. No runtime migration between modes is required.

Primary optimization target is checkout correctness and resilience under failures, with consistency prioritized over throughput when tradeoffs arise.

## Locked Design Decisions

1. 2PC model: App-level 2PC (prepare/commit/abort), not XA.
2. Checkout API contract: synchronous final `2xx/4xx` result.
3. In-doubt policy for 2PC: presumed abort.
4. Canonical transaction record location: `order` Redis DB.
5. Locking model: per-order lock plus deterministic item ordering.
6. Internal protocol endpoints: internal-only service routes.
7. Recovery policy: persistent state plus background recovery.
8. Consistency priority: consistency first over performance.
9. Recovery target: eventual stabilization within minutes after failure.
10. Duplicate checkout behavior: idempotent `200` no-op if already paid.
11. Request-time healing scope: only `2PC` commit-fence finalization may block/retry up to 30s.
12. Active in-progress checkout policy: return `409` immediately; do not sleep/retry or force recovery in request path.
13. Long-term serialization strategy: keep Phase 1 per-order locking; move to global serializable scheduling in a later phase.

## Scope (Phase 1 Only)

In scope:

- Protocol toggle in current three-service architecture.
- Saga and app-level 2PC checkout implementations.
- Crash recovery and consistency repair.
- Fault tolerance under single-container failure and restart.
- Test coverage for checkout hotpath and failure scenarios.

Out of scope:

- Phase 2 standalone orchestrator service abstraction.
- Runtime protocol switching without restart.
- Backward compatibility migrations across protocol modes.

## Current State Gaps To Close

Current checkout in `order` is a direct subtract/pay flow with rollback, but lacks durable transaction state and idempotent duplicate protection:

- `order` re-runs stock/payment on duplicate checkout attempts.
- No explicit `prepare/commit/abort` state model for 2PC.
- No canonical transaction replay mechanism for interrupted checkouts.

## Public API Impact

External API paths and semantics remain unchanged:

- `/orders/create/{user_id}`
- `/orders/find/{order_id}`
- `/orders/addItem/{order_id}/{item_id}/{quantity}`
- `/orders/checkout/{order_id}`
- `/payment/*`
- `/stock/*`

Behavioral clarifications:

- Duplicate `/orders/checkout/{order_id}` for already-paid order returns `200` with no side effects.
- Business failures remain `4xx`.

No new externally documented public endpoints are introduced.

## Internal Interfaces To Add

Internal-only protocol routes are added behind service-to-service communication (not public docs/contracts).

Payment internal routes:

- `POST /internal/tx/prepare_pay/{tx_id}/{user_id}/{amount}`
- `POST /internal/tx/commit_pay/{tx_id}`
- `POST /internal/tx/abort_pay/{tx_id}`

Stock internal routes:

- `POST /internal/tx/prepare_subtract/{tx_id}/{item_id}/{amount}`
- `POST /internal/tx/commit_subtract/{tx_id}`
- `POST /internal/tx/abort_subtract/{tx_id}`

Order internal state/recovery routes (optional, internal use):

- `POST /internal/tx/recover/{tx_id}`
- `POST /internal/recovery/scan`

Guideline:

- Internal routes must be idempotent by `tx_id`.
- Internal operations must not rely on in-memory-only state.

## Data Model Additions

### Order DB (Canonical Transaction Record)

Store one transaction document per checkout `tx_id`:

- `tx_id`
- `order_id`
- `protocol` (`saga` or `2pc`)
- `status`:
  - `INIT`
  - `STOCK_PREPARED`
  - `PAYMENT_PREPARED`
  - `COMMITTING`
  - `COMMITTED`
  - `ABORTING`
  - `ABORTED`
  - `COMPENSATING`
  - `COMPENSATED`
  - `FAILED_NEEDS_RECOVERY`
- participant step results and timestamps
- retry counters and last error
- deterministic item aggregate snapshot used for this tx

### Payment/Stock DB (Participant Idempotency + Prepared State)

Each participant keeps tx-indexed state for idempotency and recovery:

- `tx_id`
- target entity IDs and amounts
- `prepared` flag
- `committed` flag
- `aborted` flag
- expiry/TTL metadata for stale prepared entries

Guideline:

- Participant record writes are durable before acknowledgment.
- Duplicate `prepare`, `commit`, `abort` calls return the same terminal meaning without side effects.

## Step-by-Step Implementation Plan

## Step 1: Add protocol config and startup validation

Tasks:

- Add `CHECKOUT_PROTOCOL` env var support in `order`.
- Validate allowed values (`saga`, `2pc`) on startup.
- Log active mode clearly.

Acceptance criteria:

- Service fails fast on invalid value.
- Switching value requires restart and changes checkout path behavior.

Guidelines:

- Keep default explicit (`saga` unless overridden).

## Step 2: Add checkout preconditions and idempotency guard

Tasks:

- Validate order exists and belongs to existing user.
- Reject invalid line quantities and malformed amounts before protocol execution.
- If `order.paid == True`, return `200` idempotent success.
- Acquire per-order lock for checkout.

Acceptance criteria:

- Duplicate checkout never triggers additional payment/stock changes.
- Same-order concurrent checkouts yield one logical commit.

Guidelines:

- Lock key should be order-scoped and short-lived with renewal while processing.

## Step 3: Add canonical transaction store in `order`

Tasks:

- Create tx records before first participant call.
- Record every participant response and transition atomically.
- Persist deterministic item-quantity aggregate snapshot.

Acceptance criteria:

- Any in-flight checkout has a durable tx record.
- Restart can reconstruct next action from tx state only.

Guidelines:

- Use monotonic transitions and reject invalid backward transitions.

## Step 4: Implement Saga checkout path

Flow:

1. Create tx record (`INIT`).
2. Reserve/apply stock step by step in deterministic item ID order.
3. Apply payment debit.
4. Mark order paid.
5. Mark tx terminal success.
6. On failure, run compensations in reverse persisted order.

Acceptance criteria:

- Success path changes exactly once.
- Any failure leaves final invariant-safe state (paid false, no net stock/payment loss).

Guidelines:

- Compensation operations are idempotent.
- Compensation progress is persisted after each step.

## Step 5: Implement app-level 2PC checkout path

Flow:

1. Create tx record (`INIT`).
2. `prepare` stock participant(s).
3. `prepare` payment participant.
4. If all prepare `YES`: issue commits, then mark order paid.
5. If any prepare fails: issue aborts for all prepared participants.
6. Persist terminal outcome.

Acceptance criteria:

- No participant commits without globally committed decision.
- Prepared entries are recoverable after crash.

Guidelines:

- Presumed abort: if coordinator decision missing on recovery, abort prepared branches.

## Step 6: Participant internal route implementation

Tasks:

- Add internal prepare/commit/abort handlers in payment and stock.
- Persist tx participant state before returning success.
- Add TTL + sweeper for stale prepared entries.

Acceptance criteria:

- Repeated internal calls with same `tx_id` are safe and deterministic.
- Participant restart does not lose prepared/commit/abort knowledge.

Guidelines:

- Keep participant records compact and keyed for fast lookup by `tx_id`.

## Step 7: Recovery engine and background worker

Tasks:

- On service startup, scan non-terminal tx records and resume.
- Add periodic recovery scan loop.
- For request path: allow bounded block/retry only for `2PC` commit-fence finalization.
- For request path: return `409` immediately when an active non-terminal checkout tx already exists.
- Continue background recovery until terminal consistency.

Acceptance criteria:

- After container kill and restart, stranded txs converge to terminal state automatically.
- No manual intervention needed for common crash scenarios.

Guidelines:

- Distinguish transient transport errors from business failures.
- Exponential backoff with jitter for recovery retries.
- Run the recovery worker inside `order-service`; no separate container type is required for Phase 1.
- If multiple `order-service` replicas are running, enforce single-active recovery execution using a distributed leader lock (for example Redis lock with lease and renewal).
- Avoid sleep-based healing loops for active non-terminal checkout txs in request handlers; this reduces lock-lease race risk and keeps mutual exclusion simple.

## Database failure handling strategy

Request-time behavior:

1. If a Redis operation fails during checkout, treat that step as unknown or incomplete, never as committed success.
2. If `2PC` commit fence is present, attempt bounded inline finalization retries for up to 30 seconds.
3. If an active non-terminal checkout tx exists, return `409` immediately instead of sleeping/polling.
4. If unresolved after bounded fence finalization, return `4xx` and persist or re-persist tx state as `FAILED_NEEDS_RECOVERY` at first available durable write.
5. Continue recovery asynchronously until terminal tx state is reached.

Participant durability rules:

1. Payment and stock must persist participant tx state before acknowledging `prepare`, `commit`, or `abort`.
2. Participant handlers are idempotent by `tx_id`; replay after DB restart must be a safe no-op or deterministic continuation.
3. For 2PC, participants restarting in `PREPARED` state must query coordinator decision; if no durable decision exists, apply presumed abort.

Recovery after DB restart:

1. Startup scanner resumes all non-terminal tx records.
2. Periodic scanner reconciles txs that were interrupted by DB outages.
3. Saga resumes forward or compensation from last durable step.
4. 2PC resolves in-doubt branches using durable decision or presumed abort.
5. Reconciliation verifies invariants (`paid`, stock, credit) and repairs mismatch by replaying idempotent protocol actions.

Operational safeguards:

1. Exponential backoff plus jitter on DB reconnect.
2. Short circuit or circuit-breaker behavior during hard DB outage to reduce request storms.
3. Optional stale prepared cleanup with TTL only when it does not violate tx semantics.

Acceptance criteria:

1. DB outage during checkout never causes silent commit ambiguity.
2. After DB restart, all affected txs converge to terminal states without manual intervention.
3. No net money or stock loss from DB outage scenarios.

## Step 8: Concurrency and atomicity hardening

Tasks:

- Deterministic sorted item handling in checkout and compensation.
- Atomic check-and-update operations for stock and payment updates.
- Ensure item-level contention does not oversell.

Acceptance criteria:

- Concurrent inverse-order checkouts do not deadlock and preserve totals.
- No oversubtraction/overdebit under concurrent requests.

Guidelines:

- Prefer Redis atomic constructs over broad locks.

## Step 9: Test matrix across both modes

Tasks:

- Run existing integration suite in `saga` mode.
- Restart stack, run same suite in `2pc` mode.
- Add mode-specific recovery tests.

Acceptance criteria:

- Hotpath tests pass in both modes.
- Mode switch requires only restart + env change.

Guidelines:

- Keep external tests mode-agnostic where possible.

## Step 10: Fault-injection and recovery validation

Scenarios:

- Kill stock during checkout before participant acknowledgment.
- Kill payment after prepare but before commit/abort.
- Kill order coordinator mid-decision.
- Kill `stock-db` during reserve or commit windows.
- Kill `payment-db` during prepare or commit windows.
- Kill `order-db` during decision persistence.
- Restart killed service and verify convergence.
- Restart killed database and verify convergence.
- Sequential failure chain: fail one container, recover, then fail another container.

Acceptance criteria:

- Final state always satisfies money/stock/order invariants.
- No tx remains permanently non-terminal.

## Step 11: Performance/consistency comparison report

Tasks:

- Measure checkout latency and throughput for both modes.
- Document protocol-specific failure behavior and recovery timelines.

Acceptance criteria:

- One concise table compares `saga` vs `2pc` on correctness/performance tradeoffs.

## Invariants and Validation Rules

Invariants:

1. No money creation/loss from checkout failures.
2. No stock creation/loss from checkout failures.
3. `order.paid == true` iff checkout transaction committed successfully.
4. Duplicate checkout never causes second charge/subtract.

Validation rules:

- Negative quantities/amounts are rejected (`4xx`) with no side effects.
- Unknown user/order/item is rejected (`4xx`) with no side effects.

## Test Cases and Scenarios

Core correctness:

1. Happy path checkout commits once.
2. Out-of-stock causes full rollback/abort.
3. Insufficient credit causes full rollback/abort.
4. Duplicate checkout returns `200` no-op.
5. Invalid order ID has no side effects.

Concurrency:

1. Two simultaneous checkouts on same order: one logical commit.
2. Two conflicting orders with inverse item ordering: no deadlock, bounded completion.
3. Parallel non-conflicting checkouts both succeed quickly.

Failure/recovery:

1. Saga compensation interrupted by crash and resumed on restart.
2. 2PC coordinator crash with prepared participants resolves via presumed abort.
3. Participant crash before ACK recovers idempotently.
4. DB outage during prepare/commit/compensation converges after DB restart.
5. Sequential service or DB failures still converge without invariant break.
6. Background recovery converges stranded txs to terminal state.

Mode toggle:

1. Full suite passes with `CHECKOUT_PROTOCOL=saga`.
2. Full suite passes with `CHECKOUT_PROTOCOL=2pc`.
3. No code or schema migration required between runs.

## Rollout Plan

1. Implement shared transaction substrate and idempotency guards first.
2. Implement saga path and pass baseline tests.
3. Implement 2PC path and internal participant tx routes.
4. Enable recovery worker and failure tests.
5. Run dual-mode test matrix and benchmark.
6. Freeze behavior and document final tradeoffs.

## Benefits and Tradeoffs

Saga:

- Benefits: better availability, simpler participant semantics, generally lower lock hold times.
- Tradeoffs: compensation complexity and eventual consistency windows.

App-level 2PC:

- Benefits: clearer atomic decision boundary and stronger commit semantics.
- Tradeoffs: coordinator/participant in-doubt handling complexity and higher blocking risk.

## Assumptions and Defaults

1. Stack restarts between protocol mode switches.
2. Background recovery is allowed and expected in grading context for consistency.
3. Internal protocol endpoints are not part of external API contract.
4. Single-container failures are primary fault model for Phase 1.
5. Container failures include both service containers and Redis database containers.
6. Checkout request may block up to 30s only for `2PC` commit-fence finalization; active in-progress checkout returns `409` immediately.
7. Recovery continues asynchronously after request failure until terminal state.
8. Recovery worker is embedded in `order-service`; with multiple order replicas, only one instance may hold the recovery leader lock at a time.

## Future Serialization Roadmap

This Phase 1 design keeps per-order mutual exclusion because checkout still performs distributed side effects (`stock`, `payment`, `order`) without a global scheduler.

- Why mutual exclusion remains in Phase 1:
  - Prevents two concurrent checkout coordinations for the same order from racing through non-atomic side effects.
  - Preserves single-logical-commit behavior under retries and partial failures.
- Why request-time sleep/poll is avoided for active tx:
  - It adds lock-lease race risk and complexity in synchronous request handlers.
  - Background recovery is a safer place to perform long-running healing.
- Planned Phase 2+ direction:
  - Introduce a global serializable scheduler/orchestrator that can centralize ordering/serialization.
  - Replace ad-hoc per-order request-path coordination with scheduler-driven commit ordering.
