# Phase 1 Rebuild Plan: Embedded Coordinator with Clean Extraction Path

## Purpose

This plan defines a clean rebuild of Phase 1 for the `assignment.md` requirements:

- implement both SAGA and app-level 2PC in Flask + Redis
- keep the external API unchanged
- remain recoverable under single-container failures
- keep the Phase 1 implementation easy to split into a standalone orchestrator in Phase 2

The central design choice is:

- keep the coordinator deployed inside the `order` service for Phase 1
- keep the coordinator implemented as a separate code boundary from Flask routes and order-domain persistence

This preserves the Phase 1 deliverable shape while avoiding the coupling problem seen in `phase-1-attempt-2`.

## Assignment Alignment

This rebuild treats `assignment.md` as the source of truth.

Required outcomes:

1. External routes remain unchanged:
   - `/orders/*`
   - `/stock/*`
   - `/payment/*`
2. Success responses remain in `2xx`; failures remain in `4xx`.
3. Both SAGA and 2PC must exist in the same codebase.
4. Phase 1 may keep protocol logic embedded in the application.
5. Phase 2 should be able to move that logic into a separate orchestrator with minimal rewrite.
6. Backward compatibility for stored data is not required because deployments start from a clean slate.

## Goals

1. Guarantee checkout consistency:
   - no money loss
   - no stock loss
   - no double-charge
   - no double-subtract
2. Keep public behavior benchmark-compatible.
3. Make protocol logic testable without Flask route machinery.
4. Make recovery explicit and durable.
5. Avoid overbuilding Phase 2 infrastructure before Phase 1 is correct.
6. Use a concurrency model that is simple to recover after crashes.
7. Maximize request throughput on the benchmark-visible hot path without violating consistency.
8. Distinguish cheap no-op throughput from real committed checkout throughput.

## Non-Negotiables

1. No public API path changes.
2. No protocol-specific data leaks into public response schemas.
3. No Flask `abort()` calls inside protocol logic.
4. No protocol module should directly construct Flask `Response` objects.
5. No long polling loops in request handlers while holding lease locks.
6. No duplicate copies of finalization or recovery logic.
7. Redis remains the system of record for Phase 1.
8. True XA is out of scope; 2PC is application-managed.
9. App-level 2PC uses reservation-based prepared holds at participants, not timestamp ordering as the primary concurrency policy.
10. Transaction state must not rely on summary flags alone; every checkout transaction also records an append-only step log.
11. No message bus is used on the Phase 1 checkout critical path.
12. `POST /orders/checkout/{order_id}` returns `200` as an idempotent no-op when the order is already safely paid.
13. Breaking storage schemas is acceptable during the rebuild because this is a proof-of-concept/MVP and production starts with empty state.
14. Protocol mode is deployment-wide; all services in one running stack use the same transaction mode.

## Explicit Decision Record

This section records the main architectural decisions that were debated, the alternatives considered, and the reason each final choice was made.

### Decision: Keep the coordinator embedded in `order` for Phase 1, but isolated in code

Chosen:

- deploy the coordinator inside the `order` service for Phase 1
- implement it as a separate application-service boundary that does not depend on Flask

Why:

1. This matches the Phase 1 deliverable shape in `assignment.md`.
2. It avoids prematurely building Phase 2 infrastructure.
3. It directly addresses the main structural flaw in `phase-1-attempt-2`, where route code, protocol logic, and recovery were tangled.

Alternative considered:

- build the orchestrator as a separate service already in Phase 1

Why we are not doing that now:

1. It increases deployment and failure-surface complexity before the current rubric is satisfied.
2. It shifts effort from correctness to plumbing.
3. Attempt 1 already demonstrated the risk of solving a later phase first.

### Decision: Breaking the data model is acceptable during the rebuild

Chosen:

- allow schema and key-layout changes that are incompatible with earlier iterations

Why:

1. This project is being treated as a proof-of-concept/MVP.
2. The deployment model assumes a clean slate:
   - empty Redis state
   - no required in-place migration
   - no requirement to preserve old development data
3. Preserving compatibility with every prior experiment would add migration code in the most failure-sensitive part of the system for no Phase 1 benefit.

Alternative considered:

- preserve backward compatibility with previous Redis schemas and transaction formats

Why we are not doing that:

1. It adds migration logic and compatibility branches that the course deliverable does not require.
2. It slows down iteration while we are still refining the core protocol design.
3. It makes the system harder to simplify as we learn from failures.

Guardrail:

- schema-breaking changes are acceptable, but the current chosen schema must still be documented, internally coherent, and stable enough to test properly

### Decision: Support both SAGA and app-level 2PC behind one startup flag

Chosen:

- `CHECKOUT_PROTOCOL=saga`
- `CHECKOUT_PROTOCOL=2pc`

Why:

1. The course requires both mechanisms in Phase 1.
2. A startup flag keeps the public API stable.
3. It allows protocol comparison without creating two separate applications.

Deployment rule:

- one running stack uses one protocol mode at a time
- there is no mixed deployment where some services run SAGA behavior while others run 2PC behavior

Alternative considered:

- support only SAGA

Why we are not doing that:

1. Attempt 1 already showed that implementing only one mode leaves the deliverable incomplete.
2. It makes it impossible to discuss tradeoffs concretely during evaluation.

Alternative considered:

- expose protocol choice as a public request parameter

Why we are not doing that:

1. It leaks internal orchestration details into the public contract.
2. It complicates testing and benchmarking.
3. The assignment does not require protocol selection to be a client-visible behavior.

Alternative considered:

- mixed-mode deployments where different services use different transaction semantics at the same time

Why we are not doing that:

1. It multiplies the failure matrix without adding useful Phase 1 learning.
2. It makes transaction semantics harder to reason about and defend.
3. The deployment model for this project is explicitly uniform: the whole stack runs one mode per deployment.

### Decision: Keep the checkout critical path synchronous and use direct internal HTTP

Chosen:

- internal service-to-service HTTP on the checkout hot path
- no message bus on the Phase 1 critical path

Why:

1. The public API must return a synchronous final `2xx/4xx` result.
2. 2PC still needs a synchronous logical decision point even if a queue is used as transport.
3. Queue hops add broker latency, scheduling delay, and reply coordination overhead.
4. For a latency-sensitive synchronous checkout path, that complexity hurts more than it helps.

Alternative considered:

- use a message bus for SAGA and 2PC

Why we are not doing that now:

1. A message bus is a natural fit for SAGA, but not for 2PC's synchronous vote/decision/finalization lifecycle.
2. It can improve replayability and burst absorption, but it usually worsens tail latency on a synchronous request path.
3. It recreates the risk from attempt 1: building a larger asynchronous platform before Phase 1 correctness is stable.

Important nuance:

- a message bus is still a valid future Phase 2 or post-Phase-2 transport option, especially for async repair, retries, or higher-difficulty SAGA variants
- it is intentionally excluded from the Phase 1 checkout hot path, not declared universally bad

### Decision: Use reservation-based strict 2PL semantics for 2PC participants

Chosen:

- `prepare` creates durable holds:
  - stock reservations
  - payment funds holds
- `commit` consumes those holds
- `abort` releases those holds

Why:

1. This aligns directly with the `prepare -> decision -> commit/abort` lifecycle.
2. The participant state is explicit and easy to recover after crashes.
3. It is easier to explain and defend than more abstract conflict-resolution schemes.

Alternative considered:

- timestamp ordering via a Redis atomic counter

Why we are not using that as the primary mechanism:

1. It is a concurrency-control policy, not a natural representation of 2PC prepared state.
2. It tends to turn conflicts into abort-and-retry behavior, which is harder to reason about under synchronous latency constraints.
3. It makes crash recovery less explicit because durable holds are clearer than inferred ordering decisions.

Allowed secondary use of counters:

- monotonic transaction IDs
- event sequence numbers
- diagnostics and ordering within the tx log

### Decision: Use a hybrid state model, not flags-only and not full event sourcing

Chosen:

- one compact canonical tx summary record for fast-path reads
- one append-only per-transaction step log for auditability and recovery reconstruction

Why:

1. Summary records are efficient for the checkout hot path.
2. The step log preserves the history needed for debugging and ambiguous crash recovery.
3. This retains most of the benefits that prompted the event-sourcing discussion without forcing a full event-sourced architecture.

Alternative considered:

- keep only summary flags/fields

Why we are not doing that:

1. Attempt 2 already exposed how compressed state can hide important recovery distinctions.
2. It makes rollback and compensation bugs harder to understand and prove correct.

Alternative considered:

- full event sourcing for all state across services

Why we are not doing that:

1. It would require rebuilding the services around event replay semantics instead of simple materialized state.
2. It adds substantial complexity to reads, testing, and invariants.
3. It is unnecessary for Phase 1 and would again risk solving the wrong problem first.

### Decision: Keep recovery primarily in the background, not in request-time healing loops

Chosen:

- a background recovery worker embedded in `order`
- bounded request-time healing only for the narrow 2PC commit-fence case

Why:

1. Attempt 2's design log already identified the lock-lease race created by long request-time sleep/poll loops.
2. Background recovery keeps request latency bounded and easier to reason about.
3. It keeps the "in doubt" case explicit instead of hiding it behind long request hangs.

Alternative considered:

- aggressive request-time healing/polling for all stuck transactions

Why we are not doing that:

1. It weakens mutual exclusion under lease expiry.
2. It burns worker time on long-lived requests.
3. It directly fights the throughput objective.

### Decision: Keep already-paid checkout as an idempotent `200` and optimize it

Chosen:

- if an order is already safely paid, checkout returns `200` with no new side effects

Why:

1. This is correct idempotent behavior for a repeated operation.
2. It matches the earlier Phase 1 reasoning from attempt 2.
3. It improves benchmark-visible throughput because no-op responses are cheap.

Alternative considered:

- return `4xx` for already-paid orders

Why we are not doing that:

1. It treats a repeated successful operation as an error, which is the wrong API semantics.
2. It would be counted as a failure by the provided Locust scenarios.
3. It reduces headline throughput for no correctness benefit.

### Decision: Track two different throughput metrics and never mix them

Chosen:

- report headline request throughput separately from real committed checkout throughput

Why:

1. The benchmark can be dominated by cheap already-paid `200` responses.
2. A throughput claim is meaningless if it does not state what work was actually performed.
3. This lets us optimize for visible throughput without lying to ourselves about true distributed-work capacity.

Alternative considered:

- report one combined "checkout throughput" number

Why we are not doing that:

1. It hides the difference between cheap no-op requests and real distributed commits.
2. It can make a system appear much faster than it is at useful work.

## Lessons Carried Forward

### What worked in attempt 1

1. Strong focus on idempotency and failure recovery.
2. Explicit transaction thinking instead of best-effort rollback only.
3. Documenting assumptions and operational constraints.

### What failed in attempt 1

1. It solved a later-phase architecture problem first.
2. It relied on RabbitMQ-centered saga orchestration instead of Flask + Redis only.
3. It never delivered the required 2PC implementation.

### What worked in attempt 2

1. Startup protocol toggle (`saga` vs `2pc`).
2. Canonical transaction record in the order-side store.
3. Idempotent participant endpoints keyed by `tx_id`.
4. Per-order lock plus deterministic item ordering.
5. Commit-fence handling for post-decision crashes.
6. Background recovery with bounded request-time healing only where necessary.

### What failed in attempt 2

1. The order service became both domain service and coordinator.
2. Route handlers mixed HTTP, locking, transaction orchestration, and recovery.
3. Protocol modules were tied to Flask and order persistence.
4. 2PC finalization logic was spread across multiple places.
5. Recovery knew too much about protocol internals instead of resuming through one coordinator API.

### Specific failure patterns observed from attempt 2 planning and design notes

These are worth naming explicitly because they shape the rebuild:

1. `FAILED_NEEDS_RECOVERY` was at one point treated as terminal.
   - That released the active transaction guard too early.
   - A new checkout could start while the previous one still needed repair.
2. Request-path healing loops fought the lease-lock model.
   - Long waits under a renewable lease risked losing mutual exclusion.
   - This created a path to duplicate coordinator attempts.
3. The commit-fence case is real and must be modeled explicitly.
   - A 2PC commit can durably happen before the order record is marked paid.
   - This cannot be hand-waved as a rare edge case; it must have a formal recovery path.
4. Recovery behavior became spread across route code, protocol code, and dedicated recovery code.
   - That made correctness depend on multiple copies of the same idea.
5. App-level 2PC on Redis is inherently an "in-doubt" design.
   - There is no XA.
   - Presumed abort is a conscious policy choice, not an incidental behavior.

### Hindsight lessons from commit-by-commit review of `phase-1-attempt-2`

Looking at the branch history from `901934d` through `5bd4c5b` reveals additional blind spots that were not fully obvious in the final code alone.

1. The branch was implemented monolith-first, then refactored, then patched for correctness.
   - `dd99229` and `07e1e17` grew most of the logic directly in large service files.
   - `4d60207` then split the code into smaller files.
   - This means file organization improved after the fact, but the core architecture boundaries were defined too late.
   - Hindsight conclusion:
     - define coordinator, protocol, and adapter boundaries before writing most of the protocol logic
     - do not rely on a later refactor to create the architecture

2. Recovery was still treated as something that could be added after the main protocols.
   - Core protocol behavior landed before the dedicated recovery engine.
   - `8cde8d9` then introduced a large new recovery subsystem.
   - `5bd4c5b` immediately fixed a recovery-state semantics bug.
   - Hindsight conclusion:
     - recovery is not a later hardening phase only
     - the state machine must be designed from the start around "how does this resume after a crash?"

3. The initial state taxonomy was not precise enough.
   - The design did not cleanly separate:
     - request flow ended
     - transaction fully complete
     - safe to allow a new checkout
   - This is the deeper reason `FAILED_NEEDS_RECOVERY` was misclassified.
   - Hindsight conclusion:
     - status names and status groups must be designed around both correctness and re-entry semantics, not just human readability

4. Time-based throttling was mixed with correctness semantics.
   - Earlier iterations used "recent checkout" behavior in ways that could influence duplicate-checkout responses.
   - That blurred the line between:
     - correctness source of truth
     - performance optimization or client-throttle behavior
   - Hindsight conclusion:
     - correctness must come only from durable business and transaction state
     - recency caches may exist only as optional performance hints, never as the authority for duplicate semantics

5. Ambiguous participant outcomes were flattened into boolean-style state too early.
   - On timeouts, the coordinator could conservatively treat a participant as "may have prepared" and then abort.
   - That is safer than assuming "definitely not prepared", but it still compresses:
     - known prepared
     - unknown but possibly prepared
   - Hindsight conclusion:
     - uncertain participant outcomes must be modeled explicitly, or at least treated as a distinct recovery state
     - do not pretend ambiguity is the same as certainty

6. Sequential coordinator writes still left avoidable crash windows.
   - Durable decision, status updates, and commit-fence markers were not always one atomic conceptual transition.
   - Recovery could handle some of this, but the write pattern still created unnecessary small in-between states.
   - Hindsight conclusion:
     - minimize the number of separate durable writes per logical coordinator transition
     - design atomic transition helpers earlier, not after the fact

7. Early design assumed single-replica behavior longer than it should have.
   - Request-path locking existed before replica-safe recovery leadership did.
   - The recovery leader lock came later in `8cde8d9`.
   - Hindsight conclusion:
     - if the service is expected to scale, recovery coordination across replicas must be in the initial model, not retrofitted later

8. Black-box regression tests came before state-machine tests.
   - Early regression tests were strong and useful.
   - But focused recovery unit tests and status-semantics tests arrived later.
   - Hindsight conclusion:
     - end-to-end tests are necessary but insufficient
     - unit tests for transaction states, recovery semantics, and re-entry conditions must be added early

9. Some tests tolerated ambiguity because the desired semantics were not fully frozen yet.
   - Intermediate tests allowed multiple acceptable outcomes in concurrency races.
   - That is practical during exploration, but it can also hide unresolved API or consistency decisions.
   - Hindsight conclusion:
     - be strict early about what is intentionally allowed:
       - already paid is `200`
       - active in-progress may be `409`
       - safe duplicate behavior must be explicit
     - only tolerate multiple outcomes when the design intentionally permits them

10. Item aggregation solved one problem but not the larger performance shape.
    - Aggregating repeated item lines was the right move.
    - But the branch still performed per-item remote participant work after aggregation.
    - Hindsight conclusion:
      - aggregation is necessary for correctness and fairness
      - batching or otherwise reducing per-item remote chatter is still required for throughput and latency

11. The branch often responded to discovered issues with local fixes instead of tightening the underlying model.
    - The sequence of:
      - large regression tests
      - implementation
      - refactor
      - recovery add-on
      - edge-case patch
      suggests the model was being discovered while patching.
    - Hindsight conclusion:
      - when a bug exposes a state-model weakness, update the model and invariants first, then implement the local fix
      - avoid accumulating "patches that work" without strengthening the underlying transaction design

12. The net lesson from attempt 2 is not that it chose the wrong core protocols.
    - The main blind spots were:
      - boundary timing
      - recovery-first thinking
      - state precision
      - test precision
      - performance shape of internal calls
    - Hindsight conclusion:
      - the rebuild should keep the protocol ideas from attempt 2
      - it should correct the modeling and boundary mistakes much earlier in the implementation sequence

## Target Architecture

Phase 1 runtime shape:

- `order` service hosts:
  - public order API
  - embedded coordinator
  - background recovery worker
- `payment` service hosts:
  - public payment API
  - internal transaction participant API
- `stock` service hosts:
  - public stock API
  - internal transaction participant API

Code boundaries:

1. API layer:
   - Flask routes only
   - input validation
   - result-to-HTTP mapping
2. Coordinator layer:
   - protocol selection
   - transaction state transitions
   - recovery entrypoints
   - no Flask imports
3. Protocol layer:
   - `SagaProtocol`
   - `TwoPhaseCommitProtocol`
   - common interface
4. Domain/storage layer:
   - order persistence
   - transaction persistence
   - participant local transaction persistence
5. Adapter layer:
   - HTTP clients to participant services
   - internal participant route handlers

## Recommended File Structure

Target structure for the rebuild:

```text
common/
  http.py
  redis_lock.py
  result.py

coordinator/
  models.py
  ports.py
  service.py
  recovery.py
  protocols/
    base.py
    saga.py
    two_pc.py

order/
  app.py
  routes/
    public.py
  domain.py
  store.py

payment/
  app.py
  routes/
    public.py
    internal.py
  domain.py
  tx_store.py

stock/
  app.py
  routes/
    public.py
    internal.py
  domain.py
  tx_store.py
```

The important constraint is architectural, not naming:

- `coordinator/` must not depend on Flask
- `coordinator/` must depend on interfaces/ports, not concrete route modules

## Data Model

### Order Domain Record

Order state remains the public business record:

- `order_id`
- `paid`
- `items`
- `user_id`
- `total_cost`

### Canonical Checkout Transaction Record

One durable coordinator record per checkout transaction:

- `tx_id`
- `order_id`
- `protocol` (`saga` or `2pc`)
- `status`
- `decision` (`commit` or `abort`, only when relevant)
- `items_snapshot` (aggregated, deterministic item list)
- `stock_prepared`
- `stock_committed`
- `stock_compensated`
- `payment_prepared`
- `payment_committed`
- `payment_reversed`
- `last_error`
- `created_at`
- `updated_at`
- `retry_count`

This record is the fast-path materialized view used by the coordinator during normal execution.

### Append-Only Transaction Step Log

Each checkout transaction also keeps an append-only per-transaction step log.

Recommended event types:

- `tx_created`
- `stock_prepare_requested`
- `stock_prepared`
- `stock_prepare_rejected`
- `payment_prepare_requested`
- `payment_prepared`
- `payment_prepare_rejected`
- `decision_commit`
- `decision_abort`
- `stock_commit_requested`
- `stock_committed`
- `payment_commit_requested`
- `payment_committed`
- `stock_abort_requested`
- `stock_aborted`
- `payment_abort_requested`
- `payment_aborted`
- `saga_reserve_requested`
- `saga_reserved`
- `saga_charge_requested`
- `saga_charged`
- `saga_refund_requested`
- `saga_refunded`
- `saga_release_requested`
- `saga_released`
- `order_marked_paid`
- `tx_completed`
- `tx_failed_needs_recovery`

Each log entry should include:

- `sequence`
- `tx_id`
- `event_type`
- `timestamp`
- `actor`
- `details`

Storage guidance:

1. Keep the log per transaction, not as one global stream for Phase 1.
2. Append and summary-state mutation must be committed in one atomic write unit when they describe the same transition.
3. Use the canonical tx record for current-state reads.
4. Use the step log for auditability, recovery reconstruction, and debugging ambiguous crash windows.

Implementation rule:

- do not allow "log says step happened, summary says it did not" or the reverse for the same transition

### Coordinator Metadata

Keep orchestration metadata separate from domain records:

- `order_active_tx:{order_id}`
- `tx:{tx_id}`
- `tx_decision:{tx_id}`
- `order_commit_fence:{order_id}`
- `checkout_lock:{order_id}`

This avoids hiding domain state and coordinator state behind one mixed persistence surface.

### Participant Local Transaction Records

Each participant stores per-transaction state by `tx_id`:

- target entity id
- amount
- prepared flag
- committed/applied flag
- aborted/reversed flag
- timestamps
- optional expiry metadata for abandoned prepared holds

Participant operations must be idempotent by `tx_id`.

Participants should also support:

- startup reconciliation of non-terminal local tx records
- bounded stale-hold cleanup that never violates a known coordinator decision

## Core Invariants

These invariants drive both implementation and tests.

1. `order.paid == true` only if checkout has committed successfully.
2. A duplicate checkout request must not reapply side effects.
3. No checkout failure may leave net stock lost.
4. No checkout failure may leave net credit lost.
5. Concurrent checkout attempts for the same order must produce at most one logical outcome.
6. Unknown outcomes caused by crashes must converge to a terminal, invariant-safe state through recovery.

## Protocol Selection

The runtime chooses one protocol at startup:

- `CHECKOUT_PROTOCOL=saga`
- `CHECKOUT_PROTOCOL=2pc`

Rules:

1. Invalid values fail fast during startup.
2. Switching protocol requires a service restart.
3. Runtime switching is out of scope.
4. The selected mode applies to the full running stack for that deployment.
5. Mixed SAGA/2PC service combinations in one deployment are out of scope.

This preserves the assignment requirement that both protocols exist without complicating the public API.

## Concurrency Control for 2PC

Phase 1 uses reservation-based strict 2PL semantics at the participant level.

That means:

1. `prepare` in `stock` creates a durable reservation on quantity.
2. `prepare` in `payment` creates a durable reservation on funds.
3. Those prepared holds remain in place until `commit` or `abort`.
4. `commit` consumes the prepared hold into a final applied change.
5. `abort` releases the prepared hold without applying the final change.

This is the recommended Phase 1 model because it aligns naturally with 2PC's `prepare -> decision -> commit/abort` lifecycle.

Timestamp ordering via a Redis atomic counter is not the primary concurrency control mechanism for Phase 1.

It may still be used for:

- monotonic transaction IDs
- per-transaction event ordering
- diagnostics

It should not be used as the main conflict-resolution policy for checkout execution.

## Locking and Guard Responsibilities

Three mechanisms exist and they serve different purposes.

### Request Lease Lock

The per-order lease lock is short-lived request-path mutual exclusion.

Its job is:

- stop two checkout handlers from actively coordinating the same order at the same time

Its job is not:

- represent durable prepared state
- represent transaction ownership after a crash
- replace the active transaction guard

### Active Transaction Guard

The active transaction guard is a durable pointer from order to the current in-flight checkout transaction.

Its job is:

- prevent a new checkout from starting while an earlier transaction is still non-terminal or not yet safe to re-enter

It must outlive request crashes and must not be cleared only because a lease lock expired.

### Prepared Holds

Prepared holds live in participant stores.

Their job is:

- protect stock and funds between `prepare` and `commit/abort`
- survive coordinator or participant restarts

Prepared holds are part of transaction durability, not request mutual exclusion.

## Performance Strategy

Phase 1 prioritizes raw request throughput on the benchmark-visible path while preserving correctness.

The practical strategy is:

1. Keep the checkout path synchronous and avoid a message bus on the hot path.
2. Make the already-paid `200` path extremely cheap.
3. Minimize Redis round trips and HTTP calls for real checkout work.
4. Keep long-running recovery and reconciliation out of request threads.
5. Use direct service-to-service calls, not internal gateway hairpins.
6. Use connection reuse for all internal HTTP clients.
7. Keep serialization and Redis payloads compact.
8. Fail fast on overload or ambiguity instead of stalling enough requests to blow the latency budget.

Two performance metrics must be tracked separately:

1. Headline request throughput:
   - all successful request handling, including already-paid `200` no-ops
2. Committed checkout throughput:
   - only requests that perform a real successful distributed checkout

Both are useful, but they must not be conflated.

### Interpreting the throughput target

The practical target discussed in planning is:

- approximately `10,000 requests/second`
- with request latency capped around `2s`

This target is only meaningful if we state which request population is being measured.

Implications:

1. If the workload is dominated by repeated checkout calls on already-paid orders, the system can report very high request throughput because many responses are cheap `200` no-ops.
2. If the workload is dominated by real first-time distributed checkouts, the achievable throughput will be much lower because every successful commit touches:
   - order state
   - stock state
   - payment state
   - transaction metadata
3. A design that maximizes raw benchmark-visible throughput may not maximize real committed checkout throughput.

This is acceptable as long as the metric is named honestly.

### Rough latency budget thinking

The plan intentionally does not fix exact micro-budgets yet, but the architecture should be designed as if the checkout request only has room for a small number of network and Redis round trips.

That means:

1. The already-paid fast path must be near-minimal:
   - one lightweight read path
   - immediate `200`
2. The real checkout path must avoid avoidable extra hops:
   - no queue broker
   - no gateway hairpin
   - no duplicate state writes
3. Any design that turns one logical transition into many separate remote operations is suspect.

## Fast Already-Paid Path

Because the benchmark may repeatedly call checkout on the same order IDs, the already-paid path should be intentionally optimized.

Fast-path rule:

1. Read a lightweight order summary first.
2. If `paid == true` and there is no active transaction and no commit fence, return `200` immediately.
3. Do not create a new checkout tx for that no-op case.
4. Do not append a tx step-log entry for that no-op case.
5. Only enter full coordination for unpaid or ambiguous orders.

This improves headline throughput while preserving the idempotent API contract.

Rationale:

1. The cheap `200` path is not a hack; it is a valid optimization of correct idempotent behavior.
2. The provided benchmark can heavily reward this path, especially when it reuses order IDs.
3. The optimization is safe only when the order is "safely paid":
   - no active transaction
   - no commit fence
   - no ambiguous recovery-required state

## Batching and Order-Size Rules

The benchmark's create-order scenario can generate large orders, so the coordinator must avoid per-line-item chatter.

Rules:

1. Aggregate duplicate order lines by `item_id` before protocol execution.
2. Protocols operate on the aggregated item snapshot, not the raw order line list.
3. Prefer batched internal stock operations per transaction over one HTTP round trip per raw line item.
4. The tx step log should record meaningful transition boundaries, not one verbose event per raw line item unless required for recovery.
5. If the external benchmark can generate pathologically large orders, that should be treated as a performance risk to design around, not as a harmless corner case.

If internal batching is not implemented immediately, the plan must assume order-size limits will materially affect latency.

Why this matters:

1. The benchmark's create-order scenario can produce very large orders because it randomly picks an item count from a wide seeded range.
2. A naive "one remote call per line item" protocol path will collapse under such inputs.
3. Logging every tiny step can become as expensive as the business work itself.

## Request Flow

The `POST /orders/checkout/{order_id}` route should do only this:

1. Validate the order exists.
2. Check the fast already-paid path using lightweight summary state.
3. Acquire the per-order lock for unpaid or ambiguous orders.
4. Reject with `409` if a non-terminal active transaction already exists.
5. Create a canonical transaction record before the first participant call.
6. Call `CoordinatorService.execute_checkout(...)`.
7. Translate the structured result into `2xx` or `4xx`.
8. Release the lock.

The route must not contain protocol logic.

The route also must not:

1. hide recovery policy inside ad hoc conditional branches
2. duplicate commit-fence logic that exists elsewhere
3. turn already-paid no-op handling into a full transaction path

## Protocol Responsibilities

### SAGA

Success path:

1. Persist `INIT`.
2. Reserve stock from the aggregated deterministic item snapshot.
3. Charge payment.
4. Mark order paid.
5. Mark transaction completed.

Failure path:

1. Persist compensation state before compensating.
2. Reverse payment if already charged.
3. Release stock in reverse durable order.
4. Mark terminal abort/failure state.

### App-Level 2PC

Success path:

1. Persist `INIT`.
2. Prepare stock participants by creating durable stock reservations from the aggregated item snapshot.
3. Prepare payment participant by creating a durable funds hold.
4. Persist durable commit decision.
5. Set commit fence.
6. Commit stock by consuming the prepared reservations.
7. Commit payment by consuming the prepared funds hold.
8. Finalize order as paid.
9. Clear fence and mark completed.

Failure path:

1. Persist durable abort decision where possible.
2. Abort all prepared participants by releasing their prepared holds.
3. Mark terminal abort state.

Recovery policy:

- presumed abort when no durable commit decision exists

## Recovery Model

Recovery is part of Phase 1 and remains embedded in the `order` service.

Rules:

1. The recovery worker scans all non-terminal transactions on startup.
2. It runs periodically afterward.
3. It resumes work by calling a coordinator recovery entrypoint, not by reimplementing protocol logic in route code.
4. Request-time healing is limited to 2PC finalization after a known commit decision.
5. All other long-running repair happens in background recovery.
6. Recovery should consult the canonical tx record first and the append-only step log when the summary state is ambiguous.
7. If multiple `order` replicas run, only one recovery worker may actively scan and resume transactions at a time.

This keeps synchronous request handling bounded while still satisfying consistency requirements.

Replica-safety rule:

- use a distributed leader lock for the recovery worker when `order` is horizontally scaled

Additional recovery principles:

1. Recovery must be idempotent when replayed or invoked twice.
2. Recovery is allowed to be conservative and slower than the request path.
3. Recovery must prefer invariant safety over fast unblocking.
4. If recovery and normal request handling disagree, the durable transaction state wins over in-memory assumptions.

## Engineering Quality Rules

These rules apply to every implementation step and are part of the architecture, not optional style preferences.

1. Modularity is required.
2. Code duplication is treated as a design bug, especially in:
   - recovery logic
   - commit-fence handling
   - participant state transitions
   - HTTP-to-domain result mapping
3. Non-obvious code paths must have short explanatory comments.
4. Comments must explain why the code exists or why a branch is safe, not restate obvious syntax.
5. Shared logic must be extracted behind reusable helpers, ports, or protocol primitives instead of copied into routes.
6. The coordinator, protocols, and adapters must remain separated by responsibility.
7. Every step must preserve or improve consistency invariants; performance gains never justify weakening correctness.

## Validation Rules For Every Step

No implementation step is complete until all three categories below are satisfied.

### What must happen in every step

1. Implement the code changes for that step only after reading the current plan and design-limitation log.
2. Research which tests are needed for the specific change:
   - unit tests
   - integration tests
   - recovery tests
   - concurrency tests
   - benchmark validations when performance is affected
3. Implement the missing tests as part of the same step, not as optional later cleanup.
4. Run the relevant tests and inspect failures before moving on.
5. Update the plan and limitation log if the step reveals a new architectural constraint.

### Consistency gate for every step

1. No step may introduce a known path to:
   - double-charge
   - double-subtract
   - silent money loss
   - silent stock loss
   - duplicate logical commits
2. If a step leaves a temporary gap in behavior, that gap must fail closed:
   - reject requests
   - return explicit failure
   - preserve recovery state
   - never silently commit ambiguous work

### Evidence required for every step

1. Code inspection evidence:
   - architecture boundary remains intact
   - no new duplication was introduced
2. Test evidence:
   - the tests written for the step pass
3. Invariant evidence:
   - the step can be explained in terms of preserved money, stock, and order correctness

## Implementation Program

Each step below defines:

1. what should happen in that step
2. how the step must be validated
3. what acceptance criteria must be met before moving forward

### Step 1: Freeze the implementation contract and architecture skeleton

What should happen:

1. Keep all public route signatures and response classes unchanged.
2. Create or update the architecture scaffolding:
   - `coordinator/`
   - protocol interfaces
   - result types
   - storage port interfaces
3. Establish the deployment-wide protocol configuration contract.
4. Record the clean-slate schema assumption in code comments or config docs where the protocol mode is defined.

Validation:

1. Review the public API surface against `assignment.md`.
2. Add or update tests that assert the existing route shapes and already-paid `200` behavior still match the contract.
3. Add a configuration test that invalid protocol values fail fast.

Acceptance criteria:

1. Public API behavior remains unchanged from the client perspective.
2. The coordinator can be imported without Flask dependencies.
3. Protocol mode is clearly defined as deployment-wide and cannot silently drift per service.

### Step 2: Build the durable data model and atomic state-transition substrate

What should happen:

1. Implement the order domain model.
2. Implement the canonical checkout transaction summary record.
3. Implement the append-only per-transaction step log.
4. Implement coordinator metadata keys:
   - active tx guard
   - commit fence
   - request lease lock
5. Separate domain persistence from orchestration persistence.
6. Implement atomic write helpers so one logical transition updates summary and log together.

Validation:

1. Write unit tests for serialization/deserialization of all transaction records.
2. Write store-level tests that prove a transition cannot leave summary and log out of sync.
3. Write crash-window tests for ambiguous transitions:
   - before append
   - after append
   - after atomic transition write

Acceptance criteria:

1. A transaction can be reconstructed from durable Redis state alone.
2. Domain records and orchestration records are not conflated in one opaque persistence layer.
3. Summary/log divergence for the same transition is prevented by construction.
4. The storage layer reflects the clean-slate schema assumption and does not carry compatibility shims for older layouts.

### Step 3: Build participant transaction primitives first

What should happen:

1. Implement payment internal handlers for:
   - saga pay
   - saga refund
   - 2PC prepare
   - 2PC commit
   - 2PC abort
2. Implement stock internal handlers for:
   - saga reserve
   - saga release
   - 2PC prepare
   - 2PC commit
   - 2PC abort
3. Represent 2PC prepare as durable holds, not transient locks.
4. Add participant-side startup reconciliation.
5. Add bounded stale-hold cleanup.
6. Shape the stock interface so batched work over aggregated item snapshots is possible.

Validation:

1. Write idempotency tests for repeated `tx_id` requests.
2. Write participant restart tests proving prepared state survives process restart.
3. Write tests for stale-hold cleanup that prove cleanup never violates a durable coordinator decision.
4. Write tests for batched stock operations if batching is implemented.

Acceptance criteria:

1. Repeating the same internal participant request is safe.
2. Prepared stock and funds remain protected until `commit` or `abort`.
3. Participant behavior is modular and shared across protocol callers instead of duplicated.
4. Abandoned prepared holds do not remain stuck indefinitely after recoverable failures.

### Step 4: Implement the pure coordinator service

What should happen:

1. Implement `CoordinatorService.execute_checkout(...)`.
2. Implement `CoordinatorService.resume_transaction(...)`.
3. Keep all Flask response construction outside the coordinator.
4. Route all protocol selection through a single coordinator-owned dispatch path.

Validation:

1. Write unit tests for coordinator state transitions without importing Flask.
2. Write tests that assert routes only map structured results to HTTP responses.
3. Inspect the code for duplicate orchestration logic outside the coordinator boundary.

Acceptance criteria:

1. Coordinator logic is unit-testable in isolation.
2. Route handlers are thin adapters.
3. Protocol selection and recovery entry are centralized in one reusable service surface.

### Step 5: Implement the optimized already-paid fast path

What should happen:

1. Add the lightweight pre-check for:
   - `paid`
   - active tx guard
   - commit fence
2. Return immediate `200` when the order is safely paid.
3. Avoid creating a new transaction record for this no-op path.
4. Avoid entering full coordination for this path.

Validation:

1. Write route tests proving already-paid checkout returns `200`.
2. Write tests proving the fast path does not:
   - create a tx
   - mutate stock
   - mutate payment
   - append a tx log event
3. Benchmark the fast path separately from real checkout work.

Acceptance criteria:

1. The already-paid path is correct and side-effect free.
2. The already-paid path is cheaper than the real checkout path by design.
3. The optimization does not bypass ambiguity checks such as active tx or commit-fence state.

### Step 6: Implement the SAGA protocol on the new substrate

What should happen:

1. Implement SAGA against the coordinator ports, not route code.
2. Use aggregated item snapshots, not raw order lines.
3. Persist each meaningful transition through the shared atomic transition helpers.
4. Implement reverse-order compensation using durable step state.

Validation:

1. Write protocol-unit tests for:
   - happy path
   - stock rejection
   - payment rejection
   - duplicate compensation replay
2. Write integration tests proving no stock/payment drift on failure.
3. Write crash-recovery tests for interrupted compensation.

Acceptance criteria:

1. Stock failure leaves no payment or stock drift.
2. Payment failure leaves no stock drift.
3. SAGA logic does not duplicate coordinator or route behavior.
4. Compensation is safe to replay.

### Step 7: Implement the 2PC protocol on the new substrate

What should happen:

1. Implement the prepare phase for stock and payment.
2. Persist the global commit decision before any commit call.
3. Implement commit-fence behavior for post-decision crashes.
4. Implement abort handling for all prepared branches.
5. Keep commit-fence resolution in one shared coordinator-owned path.

Validation:

1. Write protocol-unit tests for:
   - all-yes prepare
   - stock prepare rejection
   - payment prepare rejection
   - crash after durable commit decision
   - repeated commit/abort requests
2. Write recovery tests for presumed-abort behavior when no durable commit decision exists.
3. Write tests that prove no participant can commit before the durable decision is written.

Acceptance criteria:

1. No participant commits before a durable global commit decision exists.
2. In-doubt branches can be resolved through durable state and presumed abort.
3. Commit-fence logic exists in one place only.
4. 2PC participant semantics remain explicit and recoverable.

### Step 8: Implement the recovery worker and replica-safe recovery control

What should happen:

1. Implement startup scanning for non-terminal transactions.
2. Implement periodic recovery scanning.
3. Use `CoordinatorService.resume_transaction(...)` for all repair work.
4. Add a distributed leader lock so only one `order` replica runs the scanner at a time.

Validation:

1. Write tests for duplicate recovery invocation; replay must be idempotent.
2. Write tests for multi-replica leader-lock behavior.
3. Write service-restart tests for both SAGA and 2PC interrupted flows.

Acceptance criteria:

1. A service restart can converge interrupted transactions.
2. Recovery remains single-active when `order` is horizontally scaled.
3. Recovery logic is not duplicated across route code, protocol code, and worker code.

### Step 9: Replace legacy checkout internals with the new orchestration path

What should happen:

1. Keep the route path unchanged.
2. Replace legacy inline rollback logic with coordinator invocation.
3. Keep the route as:
   - validation
   - fast-path pre-check
   - lock acquisition
   - coordinator call
   - HTTP mapping
4. Remove any duplicated orchestration logic left in old route code.

Validation:

1. Write integration tests that exercise the route in both `saga` and `2pc` modes.
2. Review the final route code for architecture violations:
   - no protocol logic
   - no duplicated recovery logic
   - no direct participant choreography

Acceptance criteria:

1. The route is thin and protocol-agnostic.
2. Both modes run through the same coordinator entrypoint.
3. The final route implementation is simpler than the legacy path, not just relocated complexity.

### Step 10: Research, implement, and run the full validation matrix

What should happen:

1. Research the full set of tests required for this architecture, not just the ones already listed in the repo.
2. Implement the missing tests needed to validate:
   - domain correctness
   - participant idempotency
   - coordinator transitions
   - recovery
   - concurrency
   - throughput interpretation
3. Run the suite in `saga` mode.
4. Restart the stack and run the suite in `2pc` mode.
5. Run benchmark-visible stress scenarios separately for:
   - reused-order checkout throughput
   - create-order end-to-end throughput
6. Run the consistency benchmark as an invariant check.

Validation:

1. Review the repository and benchmark repo to discover missing scenarios, especially around:
   - duplicate checkouts
   - already-paid no-op handling
   - commit-fence recovery
   - multi-replica recovery worker behavior
   - large-order behavior under aggregated item snapshots
2. Add tests for every discovered gap before calling the plan complete.
3. Capture measured latency and throughput separately for:
   - headline request throughput
   - committed checkout throughput

Acceptance criteria:

1. Baseline behavior passes in both modes.
2. Failure, retry, and recovery paths preserve invariants.
3. Test coverage explicitly validates the consistency guarantees claimed by the design.
4. Performance reports distinguish no-op `200` throughput from actual committed checkout throughput.

## Testing Priorities

The following scenarios must be covered before considering the rebuild stable.

1. Happy-path checkout succeeds in `saga`.
2. Happy-path checkout succeeds in `2pc`.
3. Out-of-stock aborts cleanly in both modes.
4. Insufficient credit aborts cleanly in both modes.
5. Duplicate checkout of an already-paid order returns `200` with no extra side effects.
6. Concurrent checkout requests for the same order do not double-apply side effects.
7. Crash after 2PC commit decision but before order finalization recovers to a consistent result.
8. Crash during saga compensation resumes safely.
9. Replayed internal participant requests are idempotent.
10. Recovery worker remains single-active under multiple `order` replicas.
11. The already-paid fast path returns `200` without entering full transaction coordination.

## Benchmark Notes

The provided benchmark is useful but can overstate checkout throughput if reused orders dominate.

Important caveats:

1. The default reused-order Locust scenario can produce many cheap already-paid `200` responses.
2. That scenario is valid for headline request throughput, but it is not the same as real committed checkout throughput.
3. The create-order scenario is a better measure of full end-to-end work.
4. Report both numbers when presenting performance results.

### Specific observations from the local benchmark project

The current benchmark repo at `C:\Users\Theo\source\repos\wdm-project-benchmark` has several behaviors that influence interpretation:

1. `stress-test/locustfile.py`
   - repeatedly checks out from a fixed order pool
   - treats `4xx` as failures
   - therefore heavily rewards an idempotent already-paid `200` path
2. `stress-test/locustfile_create_orders.py`
   - creates an order, adds stock, adds items, then checks out
   - is closer to real full-path work
   - can generate large orders because the seeded item-count range is broad
3. `consistency-test`
   - is much better as an invariant checker than as a throughput benchmark
   - its value is in verifying no money/stock drift under concurrency and failure, not in proving latency targets

Operational takeaway:

- use the reused-order scenario when you want maximum visible throughput
- use the create-order scenario when you want a more honest end-to-end throughput number
- use the consistency test to validate invariants under concurrent stress and failure injection

## Rejected Alternatives Summary

This section collects the main ideas we explicitly decided not to use in Phase 1.

### No message bus on the checkout path

Rejected for Phase 1 because:

1. It increases tail latency on a synchronous API.
2. It complicates 2PC more than it helps.
3. It risks repeating attempt 1's scope drift.

### No timestamp-ordering-first design

Rejected for Phase 1 because:

1. It is harder to explain and recover than reservation-based prepared holds.
2. It turns more conflicts into retries.
3. It is a worse fit for explicit 2PC participant state.

### No full event-sourced rebuild

Rejected for Phase 1 because:

1. It is architecturally larger than needed.
2. It complicates reads and debugging of business-state queries.
3. A hybrid summary + per-tx log captures the valuable parts with much less cost.

### No "heal everything inline" request path

Rejected for Phase 1 because:

1. It weakens lock correctness.
2. It hurts throughput and latency.
3. Background recovery is the safer place for slow repair.

### No treating already-paid as a client-visible error

Rejected because:

1. It is semantically wrong for an idempotent operation.
2. It hurts benchmark-visible throughput.
3. It gives up a cheap safe fast path for no consistency gain.

## Definition of Done for This Rebuild

This rebuild is ready for Phase 1 delivery when all of the following are true:

1. The codebase supports both `saga` and `2pc` with a startup flag.
2. The public API remains unchanged.
3. The coordinator logic is separated from Flask routes.
4. Recovery is durable and explicit.
5. The main consistency invariants are enforced in tests.
6. The coordinator can later be moved into a standalone service with mostly adapter-level changes.
7. Performance measurements explicitly separate no-op request throughput from committed checkout throughput.
8. Critical logic remains modular, with no duplicated recovery/protocol paths.
9. Non-obvious correctness-critical code paths are commented clearly enough to explain why they are safe.

## Phase 2 Extraction Path

The rebuild is intentionally shaped so Phase 2 becomes a relocation exercise, not a redesign.

Expected Phase 2 move:

1. Move `coordinator/` into its own service.
2. Replace direct in-process coordinator calls with network calls from `order`.
3. Keep protocol logic, state transitions, and recovery model largely unchanged.
4. Keep participant internal APIs as the contract between coordinator and services.

If Phase 1 code cannot be moved this way, the boundary is still too coupled and should be corrected before deeper implementation continues.
