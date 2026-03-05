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
5. No long polling loops in request handlers while holding the active-tx guard.
6. No duplicate copies of finalization or recovery logic.
7. Redis remains the system of record for Phase 1.
8. True XA is out of scope; 2PC is application-managed.
9. App-level 2PC uses reservation-based prepared holds at participants, not timestamp ordering as the primary concurrency policy.
10. Transaction state must not rely on summary flags alone; every checkout transaction also records structured application logs for intermediate steps and durable Redis records for critical decisions.
11. RabbitMQ is used as the internal transport for coordinator↔participant communication. The external API remains synchronous.
12. `POST /orders/checkout/{order_id}` returns `200` as an idempotent no-op when the order is already safely paid.
13. Breaking storage schemas is acceptable during the rebuild because this is a proof-of-concept/MVP and production starts with empty state.
14. Protocol mode is deployment-wide; all services in one running stack use the same transaction mode.
15. All read-modify-write operations on participant state (stock, credit) must be atomic at the Redis level using Lua scripts or `WATCH`/`MULTI`/`EXEC`. Non-atomic `GET` then `SET` patterns are treated as correctness bugs.
16. Internal transaction operations use RabbitMQ. Non-transactional lookups use direct service URLs. Nothing internal routes through the nginx gateway.
17. Gunicorn worker counts must be tuned for the synchronous blocking call pattern; 2 workers is insufficient for any meaningful throughput.

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

### Decision: Use RabbitMQ as internal coordinator-to-participant transport with parallel fan-out

Chosen:

- use RabbitMQ for internal coordinator↔participant communication
- the coordinator publishes prepare/commit/abort messages to stock and payment queues in parallel
- participants consume messages, execute against Redis, and publish results to a reply queue
- the coordinator consumes replies and makes the commit/abort decision
- the external checkout API remains synchronous: the route blocks until the coordinator has a final result
- stock and payment operations happen in parallel (not serial)

Why:

1. The assignment explicitly rewards event-driven asynchronous architectures with extra points for difficulty.
2. The lecturer heavily advises for a message broker.
3. Parallel fan-out to stock and payment cuts checkout latency roughly in half compared to serial HTTP calls.
4. RabbitMQ provides durable message delivery, which improves fault tolerance — messages survive broker restarts.
5. The pattern naturally fits SAGA compensation (publish compensation messages on failure).
6. It demonstrates more advanced distributed systems understanding during the evaluation interview.

How the synchronous API works with async internals:

1. `POST /orders/checkout/{order_id}` arrives at the order service.
2. The coordinator creates a tx record, then publishes prepare/reserve messages to RabbitMQ.
3. The coordinator blocks on a reply queue (with timeout) waiting for participant results.
4. Once all results arrive, the coordinator decides and publishes commit/abort messages.
5. The coordinator blocks on commit/abort confirmations.
6. The route returns `2xx` or `4xx` to the client.

The blocking wait on the reply queue is bounded by a timeout. If participants don't respond in time, the coordinator aborts (presumed abort for 2PC, compensation for SAGA).

Why this is different from attempt 1 (which also used RabbitMQ):

1. Attempt 1 used RabbitMQ as the sole orchestration mechanism with polling for the synchronous response. The checkout API published a message, then polled Redis for saga state — a hot-loop bottleneck.
2. This design uses RabbitMQ as transport but keeps the coordinator as an in-process state machine that blocks on reply queues. No polling loop. The coordinator directly receives results via RabbitMQ's consumer model.
3. Attempt 1 had no 2PC at all. This design uses the same message transport for both SAGA and 2PC.
4. Attempt 1 used a separate order-worker process for orchestration. This design keeps the coordinator in-process in the order service, matching the Phase 2 extraction architecture.

Lessons preserved from attempt 1:

- Lua scripts for atomic participant operations (this worked well)
- Idempotent participant handlers keyed by tx_id (this worked well)
- Durable queues with persistent messages (this worked well)
- Dead-letter queues for poison messages (adopt this)

Lessons avoided from attempt 1:

- No Redis polling loop for checkout response (use reply queue blocking instead)
- No separate worker process (keep coordinator in-process)
- No 7-day idempotency TTL (use shorter TTL appropriate for recovery window)
- No complex retry queue topology (use simple DLQ + recovery worker re-publish)

Alternative considered:

- direct internal HTTP calls (the previous plan)

Why we are moving away from that:

1. Serial HTTP calls are the primary latency bottleneck for checkout.
2. HTTP does not provide built-in retry/redelivery semantics.
3. Direct HTTP does not qualify as event-driven architecture, which the assignment rewards.
4. The lecturer explicitly advises for a message broker.

Alternative considered:

- parallel HTTP calls with ThreadPoolExecutor (no broker)

Why that alone is insufficient:

1. It achieves parallelism but not the event-driven architecture bonus.
2. It provides no message durability — if a participant is temporarily down, the message is lost.
3. It does not demonstrate the distributed systems patterns expected for difficulty scoring.
4. ThreadPoolExecutor parallelism can still be used within the coordinator as an implementation detail if needed for non-message-based operations.

### Decision: Parallel participant operations are the default execution model

Chosen:

- stock and payment prepare/reserve operations happen in parallel, not serial
- stock and payment commit/abort operations happen in parallel, not serial
- the coordinator publishes to both participant queues simultaneously and waits for both replies

Why:

1. Stock and payment are independent participants. There is no ordering dependency between them during prepare or commit.
2. Parallel execution cuts the dominant latency cost (participant round trips) roughly in half.
3. Both attempt 1 and attempt 2 used serial participant calls, which was the primary latency bottleneck.

Ordering rules:

- Within 2PC: parallel prepare → decision → parallel commit/abort. The decision is the serialization point.
- Within SAGA: parallel reserve stock + charge payment → mark paid. On failure: parallel compensation (refund + release).
- Item aggregation still happens before parallel dispatch — the coordinator aggregates duplicate items, then sends one message per participant (not per item).

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
- diagnostics and ordering within application logs

### Decision: Use a hybrid state model with lightweight observability

Chosen:

- one compact canonical tx summary record in Redis for fast-path reads and recovery decisions
- durable Redis records only for the critical decision point (commit/abort decision) and the final outcome
- structured application-level logging (stdout/gunicorn) for intermediate protocol steps

Why:

1. Summary records are efficient for the checkout hot path.
2. The commit/abort decision is the only intermediate state that recovery truly needs to be durable — everything else can be re-derived from the summary record plus the decision.
3. Application-level structured logging (with tx_id, step name, timestamp) provides the same debugging and auditability benefit as a per-tx Redis log, without the write overhead.
4. Each real checkout would otherwise generate 6-10 durable Redis log writes. Under load, this materially increases latency and Redis pressure for information that is only needed during debugging, not during recovery.

What is durably persisted in Redis:

1. The canonical tx summary record (status, participant flags, decision).
2. The commit/abort decision marker (`tx_decision:{tx_id}`).
3. The commit fence (`order_commit_fence:{order_id}`).
4. The active-tx guard (`order_active_tx:{order_id}`).

What is logged to application logs only:

1. Intermediate step transitions (prepare requested, prepared, commit requested, etc.).
2. Timing and diagnostic metadata.
3. Error context for failed operations.

Recovery uses the durable summary record and decision marker. If the summary is ambiguous (e.g., crash between decision write and summary update), recovery checks the decision marker. This covers the same crash windows that the step log was designed for, with fewer moving parts.

Alternative considered:

- full append-only per-transaction step log in Redis

Why we are not doing that:

1. It adds significant write overhead per checkout under load.
2. Recovery only needs the decision point to resolve ambiguity, not the full step history.
3. The atomic-write requirement (summary + log in one `MULTI`/`EXEC`) adds implementation complexity for marginal recovery benefit.
4. Application logs provide the same debugging value without the performance cost.

Alternative considered:

- keep only summary flags/fields with no logging

Why we are not doing that:

1. Intermediate step visibility is valuable for debugging compensation and rollback bugs.
2. Structured application logs are cheap and provide this without durable write overhead.

Alternative considered:

- full event sourcing for all state across services

Why we are not doing that:

1. It would require rebuilding the services around event replay semantics instead of simple materialized state.
2. It adds substantial complexity to reads, testing, and invariants.
3. It is unnecessary for Phase 1 and would again risk solving the wrong problem first.

### Decision: All participant read-modify-write operations must be Redis-atomic

Chosen:

- every operation that reads a value, modifies it, and writes it back must use a Lua script or `WATCH`/`MULTI`/`EXEC`
- this applies to both public endpoints (`/stock/subtract`, `/payment/pay`, `/stock/add`, `/payment/add_funds`) and internal transaction endpoints

Why:

1. The current template code does `GET` then `SET` without any atomicity guard.
2. Two concurrent `subtract` calls can both read `stock=10`, both compute `stock=9`, and both write `9`, silently losing inventory.
3. Under benchmark load, this is not a theoretical risk — it will cause consistency failures immediately.
4. This is a prerequisite for any meaningful consistency guarantee, independent of SAGA or 2PC.

Preferred approach:

- use Lua scripts for participant-side atomic operations because they are simpler and faster than `WATCH`/`MULTI`/`EXEC` retry loops
- a Lua script for subtract: read current value, check sufficient quantity, decrement, write, return result — all in one atomic step
- this eliminates the race window entirely rather than adding retry logic around it

Alternative considered:

- `WATCH`/`MULTI`/`EXEC` optimistic locking

Why Lua scripts are preferred:

1. `WATCH` requires a retry loop on contention, which adds code complexity and latency variance.
2. Lua scripts execute atomically on the Redis server with no contention retries needed.
3. They are the standard Redis pattern for conditional read-modify-write.

### Decision: Internal transaction operations use RabbitMQ; non-transactional lookups use direct HTTP

Chosen:

- all coordinator↔participant transaction operations (prepare, commit, abort, reserve, release, charge, refund) go through RabbitMQ
- non-transactional lookups that the checkout route needs before entering the coordinator (e.g., order read, item price lookup for `addItem`) use direct HTTP to the relevant service, bypassing the nginx gateway
- the nginx gateway is only used for external client traffic

Why:

1. Transaction operations benefit from RabbitMQ's durability, parallel fan-out, and retry semantics.
2. Simple read operations (find item, find user) don't need message broker overhead — direct HTTP is faster for synchronous lookups.
3. Routing any internal call through nginx adds unnecessary latency.

Implementation:

- add `STOCK_SERVICE_URL` and `PAYMENT_SERVICE_URL` to docker-compose environment for the order service (for non-transactional HTTP lookups)
- add `ORDER_SERVICE_URL` to docker-compose environment for stock and payment services (for `GET /internal/tx_decision/{tx_id}` queries during startup reconciliation)
- add `RABBITMQ_URL` to all services for message broker connectivity
- remove `GATEWAY_URL` entirely — no internal path needs it (see "Eliminate GATEWAY_URL" section below)

### Decision: Gunicorn worker count must match the synchronous blocking architecture

Chosen:

- set gunicorn workers to at least `8` for the order service and `4` for stock and payment services in docker-compose
- these are starting values; tune further based on benchmark results

Why:

1. The current configuration uses `-w 2` for all services.
2. The checkout path makes synchronous blocking HTTP calls to participants. Each gunicorn worker is blocked for the entire duration of those calls.
3. With 2 workers, the order service can process at most 2 concurrent checkouts. Under the benchmark's concurrent load, this is the primary throughput bottleneck.
4. The plan targets ~10,000 requests/second. Even the already-paid fast path needs enough workers to accept concurrent connections.

Alternative considered:

- switch to async workers (gevent or eventlet)

Why we are not doing that now:

1. Async workers require careful attention to Redis client compatibility and connection pooling.
2. They change the concurrency model in ways that affect lock behavior and recovery reasoning.
3. Increasing sync worker count is a simpler first step that can be done immediately.
4. Async workers remain a valid later optimization if sync workers prove insufficient.

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
4. Lua scripts for atomic participant operations (stock_reserve, payment_charge, etc.) — these were correct and well-designed.
5. Inbox + effect markers for idempotency within Lua scripts.
6. Thread-local RabbitMQ publisher connections (avoids pika channel-sharing bugs).
7. Durable queues with persistent messages (delivery_mode=2).
8. Dead-letter queues for poison messages.
9. Leader-locked reconciliation worker.

### What failed in attempt 1

1. It solved a later-phase architecture problem first (built full async platform before basic correctness).
2. Checkout API used a Redis polling loop to wait for saga completion — this was a hot-loop bottleneck under load (~2% duplicate-order failures at 10k users).
3. It never delivered the required 2PC implementation (only SAGA).
4. Used a separate order-worker process for orchestration, which made the coordinator harder to reason about than an in-process coordinator.
5. 7-day TTL on idempotency markers was unnecessarily long and created edge-case risks.
6. Complex retry-queue topology (main → retry → main with x-death counting) added operational complexity for marginal benefit over simpler DLQ + recovery re-publish.

### What this rebuild takes from attempt 1

1. Lua script patterns for atomic participant operations.
2. Thread-local publisher connections.
3. Durable persistent messages.
4. DLQ for unprocessable messages.
5. Idempotent participant handlers keyed by tx_id.

### What this rebuild avoids from attempt 1

1. No Redis polling loop — coordinator blocks on RabbitMQ reply queue instead.
2. No separate worker process — coordinator is in-process in the order service.
3. No complex retry-queue topology — DLQ + recovery worker re-publish is simpler and sufficient.
4. Shorter idempotency marker TTL (1 hour, not 7 days).
5. 2PC is implemented from the start, not deferred.

### What worked in attempt 2

1. Startup protocol toggle (`saga` vs `2pc`).
2. Canonical transaction record in the order-side store.
3. Idempotent participant endpoints keyed by `tx_id` with Lua scripts.
4. Per-order lock plus deterministic item ordering.
5. Commit-fence handling for post-decision crashes.
6. Background recovery with bounded request-time healing only where necessary.
7. Presumed-abort policy for 2PC when no durable commit decision exists.
8. Leader-locked recovery worker for multi-replica safety.
9. Detailed state machine with explicit status transitions.

### What failed in attempt 2

1. The order service became both domain service and coordinator — route handlers mixed HTTP, locking, transaction orchestration, and recovery.
2. Protocol modules were tied to Flask and order persistence.
3. 2PC finalization logic was spread across multiple places.
4. Recovery knew too much about protocol internals instead of resuming through one coordinator API.
5. **Critical bug**: `FAILED_NEEDS_RECOVERY` was classified as terminal in `is_terminal_status()`, which cleared the active-tx guard and allowed a second concurrent checkout to start while the first was still recovering. This caused duplicate charges/stock drift.
6. Serial participant calls (stock → payment) were the dominant latency bottleneck.
7. Internal calls routed through the nginx gateway added unnecessary overhead.
8. Only 2 gunicorn workers per service — severe throughput limitation.
9. Request-path healing loops originally had sleep/poll that could outlive lock leases, creating race conditions.
10. Recovery was added late (commit `8cde8d9`) after the main protocol was already written, leading to immediate recovery-state bugs (fixed in `5bd4c5b`).

### What this rebuild takes from attempt 2

1. The state machine design (SAGA and 2PC status transitions).
2. Canonical tx summary record with participant progress flags.
3. Commit-fence mechanism for post-decision crash recovery.
4. Presumed-abort policy for 2PC.
5. Active-tx guard to prevent concurrent checkouts on the same order.
6. Bounded request-time healing only for the commit-fence case.
7. Lua-based atomic participant operations.

### What this rebuild avoids from attempt 2

1. No serial participant calls — parallel via RabbitMQ.
2. `FAILED_NEEDS_RECOVERY` is explicitly NOT terminal and does NOT clear the active-tx guard.
3. No gateway hairpinning for internal calls.
4. Adequate gunicorn worker counts from the start.
5. Recovery is designed alongside the protocol, not added afterward.
6. The coordinator is a separate code boundary from Flask routes from the beginning, not refactored later.
7. No request-path sleep/poll loops — return `409` immediately for active non-terminal transactions.

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

- `RabbitMQ` broker:
  - durable queues for stock and payment commands
  - reply queues for coordinator to receive participant results
  - dead-letter queues for poison messages
- `order` service hosts:
  - public order API (Flask + gunicorn)
  - embedded coordinator (publishes commands to RabbitMQ, consumes replies)
  - background recovery worker (re-publishes stale transactions)
  - RabbitMQ publisher connection (thread-local, as attempt 1 correctly did)
- `payment` service hosts:
  - public payment API (Flask + gunicorn)
  - RabbitMQ consumer for transaction commands (prepare, commit, abort, charge, refund)
  - publishes results back to coordinator reply queue
- `stock` service hosts:
  - public stock API (Flask + gunicorn)
  - RabbitMQ consumer for transaction commands (prepare, commit, abort, reserve, release)
  - publishes results back to coordinator reply queue

Message topology:

- `stock.commands` queue (durable) — coordinator publishes stock hold/release/commit commands
- `payment.commands` queue (durable) — coordinator publishes payment hold/release/commit commands
- per-process reply queues (exclusive, auto-delete) — each gunicorn worker process creates its own reply queue on startup (e.g., `coordinator.replies.{uuid}`). Participants reply to the queue name specified in the message's `reply_to` field. This ensures replies always reach the correct process.
- `*.dlq` queues — dead-letter queues for unprocessable messages
- all messages are persistent (delivery_mode=2)
- all messages carry `tx_id`, `correlation_id`, and `reply_to` fields

Reply correlation pattern:

1. Each gunicorn worker process starts a background consumer thread on startup.
2. The background thread consumes from the process's exclusive reply queue.
3. Request-handler threads register a `threading.Event` in a shared correlation map keyed by `tx_id` before publishing commands.
4. When the background thread receives a reply, it stores the result in the correlation map and signals the event.
5. The request-handler thread waits on the event with a bounded timeout.
6. On timeout, the request-handler treats the missing reply as a participant failure.

Gunicorn prefork lifecycle constraint:

1. Gunicorn uses a prefork model: the master process forks worker processes. If RabbitMQ connections are opened before forking, child processes inherit stale file descriptors that will fail silently or raise exceptions.
2. All RabbitMQ connections (publisher, reply consumer) must be created inside each worker process after forking — use gunicorn's `post_fork` or `post_worker_init` hook, never at module import time.
3. If a worker dies and is respawned (crash, `max_requests` recycling), its exclusive reply queue is auto-deleted and any in-flight replies to that queue are lost. This is acceptable: the coordinator thread's `Event.wait()` times out, the transaction is marked `FAILED_NEEDS_RECOVERY`, and the recovery worker picks it up on the next scan.

Why per-process reply queues (not a shared `coordinator.replies` queue):

1. With a shared queue and 8 worker processes, any process could consume any reply. A reply intended for process A could be consumed by process B, which has no matching correlation entry.
2. Per-process exclusive queues guarantee that replies always arrive at the process that published the command.
3. Exclusive queues are auto-deleted when the process disconnects, so they don't accumulate.
4. This is the standard RabbitMQ RPC pattern adapted for multi-process servers.

Pika threading constraints:

1. Pika's `BlockingConnection` is not thread-safe. Each thread that interacts with RabbitMQ needs its own connection.
2. Publisher connections: use `threading.local()` for per-thread publisher connections (as attempt 1 correctly did).
3. Reply consumer: one dedicated background thread per process with its own connection, consuming from the process's exclusive reply queue.
4. The correlation map (dict + events) is the only shared state between the publisher threads and the consumer thread. Use a `threading.Lock` to protect it.
5. Participant consumers in stock/payment services: one consumer thread per process with its own connection.

Code boundaries:

1. API layer:
   - Flask routes only
   - input validation
   - result-to-HTTP mapping
2. Coordinator layer:
   - protocol selection
   - transaction state transitions
   - recovery entrypoints
   - message publishing and reply consumption
   - no Flask imports
3. Protocol layer:
   - `SagaProtocol`
   - `TwoPhaseCommitProtocol`
   - common interface
   - both protocols publish to the same participant queues, just with different command types
4. Domain/storage layer:
   - order persistence
   - transaction persistence
   - participant local transaction persistence
5. Messaging layer:
   - RabbitMQ connection management (publisher and consumer)
   - message serialization/deserialization
   - reply correlation
6. Participant worker layer:
   - RabbitMQ consumers in stock and payment services
   - dispatch incoming commands to Lua-based atomic operations
   - publish results to reply queue

## Recommended File Structure

Target structure for the rebuild:

```text
common/
  __init__.py
  messaging.py        # RabbitMQ connection, publish, consume helpers
  result.py           # Structured result types
  models.py           # msgspec Struct definitions for all RabbitMQ messages (commands + replies)
  constants.py        # Shared enums/constants: command names, status codes, error codes

coordinator/
  models.py           # CheckoutTxValue, status constants
  ports.py            # Abstract interfaces: OrderPort, TxStorePort (+ participant interface)
  service.py          # CoordinatorService: execute_checkout, resume_transaction
  recovery.py         # Background recovery worker
  messaging.py        # Publish commands, consume replies, correlation
  protocols/
    base.py           # Common protocol interface
    saga.py           # SAGA: parallel reserve+charge → compensate on failure
    two_pc.py         # 2PC: parallel prepare → decision → parallel commit/abort

order/
  app.py              # Flask app, starts recovery worker + RabbitMQ connections
  routes/
    public.py         # Checkout, order CRUD
    internal.py       # GET /internal/tx_decision/{tx_id}
  domain.py           # Order business logic
  store.py            # Order + tx persistence in Redis

payment/
  app.py              # Flask app + RabbitMQ consumer startup
  routes/
    public.py         # Payment CRUD (create_user, find_user, add_funds)
  worker.py           # RabbitMQ consumer: dispatch commands to service
  service.py          # Atomic Lua-based pay/refund/prepare/commit/abort
  lua_scripts.py      # Lua script definitions
  tx_store.py         # Participant tx record persistence

stock/
  app.py              # Flask app + RabbitMQ consumer startup
  routes/
    public.py         # Stock CRUD (create, find, add, subtract)
  worker.py           # RabbitMQ consumer: dispatch commands to service
  service.py          # Atomic Lua-based reserve/release/prepare/commit/abort
  lua_scripts.py      # Lua script definitions
  tx_store.py         # Participant tx record persistence
```

The important constraints are architectural, not naming:

- `common/` is a shared package imported by all three services and the coordinator. It contains message definitions (`msgspec.Struct` types), RabbitMQ helpers, and shared constants. It must not depend on Flask, Redis, or any service-specific code.
- `coordinator/` must not depend on Flask
- `coordinator/` must depend on interfaces/ports, not concrete route modules
- `coordinator/` must access order domain state and tx persistence exclusively through port interfaces defined in `coordinator/ports.py` (see "Coordinator Port Interfaces" section below). In Phase 1 the implementations are direct Redis calls living in `order/store.py`. In Phase 2 the implementations swap to HTTP calls or the orchestrator's own Redis — coordinator code does not change.
- participant workers consume from RabbitMQ and call Lua scripts — they do not import Flask
- the messaging layer is shared but transport-agnostic enough that Phase 2 can replace RabbitMQ with another broker or direct HTTP if needed

### Coordinator Port Interfaces

The coordinator must not call Redis or the order domain directly. All external state access goes through abstract interfaces defined in `coordinator/ports.py`. This is the primary mechanism that makes Phase 2 extraction a swap of implementations rather than a rewrite of protocol logic.

#### `OrderPort`

```python
class OrderPort(Protocol):
    def read_order(self, order_id: str) -> Optional[OrderSnapshot]: ...
    def mark_paid(self, order_id: str, tx_id: str) -> bool: ...
```

- `read_order`: returns the order's items, user_id, total_cost, and paid flag. Used by the coordinator to build the item snapshot and check paid status.
- `mark_paid`: sets `paid = true` on the order domain record. Must be idempotent (no-op if already paid). Returns whether the order was successfully marked.

Phase 1 implementation: direct Redis read/write in `order/store.py`.
Phase 2 implementation: HTTP call to `POST /internal/orders/{order_id}/mark_paid` on the order service.

#### `TxStorePort`

```python
class TxStorePort(Protocol):
    def create_tx(self, tx: CheckoutTxValue) -> None: ...
    def update_tx(self, tx: CheckoutTxValue) -> None: ...
    def get_tx(self, tx_id: str) -> Optional[CheckoutTxValue]: ...
    def get_non_terminal_txs(self) -> list[CheckoutTxValue]: ...
    def set_decision(self, tx_id: str, decision: str) -> None: ...
    def get_decision(self, tx_id: str) -> Optional[str]: ...
    def set_commit_fence(self, order_id: str, tx_id: str) -> None: ...
    def get_commit_fence(self, order_id: str) -> Optional[str]: ...
    def clear_commit_fence(self, order_id: str) -> None: ...
    def acquire_active_tx_guard(self, order_id: str, tx_id: str, ttl: int) -> bool: ...
    def get_active_tx_guard(self, order_id: str) -> Optional[str]: ...
    def clear_active_tx_guard(self, order_id: str) -> None: ...
    def refresh_active_tx_guard(self, order_id: str, ttl: int) -> bool: ...
```

Phase 1 implementation: direct Redis calls in `order/store.py`. All tx keys (`tx:{tx_id}`, `tx_decision:{tx_id}`, `order_active_tx:{order_id}`, `order_commit_fence:{order_id}`) live in the order service's Redis.
Phase 2 implementation: the orchestrator gets its own Redis instance and owns all tx state directly. The `TxStorePort` implementation becomes local Redis calls inside the orchestrator service — no cross-service access needed. The `order_active_tx` and `order_commit_fence` keys move with the coordinator since they are coordinator concerns, not order-domain concerns.

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
- `user_id` (copied from order at tx creation — needed for payment commands)
- `total_cost` (computed from aggregated items — needed for payment commands)
- `protocol` (`saga` or `2pc`)
- `status` (see status constants below)
- `decision` (`commit` or `abort`, only when relevant)
- `items_snapshot` (aggregated, deterministic item list: `list[tuple[item_id, quantity]]`)
- `stock_held` (bool — stock hold/prepare succeeded)
- `stock_committed` (bool — stock commit succeeded, 2PC only)
- `stock_released` (bool — stock release/abort/compensate succeeded)
- `payment_held` (bool — payment hold/prepare succeeded)
- `payment_committed` (bool — payment commit succeeded, 2PC only)
- `payment_released` (bool — payment release/abort/compensate succeeded)
- `last_error` (string, optional)
- `created_at` (epoch ms)
- `updated_at` (epoch ms)
- `retry_count` (int)

This record is the fast-path materialized view used by the coordinator during normal execution.

### Status Constants and State Machine

Terminal statuses (safe to clear active-tx guard):

- `COMPLETED` — checkout fully committed, order marked paid
- `ABORTED` — checkout cleanly aborted, all side effects reversed

Non-terminal statuses (**must NOT clear active-tx guard**):

- `INIT` — tx record created, no participant commands sent yet
- `HOLDING` — hold commands published to participants, waiting for replies
- `HELD` — all holds succeeded, ready for decision (2PC) or finalization (SAGA)
- `COMMITTING` — commit commands published to participants, awaiting confirmations. In 2PC: commit decision persisted before entering this state. In SAGA: order marked paid before entering this state.
- `COMPENSATING` — compensation/abort commands published, waiting for replies
- `FAILED_NEEDS_RECOVERY` — an operation failed and recovery must resolve it

**Critical invariant**: `FAILED_NEEDS_RECOVERY` is NOT terminal. It must NOT clear the active-tx guard. This was the root cause of the duplicate-checkout bug in attempt 2.

**Critical invariant**: participant progress flags (`stock_held`, `payment_held`, etc.) must be updated in the tx record as each reply arrives during `HOLDING`, not deferred until the state transition. If the coordinator crashes mid-HOLDING after receiving one reply, recovery must be able to see which participant succeeded so it can compensate correctly. Without incremental flag updates, recovery sees `HOLDING` with all flags false and cannot distinguish "neither replied" from "one replied but the flag wasn't persisted."

#### SAGA state transitions

```
INIT → HOLDING (hold commands published to stock + payment)
HOLDING → HELD (both holds succeeded)
HOLDING → COMPENSATING (one or both holds failed/timed out; compensate all that may have succeeded)
HELD → COMMITTING (order marked paid, commit commands published to finalize participant tx records)
COMMITTING → COMPLETED (both commits confirmed)
COMMITTING → FAILED_NEEDS_RECOVERY (commit confirmation failed — order is already paid, just need to retry commits)
HELD → COMPENSATING (order mark-paid failed)
COMPENSATING → ABORTED (all compensations succeeded or returned tx_not_found)
COMPENSATING → FAILED_NEEDS_RECOVERY (compensation failed)
FAILED_NEEDS_RECOVERY → COMPENSATING (recovery retries compensation)
FAILED_NEEDS_RECOVERY → COMMITTING (recovery finds order already paid → re-publish commits)
FAILED_NEEDS_RECOVERY → COMPLETED (recovery discovers commits already confirmed)
```

**Stale `HOLDING` recovery**: if the coordinator crashes mid-`HOLDING` (e.g., one participant reply received and its flag persisted, but the process dies before the second reply or status transition), the tx stays in `HOLDING` with partially-updated flags. The recovery worker treats stale `HOLDING` the same as `FAILED_NEEDS_RECOVERY`: it inspects the participant flags to determine which holds succeeded, then compensates or completes forward accordingly. There is no explicit `HOLDING → FAILED_NEEDS_RECOVERY` transition needed — recovery operates directly on the stale `HOLDING` record. The active-tx guard's TTL ensures the guard eventually expires so recovery can acquire it.

SAGA recovery direction:
- `HELD` or `HOLDING` with both `stock_held` and `payment_held` true → complete forward (mark paid, send commits). The `HOLDING` + both-flags-true case means the coordinator crashed after receiving both replies but before transitioning to `HELD`. Note: if the coordinator crashes after marking the order paid but before transitioning to `COMMITTING`, recovery still sees `HELD`. This is safe because `mark paid` is idempotent (a second write is a no-op on an already-paid order). Recovery re-marks paid, publishes commit commands, and transitions to `COMPLETED`.
- `HOLDING` with one hold succeeded, one failed/unknown → compensate all that may have succeeded (including timed-out participants)
- `HOLDING` with neither flag true → no participant succeeded (or coordinator crashed before persisting any flag). Mark aborted. No compensation needed, but recovery may still publish release/refund defensively (participants return `tx_not_found` harmlessly).
- `COMMITTING` → re-publish commit commands (order is already paid, just finalizing participant records)
- `COMPENSATING` → retry compensation

#### 2PC state transitions

```
INIT → HOLDING (prepare commands published to stock + payment)
HOLDING → HELD (both prepares succeeded — "all voted yes")
HOLDING → COMPENSATING (one or both prepares failed/timed out; abort ALL participants including timed-out ones)
HELD → COMMITTING (commit decision persisted + fence set + commit commands published — see write ordering below)
COMMITTING → COMPLETED (all commits confirmed + order marked paid + fence cleared)
COMMITTING → FAILED_NEEDS_RECOVERY (commit confirmation failed)
COMPENSATING → ABORTED (all aborts succeeded or returned tx_not_found)
COMPENSATING → FAILED_NEEDS_RECOVERY (abort failed)
FAILED_NEEDS_RECOVERY → COMMITTING (recovery finds commit decision → re-publish commits)
FAILED_NEEDS_RECOVERY → COMPENSATING (recovery finds no commit decision → presumed abort)
FAILED_NEEDS_RECOVERY → COMPLETED (recovery discovers order already paid)
```

**Stale `HOLDING` recovery (2PC)**: same principle as SAGA. If the coordinator crashes mid-`HOLDING`, recovery inspects the partially-updated participant flags. Since no commit decision can exist yet (decisions are only written after `HELD`), recovery applies presumed abort: it publishes abort commands to all participants that may have prepared. Participants are idempotent — aborting a non-existent prepare returns `tx_not_found` harmlessly.

2PC recovery direction:
- `tx_decision:{tx_id}` exists with value `commit` → re-publish commit commands
- `order_commit_fence:{order_id}` exists → re-publish commit commands
- `HOLDING` with both prepare flags true but no decision marker → presumed abort (decision was never written, so no commit happened). Publish abort commands to both participants.
- `HOLDING` with partial or no flags true → presumed abort. Publish abort commands to all participants that may have prepared (safe — abort on non-existent prepare returns `tx_not_found`).
- all other non-terminal states with no commit decision → presumed abort → publish abort commands

### Structured Application Logging for Protocol Steps

Intermediate protocol steps are logged via structured application logging (stdout/gunicorn), not persisted in Redis.

Recommended log fields per entry:

- `tx_id`
- `step` (e.g., `stock_prepare_requested`, `payment_charged`, `decision_commit`)
- `timestamp`
- `result` (success, failure, timeout)
- `details` (error messages, participant responses)

This provides debugging and auditability without the write overhead of a per-transaction Redis log.

Recovery does not depend on these logs. Recovery uses only:

1. The canonical tx summary record (`tx:{tx_id}`).
2. The durable decision marker (`tx_decision:{tx_id}`).
3. The commit fence (`order_commit_fence:{order_id}`).

If the summary record is ambiguous after a crash (e.g., participants were called but status was not updated), recovery checks the decision marker to determine whether to complete forward or abort.

### Coordinator Metadata

Keep orchestration metadata separate from domain records:

- `order_active_tx:{order_id}` — durable active transaction guard with TTL (replaces both the old lease lock and active-tx guard)
- `tx:{tx_id}` — canonical transaction summary record
- `tx_decision:{tx_id}` — durable commit/abort decision marker
- `order_commit_fence:{order_id}` — commit fence for post-decision crash recovery

This avoids hiding domain state and coordinator state behind one mixed persistence surface.

### Participant Local Transaction Records

Each participant stores per-transaction state by `tx_id`:

- target entity id
- amount
- prepared flag
- committed/applied flag
- aborted/reversed flag
- timestamps

Participant operations must be idempotent by `tx_id`.

### Participant Operations: Unified Model for SAGA and 2PC

SAGA and 2PC perform the same participant-side data operations — they differ only in the coordinator's control flow. The participant layer provides three operations per service that serve both protocols:

#### Stock operations

| Operation | Stock effect | Tx record effect | Used by |
|-----------|-------------|------------------|---------|
| `hold` | check all items exist + sufficient stock → subtract all | create tx record per item (state from command) | SAGA reserve, 2PC prepare |
| `release` | add back all items | mark all tx records released/aborted | SAGA compensate, 2PC abort |
| `commit` | no stock change (already subtracted in hold) | mark all tx records committed | SAGA commit, 2PC commit |

#### Payment operations

| Operation | Credit effect | Tx record effect | Used by |
|-----------|-------------|------------------|---------|
| `hold` | check user exists + sufficient credit → subtract | create tx record (state from command) | SAGA charge, 2PC prepare |
| `release` | add back credit | mark tx record refunded/aborted | SAGA compensate, 2PC abort |
| `commit` | no credit change (already subtracted in hold) | mark tx record committed | SAGA commit, 2PC commit |

The command message specifies which operation (`hold`, `release`, `commit`). Participants dispatch on command type. They do not need to know which protocol the coordinator is running — the command tells them what to do.

**SAGA sends `commit` too**: after both holds succeed in SAGA, the coordinator sends `commit` commands to both participants (same as 2PC). This gives participant tx records a clean terminal state (`committed`) in both protocols. Without this, SAGA participant tx records would stay in `held` status indefinitely, making it impossible to distinguish "active hold waiting for decision" from "completed SAGA transaction" without querying the coordinator. The `commit` command for SAGA is a no-op on the data side (stock is already subtracted, credit already taken) — it only updates the participant tx record status.

Public endpoints (`/stock/subtract`, `/stock/add`, `/payment/pay`, `/payment/add_funds`) use separate simpler Lua scripts with no tx record. These are independent of the transaction system.

#### Batch stock Lua script (hold)

The stock `hold` operation receives the full aggregated item list in one message and processes all items atomically in one Lua script invocation:

```lua
-- KEYS[1..n] = item keys (e.g., "item:{item_id}" for each unique item)
-- ARGV[1] = tx_id
-- ARGV[2] = JSON-encoded array of {item_id, quantity} pairs
-- ARGV[3..3+n-1] = quantities corresponding to each KEYS entry (for Redis cluster compat, though not needed in Phase 1)

-- Idempotency check FIRST: if this tx_id was already processed, return
-- the previous result immediately. This must precede validation because
-- stock levels may have changed since the original call (consumed by
-- other transactions), and re-validating would return a spurious
-- insufficient_stock error on a legitimate replay.
local existing_tx = redis.call('GET', 'stock_tx:' .. ARGV[1])
if existing_tx then
    return existing_tx  -- already processed, return previous result
end

-- First pass: validate all items exist and have sufficient stock
for i = 1, #KEYS do
    local data = redis.call('GET', KEYS[i])
    if not data then
        return cjson.encode({ok=false, error="not_found", item=KEYS[i]})
    end
    local item = cmsgpack.unpack(data)  -- or cjson.decode if using JSON
    local qty = tonumber(ARGV[2 + i])
    if item.stock < qty then
        return cjson.encode({ok=false, error="insufficient_stock", item=KEYS[i]})
    end
end

-- Second pass: subtract all items (only reached if all checks passed)
for i = 1, #KEYS do
    local data = redis.call('GET', KEYS[i])
    local item = cmsgpack.unpack(data)
    local qty = tonumber(ARGV[2 + i])
    item.stock = item.stock - qty
    redis.call('SET', KEYS[i], cmsgpack.pack(item))
end

-- Create tx record for idempotency and recovery
local tx_record = {items=..., status="held", created_at=...}
redis.call('SET', 'stock_tx:' .. ARGV[1], cjson.encode(tx_record))

return cjson.encode({ok=true})
```

Key properties:

1. **All-or-nothing**: if any item check fails, nothing is modified. No partial subtraction.
2. **Idempotent**: if called again with the same `tx_id`, returns the previous result.
3. **Atomic**: single Lua script execution, no interleaving with other Redis commands.
4. **Multi-item**: all items in one invocation, not one network round trip per item.

This works because each service has a single Redis instance (not a Redis Cluster). All item keys are on the same instance, so multi-key Lua is safe.

The `release` and `commit` scripts follow the same pattern: look up the tx record by `tx_id`, modify the item data (for release) or just the tx record (for commit), and return.

#### Payment Lua script (hold)

Payment `hold` is simpler — one user, one amount:

```lua
-- KEYS[1] = user key
-- ARGV[1] = tx_id
-- ARGV[2] = amount

-- Idempotency check
local existing_tx = redis.call('GET', 'payment_tx:' .. ARGV[1])
if existing_tx then return existing_tx end

-- Check user exists and has sufficient credit
local data = redis.call('GET', KEYS[1])
if not data then return cjson.encode({ok=false, error="not_found"}) end
local user = cmsgpack.unpack(data)
local amount = tonumber(ARGV[2])
if user.credit < amount then
    return cjson.encode({ok=false, error="insufficient_credit"})
end

-- Subtract and persist
user.credit = user.credit - amount
redis.call('SET', KEYS[1], cmsgpack.pack(user))

-- Create tx record
redis.call('SET', 'payment_tx:' .. ARGV[1], cjson.encode({
    user_id=KEYS[1], amount=amount, status="held"
}))

return cjson.encode({ok=true})
```

#### Serialization note for Lua scripts

Redis Lua has built-in `cjson` and `cmsgpack` libraries. Since the existing codebase uses `msgspec` msgpack for Redis values, the Lua scripts should use `cmsgpack.unpack()` / `cmsgpack.pack()` to read/write the same format. If `cmsgpack` proves difficult to work with for complex structs, an alternative is to store item stock and price as separate Redis hash fields (`HGET`/`HSET`) instead of a single serialized blob — this avoids serialization inside Lua entirely. Either approach is acceptable; the key requirement is atomicity.

### Stale-Hold Cleanup

Stale prepared holds are cleaned up by the coordinator's recovery worker, not unilaterally by participants.

Why:

1. Participants cannot know whether the coordinator decided to commit or abort a transaction.
2. A participant that times out and releases a prepared hold could violate a commit decision.
3. The coordinator has the authoritative decision state and can safely issue `abort` calls.

If a participant needs to query the coordinator's decision (e.g., during startup reconciliation), the order service exposes:

- `GET /internal/tx_decision/{tx_id}` — returns `commit`, `abort`, or `unknown`

Participants may use this during startup to reconcile non-terminal local tx records, but they must not unilaterally abort holds when the answer is `unknown`.

### Message Contract

All RabbitMQ messages use `msgspec` msgpack serialization — the same library already used for Redis values. This keeps a single serialization dependency across the entire codebase.

Message definitions live in `common/models.py` as `msgspec.Struct` types. All three services and the coordinator import from this shared package. This is the single source of truth for the wire format.

#### `common/models.py` — message definitions

```python
import msgspec
from typing import Optional

class StockHoldPayload(msgspec.Struct):
    items: list[tuple[str, int]]  # [(item_id, quantity), ...]

class PaymentHoldPayload(msgspec.Struct):
    user_id: str
    amount: int

class ParticipantCommand(msgspec.Struct):
    tx_id: str
    command: str          # "hold" | "release" | "commit"
    reply_to: str         # "coordinator.replies.{process_uuid}"
    stock_payload: Optional[StockHoldPayload] = None
    payment_payload: Optional[PaymentHoldPayload] = None

class ParticipantReply(msgspec.Struct):
    tx_id: str
    service: str          # "stock" | "payment"
    command: str          # "hold" | "release" | "commit"
    ok: bool
    error: Optional[str] = None   # error code when ok=False
    details: Optional[dict] = None
```

Encoding/decoding:

```python
encoder = msgspec.msgpack.Encoder()
decoder = msgspec.msgpack.Decoder(ParticipantCommand)  # or ParticipantReply

# Publish
body = encoder.encode(command)
channel.basic_publish(..., body=body)

# Consume
command = decoder.decode(body)
```

Error codes for `ParticipantReply.error`: `"not_found"`, `"insufficient_stock"`, `"insufficient_credit"`, `"already_held"`, `"already_released"`, `"already_committed"`, `"tx_not_found"`.

#### `common/constants.py` — shared enums

```python
# Command names
CMD_HOLD = "hold"
CMD_RELEASE = "release"
CMD_COMMIT = "commit"

# Service names
SVC_STOCK = "stock"
SVC_PAYMENT = "payment"

# Tx status constants (also used by coordinator/models.py)
STATUS_INIT = "init"
STATUS_HOLDING = "holding"
STATUS_HELD = "held"
STATUS_COMMITTING = "committing"
STATUS_COMPENSATING = "compensating"
STATUS_COMPLETED = "completed"
STATUS_ABORTED = "aborted"
STATUS_FAILED_NEEDS_RECOVERY = "failed_needs_recovery"

TERMINAL_STATUSES = {STATUS_COMPLETED, STATUS_ABORTED}
```

#### Why msgpack everywhere (not JSON for messages)

1. The codebase already depends on `msgspec` for Redis. One serialization library means one set of Struct definitions that work for both Redis and RabbitMQ.
2. `msgspec.msgpack` is significantly faster than `json.dumps`/`json.loads` — this matters under high message throughput.
3. RabbitMQ management UI debugging is still possible via application-level structured logging (which already logs tx_id, command, and result for every message).
4. Shared `msgspec.Struct` types in `common/models.py` give type safety and validation on both sides of the wire — malformed messages fail at decode time with a clear error.

### Docker-Compose Changes

Add RabbitMQ to docker-compose:

```yaml
rabbitmq:
  image: rabbitmq:3-management
  ports:
    - "5672:5672"     # AMQP
    - "15672:15672"   # Management UI (useful for debugging)
  environment:
    RABBITMQ_DEFAULT_USER: guest
    RABBITMQ_DEFAULT_PASS: guest
  healthcheck:
    test: ["CMD", "rabbitmqctl", "status"]
    interval: 10s
    timeout: 5s
    retries: 5
```

Add to all services:

```yaml
environment:
  - RABBITMQ_URL=amqp://guest:guest@rabbitmq:5672/
```

Add to order-service:

```yaml
environment:
  - STOCK_SERVICE_URL=http://stock-service:5000
  - PAYMENT_SERVICE_URL=http://payment-service:5000
  - CHECKOUT_PROTOCOL=saga  # or 2pc
depends_on:
  rabbitmq:
    condition: service_healthy
```

Add to stock-service and payment-service:

```yaml
environment:
  - ORDER_SERVICE_URL=http://order-service:5000
  - CHECKOUT_PROTOCOL=saga  # must match order-service
depends_on:
  rabbitmq:
    condition: service_healthy
```

Update gunicorn commands:

```yaml
order-service:
  command: gunicorn -b 0.0.0.0:5000 -w 8 --timeout 30 --log-level=info app:app

stock-service:
  command: gunicorn -b 0.0.0.0:5000 -w 4 --timeout 30 --log-level=info app:app

payment-service:
  command: gunicorn -b 0.0.0.0:5000 -w 4 --timeout 30 --log-level=info app:app
```

### Eliminate GATEWAY_URL from order service

The current `order/app.py` routes **all** internal calls through the nginx gateway via `GATEWAY_URL`. Every one of these must be migrated to direct service URLs:

| Current call (via gateway)                              | Replacement (direct)                                  | Used in            |
|---------------------------------------------------------|-------------------------------------------------------|--------------------|
| `{GATEWAY_URL}/stock/find/{item_id}`                    | `{STOCK_SERVICE_URL}/find/{item_id}`                  | `addItem` (lookup) |
| `{GATEWAY_URL}/stock/add/{item_id}/{quantity}`          | handled by RabbitMQ `release` command                 | checkout rollback  |
| `{GATEWAY_URL}/stock/subtract/{item_id}/{quantity}`     | handled by RabbitMQ `hold` command                    | checkout           |
| `{GATEWAY_URL}/payment/pay/{user_id}/{amount}`          | handled by RabbitMQ `hold` command                    | checkout           |

After the rebuild:
- `GATEWAY_URL` is removed entirely from `order/app.py` and `docker-compose.yml`.
- `STOCK_SERVICE_URL` and `PAYMENT_SERVICE_URL` are the only service-to-service env vars.
- Non-transactional HTTP lookups (e.g., `addItem` fetching item price) use direct URLs.
- All transactional operations (hold/release/commit) go through RabbitMQ — no HTTP at all.
- The nginx gateway handles **only** external client traffic.

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

Two mechanisms exist and they serve different purposes.

### Active Transaction Guard (with TTL)

The active transaction guard (`order_active_tx:{order_id}`) is a durable pointer from order to the current in-flight checkout transaction, stored in Redis with a TTL.

Its job is:

- prevent a new checkout from starting while an earlier transaction is still non-terminal or not yet safe to re-enter
- provide request-path mutual exclusion (replaces the need for a separate lease lock)

It must outlive individual requests and must not be cleared except when the transaction reaches a safe-to-reenter terminal state.

The TTL serves as a safety net: if the coordinator crashes without cleaning up, the guard expires after a bounded time, allowing the recovery worker to take over. The TTL should be generous (e.g., 30-60 seconds) — long enough that it never expires during normal operation, short enough that crashed transactions don't block forever.

Why a single mechanism instead of separate lease lock + active-tx guard:

1. Two locking mechanisms with different lifetimes create interaction bugs (lease expires while guard is still set, guard cleared while lease is held by another request, etc.).
2. The active-tx guard already prevents concurrent checkouts. A separate lease lock's only advantage is self-expiry on crash — but a TTL on the guard achieves the same thing.
3. The recovery worker handles stuck transactions regardless, so the TTL is defense-in-depth, not the primary recovery mechanism.
4. Fewer locking primitives means fewer correctness edge cases to reason about and test.

Implementation:

- use `SET order_active_tx:{order_id} {tx_id} NX EX {ttl}` to atomically acquire
- the `NX` flag prevents concurrent acquisition
- refresh the TTL during long operations if needed
- clear explicitly on terminal state
- recovery worker picks up expired guards

### Prepared Holds

Prepared holds live in participant stores.

Their job is:

- protect stock and funds between `prepare` and `commit/abort`
- survive coordinator or participant restarts

Prepared holds are part of transaction durability, not request mutual exclusion.

Cleanup of stale prepared holds is coordinator-driven: the recovery worker on the order service detects non-terminal transactions and issues explicit `abort` calls to participants. Participants must not unilaterally clean up their own holds based on timeouts, because they cannot know whether the coordinator decided to commit. If a participant needs to query the coordinator's decision, the order service exposes `GET /internal/tx_decision/{tx_id}` which returns the durable decision (commit, abort, or unknown).

## Performance Strategy

Phase 1 prioritizes raw request throughput on the benchmark-visible path while preserving correctness.

The practical strategy is:

1. Use RabbitMQ for parallel fan-out to stock and payment. This cuts the dominant latency cost (participant round trips) roughly in half compared to serial HTTP calls.
2. Make the already-paid `200` path extremely cheap — pure Redis read, no RabbitMQ interaction.
3. Minimize Redis round trips per participant operation. Lua scripts do read-check-modify-write in one atomic step.
4. Keep long-running recovery and reconciliation out of request threads.
5. Use direct HTTP (bypassing nginx) only for non-transactional lookups (item price, user info). All transaction operations go through RabbitMQ.
6. Use persistent RabbitMQ connections with channel pooling. Use `requests.Session` with connection pooling for any remaining HTTP calls.
7. Keep serialization and Redis/message payloads compact (msgspec msgpack for both Redis values and RabbitMQ messages).
8. Fail fast on overload or ambiguity instead of stalling enough requests to blow the latency budget.
9. Use Lua scripts for all participant-side atomic operations. This is both a correctness and performance requirement.
10. Set gunicorn worker counts high enough for the architecture. Starting values: `8` workers for order, `4` for stock and payment. Tune based on benchmark results.
11. Send one message per participant per protocol phase, not one message per item. The stock message contains the full aggregated item list; the stock service processes all items in one Lua script invocation.

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
2. The real checkout path benefits from parallel participant operations via RabbitMQ:
   - publish prepare/reserve to stock and payment simultaneously
   - wait for both replies in parallel
   - no gateway hairpin for any internal call
   - no duplicate state writes
3. Any design that turns one logical transition into many separate serial remote operations is suspect. Parallel fan-out is the mitigation.

## Fast Already-Paid Path

Because the benchmark may repeatedly call checkout on the same order IDs, the already-paid path should be intentionally optimized.

Fast-path rule:

1. Read a lightweight order summary first.
2. If `paid == true` and there is no active transaction and no commit fence, return `200` immediately.
3. Do not create a new checkout tx for that no-op case.
4. Only enter full coordination for unpaid or ambiguous orders.

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
3. Prefer batched internal stock operations per transaction. Send one message to the stock service containing the full aggregated item list, not one message per item.
4. Application-level structured logs should record meaningful transition boundaries, not one verbose event per raw line item.
5. If the external benchmark can generate pathologically large orders, that should be treated as a performance risk to design around, not as a harmless corner case.

If internal batching is not implemented immediately, the plan must assume order-size limits will materially affect latency.

Why this matters:

1. The benchmark's create-order scenario can produce very large orders because it randomly picks an item count from a wide seeded range.
2. A naive "one message per line item" protocol path will collapse under such inputs.
3. Logging every tiny step can become as expensive as the business work itself.

## Request Flow

The `POST /orders/checkout/{order_id}` route should do only this:

1. Validate the order exists.
2. Check the fast already-paid path using lightweight summary state.
3. Acquire the active-tx guard (`SET order_active_tx:{order_id} {tx_id} NX EX {ttl}`) for unpaid or ambiguous orders.
4. If acquisition fails, check whether the existing active tx is still live or expired/terminal. Reject with `409` if a non-terminal active transaction already exists.
5. Create a canonical transaction record before the first participant call.
6. Call `CoordinatorService.execute_checkout(...)`.
7. Translate the structured result into `2xx` or `4xx`.
8. Clear the active-tx guard on terminal state.

The route must not contain protocol logic.

The route also must not:

1. hide recovery policy inside ad hoc conditional branches
2. duplicate commit-fence logic that exists elsewhere
3. turn already-paid no-op handling into a full transaction path

## Protocol Responsibilities

### SAGA

Success path:

1. Persist `INIT`.
2. Publish hold commands to stock and payment **in parallel** via RabbitMQ (one message to stock queue, one to payment queue, both containing aggregated item/amount data).
3. Wait for both participant replies on the coordinator reply queue (bounded timeout).
4. If both succeed: mark order paid, publish `commit` commands to both participants (finalizes participant tx records), mark transaction completed.

Failure path:

1. If stock reserve fails but payment charge succeeded: publish refund command to payment. Wait for confirmation. Mark aborted.
2. If payment charge fails but stock reserve succeeded: publish release command to stock. Wait for confirmation. Mark aborted.
3. If both definitively fail (explicit failure replies): mark aborted (no compensation needed).
4. If a participant times out (no reply within deadline): treat as "possibly succeeded" for compensation purposes. Always publish release/refund to timed-out participants — the participant's idempotent Lua script will no-op if the hold never happened (`tx_not_found`). Compensate the other participant if it succeeded. Mark `FAILED_NEEDS_RECOVERY` if compensation also fails.
5. If both participants time out: publish release/refund to both. This is safe because release/refund on a non-existent tx record returns a harmless `tx_not_found` error. Mark `FAILED_NEEDS_RECOVERY` if either compensation fails; mark aborted if both succeed or return `tx_not_found`.

Recovery policy:

- If the tx summary shows stock reserved and payment charged but the order is not yet marked paid, recovery **completes forward** (marks paid and completes the transaction). The charge already happened; reversing it risks the refund failing, which would leave the system in a worse state than completing forward.
- If the tx summary shows stock reserved but payment not yet charged (or timed out), recovery compensates backward (publishes release to stock, marks aborted).
- If the tx summary shows payment charged but stock not reserved (or timed out), recovery compensates backward (publishes refund to payment, marks aborted).
- Both partial-success cases are possible because stock and payment execute in parallel.
- Compensation operations (refund, release) must be idempotent so that replayed recovery is safe.

### App-Level 2PC

Success path:

1. Persist `INIT`.
2. Publish prepare commands to stock and payment **in parallel** via RabbitMQ (stock prepare with aggregated items, payment prepare with user_id and total_cost).
3. Wait for both prepare replies on the coordinator reply queue (bounded timeout).
4. If both prepared successfully, execute the commit sequence in this exact order:
   a. Write `tx_decision:{tx_id} = commit` (durable decision marker — must be first).
   b. Write `order_commit_fence:{order_id} = tx_id` (commit fence).
   c. Update tx status to `COMMITTING` (only after decision marker exists).
   d. Publish commit commands to stock and payment **in parallel** via RabbitMQ.
5. Wait for both commit confirmations.
6. Finalize order as paid.
7. Clear fence and mark completed.

**Critical write ordering**: the decision marker (4a) must be persisted before the status changes to COMMITTING (4c). If the coordinator crashes after writing COMMITTING but before writing the decision marker, recovery would find COMMITTING + no decision → presumed abort, contradicting the intended commit. Writing the decision first ensures recovery always has the authoritative decision available.

Failure path:

1. If either prepare fails or times out: persist durable abort decision.
2. Publish abort commands to **all** participants (in parallel) — including timed-out ones that may have prepared. A timed-out participant may have executed the prepare; aborting it is safe because the abort Lua script no-ops with `tx_not_found` if the prepare never happened.
3. Wait for abort confirmations.
4. Mark terminal abort state.

Recovery policy:

- Presumed abort when no durable commit decision exists.
- If commit decision exists but commits are incomplete: re-publish commit commands. Participants are idempotent by tx_id, so re-delivery is safe.
- If commit fence exists but order is not yet marked paid: complete forward (mark paid, clear fence).
- Timeout on prepare reply is treated as "unknown, possibly prepared" — abort is the safe choice because no commit decision has been written yet.

## Recovery Model

Recovery is part of Phase 1 and remains embedded in the `order` service.

Rules:

1. The recovery worker scans all non-terminal transactions on startup.
2. It runs periodically afterward.
3. It resumes work by calling a coordinator recovery entrypoint, not by reimplementing protocol logic in route code.
4. Recovery re-publishes commands to RabbitMQ (commit, abort, compensate) the same way the normal checkout path does. Participants are idempotent by tx_id, so re-delivery is safe.
5. Request-time healing is limited to 2PC finalization after a known commit decision (the commit-fence case).
6. All other long-running repair happens in background recovery.
7. Recovery consults the canonical tx summary record first, then the durable decision marker (`tx_decision:{tx_id}`) when the summary is ambiguous.

This keeps synchronous request handling bounded while still satisfying consistency requirements.

Replica-safety:

- Phase 1 runs a single `order` service instance. The recovery worker does not need a distributed leader lock.
- If horizontal scaling is added later, a distributed leader lock should be introduced at that point.
- This is documented as a known scaling limitation, not deferred indefinitely — but implementing and testing leader election before the basic protocol works is premature.

Additional recovery principles:

1. Recovery must be idempotent when replayed or invoked twice.
2. Recovery is allowed to be conservative and slower than the request path.
3. Recovery must prefer invariant safety over fast unblocking.
4. If recovery and normal request handling disagree, the durable transaction state wins over in-memory assumptions.
5. SAGA recovery completes forward after a successful charge (see SAGA protocol section). It only compensates backward on explicit failures or when charge has not yet happened.

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

The program is organized into 5 consolidated steps. Each step produces working, testable code.

The previous 10-step program was too granular for the available timeline. This consolidation merges steps that have no meaningful independent deliverable and reduces per-step ceremony while preserving the same correctness gates.

### Step 1: Foundation — atomic participants, RabbitMQ, data model, and deployment fixes

What should happen:

1. Fix all participant read-modify-write operations to use Lua scripts:
   - `/stock/subtract`, `/stock/add` — atomic stock modification
   - `/payment/pay`, `/payment/add_funds` — atomic credit modification
   - This is the highest-priority correctness fix; without it, no higher-level protocol matters.
2. Add RabbitMQ to docker-compose:
   - use `rabbitmq:3-management` image for local development (includes management UI for debugging)
   - configure durable queues: `stock.commands`, `payment.commands`
   - reply queues are per-process exclusive auto-delete queues (`coordinator.replies.{uuid}`), declared at worker startup — not pre-configured as durable queues
   - configure dead-letter queues: `stock.commands.dlq`, `payment.commands.dlq`
   - all messages persistent (delivery_mode=2)
   - add `RABBITMQ_URL` environment variable to all services
3. Implement the `common/` shared package (imported by all services):
   - `common/models.py`: `msgspec.Struct` definitions for `ParticipantCommand`, `ParticipantReply`, payload types
   - `common/constants.py`: command names, service names, status constants, terminal status set
   - `common/messaging.py`: RabbitMQ connection helpers (connect, publish, consume), thread-local publisher connections (as attempt 1 correctly did)
   - `common/result.py`: structured result types for coordinator → route communication
   - all RabbitMQ messages serialized with `msgspec.msgpack` (same library as Redis values)
   - correlation ID for matching replies to requests
4. Implement participant RabbitMQ consumers in stock and payment:
   - consume from `stock.commands` / `payment.commands`
   - dispatch to Lua-based atomic operations
   - publish results to `coordinator.replies`
   - all operations idempotent by `tx_id`
5. Implement the canonical checkout transaction summary record and coordinator metadata keys:
   - `order_active_tx:{order_id}` (with TTL)
   - `tx:{tx_id}`
   - `tx_decision:{tx_id}`
   - `order_commit_fence:{order_id}`
6. Separate domain persistence from orchestration persistence at the code level.
7. Add `STOCK_SERVICE_URL` and `PAYMENT_SERVICE_URL` for non-transactional HTTP lookups; update `addItem` to use direct stock URL.
8. Increase gunicorn workers: `-w 8` for order, `-w 4` for stock and payment.
9. Add `GET /internal/tx_decision/{tx_id}` to the order service for participant startup reconciliation queries.
10. Create the coordinator package skeleton with result types and protocol interface. Verify it has no Flask imports.
11. Set up the `CHECKOUT_PROTOCOL` environment variable with fail-fast validation.

Key design choices from attempt 1 to preserve:

- Thread-local publisher connections (avoids pika channel-sharing bugs under concurrent requests)
- Persistent messages with delivery_mode=2
- Inbox/effect markers for idempotency within Lua scripts (attempt 1's Lua scripts already did this correctly)
- DLQ for poison messages

Key design choices from attempt 1 to avoid:

- No Redis polling loop for checkout response — coordinator blocks on reply queue instead
- No separate worker process for orchestration — coordinator is in-process
- No complex retry-queue topology (main → retry → main loop) — keep it simple: DLQ + recovery worker re-publish
- No 7-day idempotency marker TTL — use TTL appropriate for the recovery window (e.g., 1 hour)

Validation:

1. Write unit tests proving Lua-based subtract/add are atomic under concurrent access.
2. Write idempotency tests for repeated `tx_id` participant operations (via RabbitMQ consumer).
3. Write serialization/deserialization tests for `common/models.py` Struct types (round-trip encode/decode for all message types).
4. Write a configuration test that invalid protocol values fail fast.
5. Verify existing public API tests still pass.
6. Verify RabbitMQ queues are created and messages flow end-to-end (publish command → consume → process → publish reply → consume reply).

Acceptance criteria:

1. No read-modify-write race condition exists in any participant operation.
2. Participant RabbitMQ consumers are idempotent and use durable holds for 2PC prepare.
3. Messages flow correctly through RabbitMQ queues.
4. Non-transactional HTTP calls bypass the gateway.
5. Worker counts are sufficient for concurrent load.
6. Coordinator package is importable without Flask.

### Step 2: Coordinator and protocols — SAGA and 2PC with parallel RabbitMQ fan-out

What should happen:

1. Implement `CoordinatorService.execute_checkout(order_id, order_snapshot, mode)`:
   - publishes commands to stock and payment queues in parallel
   - blocks on coordinator reply queue with bounded timeout for both participant results
   - makes commit/abort decision based on results
   - publishes commit/abort commands in parallel
   - blocks on confirmations
2. Implement `CoordinatorService.resume_transaction(tx_id)` for recovery.
3. Implement the SAGA protocol:
   - publish reserve-stock + charge-payment **in parallel** → wait for both replies
   - if both succeed → mark paid → complete
   - if either fails → publish compensation for the other **in parallel** → mark aborted
   - forward recovery after successful charge (complete forward, don't reverse)
4. Implement the 2PC protocol:
   - publish prepare-stock + prepare-payment **in parallel** → wait for both replies
   - if both prepared → persist commit decision → set fence → publish commit-stock + commit-payment **in parallel** → wait for confirmations → mark paid → clear fence → complete
   - if either prepare fails/times out → persist abort decision → publish abort **in parallel** → mark aborted
   - presumed abort when no durable commit decision exists
5. Implement the already-paid fast path:
   - if `paid == true` and no active tx and no commit fence → return `200` immediately
   - no tx record created, no RabbitMQ interaction, no side effects
6. Wire the checkout route as a thin adapter:
   - validate order exists
   - check fast path
   - acquire active-tx guard (`SET NX EX`)
   - if acquisition fails: check if existing tx is terminal. If non-terminal → `409`. If terminal/expired → clean up and retry acquisition.
   - call coordinator
   - map structured result to HTTP
   - clear guard on terminal state

Critical `FAILED_NEEDS_RECOVERY` handling (from attempt 2 bug):

- `FAILED_NEEDS_RECOVERY` is **NOT** a terminal status for the purpose of clearing the active-tx guard.
- The guard must remain in place until recovery moves the transaction to `COMPLETED` or `ABORTED`.
- If a new checkout request arrives and finds an active-tx guard pointing to a `FAILED_NEEDS_RECOVERY` transaction, it returns `409`. The recovery worker will handle it.

Timeout handling for RabbitMQ replies:

- If a participant reply does not arrive within the deadline (e.g., 5 seconds), the coordinator treats the timed-out participant as "possibly succeeded."
- For SAGA: always publish release/refund to timed-out participants (safe because the Lua script no-ops with `tx_not_found` if the hold never happened). Also compensate the other participant if it explicitly succeeded.
- For 2PC: abort all participants (including timed-out ones). No commit decision has been written yet, so presumed abort is safe.
- The transaction is marked `FAILED_NEEDS_RECOVERY` if compensation/abort also fails.
- The recovery worker will re-publish commands later. Participants are idempotent, so re-delivery is safe.

Validation:

1. Write protocol-unit tests (no Flask) for:
   - SAGA happy path (parallel hold → commit), stock rejection, payment rejection, both reject
   - SAGA both-timeout: verify release/refund published to both timed-out participants (no orphaned holds)
   - SAGA single-timeout: verify timed-out participant gets release/refund and succeeded participant gets compensated
   - 2PC happy path (parallel prepare → commit decision → parallel commit), prepare rejection, timeout, crash after commit decision
   - 2PC write ordering: verify decision marker written before status changes to COMMITTING
   - duplicate compensation/commit replay
   - SAGA partial success (stock OK, payment fail → compensate stock; payment OK, stock fail → compensate payment)
   - partial reply persistence: verify participant flags updated during HOLDING (not deferred to transition)
2. Write integration tests exercising the checkout route in both modes.
3. Write tests proving already-paid returns `200` with no side effects.
4. Write tests proving concurrent checkout on the same order produces at most one outcome.
5. Write tests proving `FAILED_NEEDS_RECOVERY` does not clear the active-tx guard.

Acceptance criteria:

1. Both protocols produce correct outcomes on the happy path and all failure paths.
2. Stock and payment operations execute in parallel (not serial).
3. No stock or payment drift on any failure, including partial success in parallel execution.
4. The route is thin and protocol-agnostic.
5. The already-paid path is cheap, side-effect free, and involves no RabbitMQ interaction.
6. `FAILED_NEEDS_RECOVERY` never clears the active-tx guard.

### Step 3: Recovery worker

What should happen:

1. Implement startup scanning for non-terminal transactions.
2. Implement periodic recovery scanning.
3. Use `CoordinatorService.resume_transaction(...)` for all repair work.
4. Recovery re-publishes commands to RabbitMQ (the same queues as normal checkout). Participants are idempotent by tx_id, so re-delivery is safe.
5. Recovery for SAGA:
   - stock reserved + payment charged → complete forward (mark paid)
   - stock reserved + payment not charged → publish release to stock
   - payment charged + stock not reserved → publish refund to payment
   - neither succeeded → mark aborted (nothing to compensate)
6. Recovery for 2PC:
   - commit decision exists → re-publish commit commands to all participants
   - no commit decision → presumed abort → publish abort commands to all participants
   - commit fence exists but order not paid → complete forward
7. Recovery issues explicit abort/release commands for stale prepared holds.

Validation:

1. Write tests for duplicate recovery invocation; replay must be idempotent.
2. Write service-restart tests for both SAGA and 2PC interrupted flows.
3. Write tests proving recovery converges crashed transactions to terminal states.
4. Write tests proving recovery does not race with active request-path coordination (active-tx guard prevents overlap).

Acceptance criteria:

1. A service restart converges all interrupted transactions.
2. Recovery logic is not duplicated — it goes through the coordinator, which re-publishes to the same RabbitMQ queues.
3. Stale prepared holds are cleaned up by the coordinator, not unilaterally by participants.

### Step 4: End-to-end validation and benchmark tuning

What should happen:

1. Run the full test suite in `saga` mode, then `2pc` mode.
2. Run the provided benchmark (`wdm-project-benchmark`):
   - consistency test as an invariant checker
   - reused-order stress test for headline throughput
   - create-order stress test for committed checkout throughput
3. Identify and fix any consistency failures or performance bottlenecks.
4. Add tests for any discovered gaps, especially:
   - large-order behavior with aggregated items
   - concurrent duplicate checkouts
   - commit-fence recovery
5. Tune worker counts and connection pooling based on benchmark results.

Acceptance criteria:

1. Consistency test passes with zero stock/payment drift in both modes.
2. Performance is reasonable for the synchronous architecture (specific numbers depend on hardware).
3. Both throughput metrics (headline and committed) are measured and reported separately.

### Step 5: Container fault tolerance

What should happen:

1. Test single-container failure scenarios:
   - kill order service during checkout → recovery converges after restart
   - kill stock service during checkout → coordinator detects failure, aborts or retries
   - kill payment service during checkout → same
   - kill a Redis instance → service reconnects, recovery picks up
   - kill a RabbitMQ instance → the remaining replica takes over; at most one container is killed at a time, so the RabbitMQ cluster maintains quorum and durable queues remain available without manual intervention
2. Fix any issues discovered during fault injection.
3. Verify the system remains consistent after each failure scenario.

Validation:

1. Script the failure scenarios (docker kill + docker start).
2. Run consistency checks after each recovery.

Acceptance criteria:

1. Single-container failure does not cause permanent inconsistency.
2. The system recovers to a consistent state within a bounded time after the failed container restarts.

## Testing Priorities

The following scenarios must be covered before considering the rebuild stable.

1. **Participant atomicity**: concurrent subtract/add calls on the same item/user produce correct totals (Lua script correctness).
2. Happy-path checkout succeeds in `saga` (parallel reserve + charge).
3. Happy-path checkout succeeds in `2pc` (parallel prepare → decision → parallel commit).
4. Out-of-stock aborts cleanly in both modes (including compensating the payment participant that succeeded in parallel).
5. Insufficient credit aborts cleanly in both modes (including compensating the stock participant that succeeded in parallel).
6. Duplicate checkout of an already-paid order returns `200` with no extra side effects and no RabbitMQ interaction.
7. Concurrent checkout requests for the same order do not double-apply side effects.
8. Crash after 2PC commit decision but before order finalization recovers to a consistent result.
9. Crash during SAGA after successful charge completes forward (marks paid, does not reverse).
10. Crash during SAGA before charge compensates backward (releases stock).
11. Replayed participant commands via RabbitMQ are idempotent.
12. The already-paid fast path returns `200` without entering full transaction coordination.
13. Single-container failure (order, stock, payment, Redis, or RabbitMQ) followed by restart converges to consistent state.
14. `FAILED_NEEDS_RECOVERY` does not clear the active-tx guard (attempt 2 bug — must not recur).
15. RabbitMQ reply timeout triggers correct compensation/abort behavior — including releasing timed-out participants that may have succeeded.
16. Participant timeout (one participant replies, other times out) triggers correct partial-failure handling in both modes.
17. Both-timeout scenario: both participants time out → release/abort published to both → no orphaned holds.
18. Coordinator crash mid-HOLDING with one reply received → recovery sees partial flags → compensates correctly.
19. 2PC commit decision marker is written before tx status changes to COMMITTING (write ordering).
20. SAGA sends commit to participants after marking order paid → participant tx records reach terminal state.

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

### No serial HTTP-only checkout path (REVERSED)

Previously rejected message bus on the checkout path. Now reversed: RabbitMQ is used for internal coordinator↔participant transport with parallel fan-out. See the "Use RabbitMQ" decision record above for full rationale.

The key differences from the original rejection:
1. We now use RabbitMQ for parallel fan-out, not as a polling-based orchestration layer.
2. The coordinator still blocks synchronously on reply queues — no Redis polling loop like attempt 1.
3. The assignment explicitly rewards event-driven architecture, and the lecturer advises for it.

### No timestamp-ordering-first design

Rejected for Phase 1 because:

1. It is harder to explain and recover than reservation-based prepared holds.
2. It turns more conflicts into retries.
3. It is a worse fit for explicit 2PC participant state.

### No full event-sourced rebuild

Rejected for Phase 1 because:

1. It is architecturally larger than needed.
2. It complicates reads and debugging of business-state queries.
3. A hybrid summary record + durable decision marker + application logs captures the valuable parts with much less cost.

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
6. The coordinator accesses order domain state and tx persistence exclusively through `OrderPort` and `TxStorePort` interfaces, so it can be moved into a standalone service by swapping implementations alone.
7. Performance measurements explicitly separate no-op request throughput from committed checkout throughput.
8. Critical logic remains modular, with no duplicated recovery/protocol paths.
9. Non-obvious correctness-critical code paths are commented clearly enough to explain why they are safe.
10. All participant read-modify-write operations are atomic (Lua scripts).
11. Internal transaction operations use RabbitMQ with msgspec msgpack serialization. Non-transactional lookups use direct HTTP. Nothing routes through nginx internally.
12. All RabbitMQ message types are defined as `msgspec.Struct` in `common/models.py` and imported by all services.
13. Gunicorn worker counts are tuned for the architecture.
14. Single-container failure followed by restart converges to a consistent state.
15. Stock and payment operations execute in parallel via RabbitMQ fan-out.
16. The architecture qualifies as event-driven for the assignment's difficulty scoring.

## Phase 2 Extraction Path

The rebuild is intentionally shaped so Phase 2 becomes a relocation exercise, not a redesign. The port interfaces (`OrderPort`, `TxStorePort`) and RabbitMQ participant transport are the two mechanisms that make this possible.

### What moves cleanly (no logic changes)

1. **`coordinator/`** moves into its own service. Protocol logic, state transitions, and recovery model are unchanged — they depend only on port interfaces and RabbitMQ, not on Flask or direct Redis access.
2. **Recovery worker** moves with the coordinator.
3. **Participant RabbitMQ consumers** (stock, payment) are unchanged — they already communicate through message queues, not in-process calls.
4. **`common/`** remains shared across all services (same as Phase 1).

### What requires new adapter implementations

1. **`OrderPort` implementation swaps from direct Redis to HTTP**:
   - Phase 1: `order/store.py` reads/writes order records in Redis directly.
   - Phase 2: the orchestrator calls new internal APIs on the order service:
     - `GET /internal/orders/{order_id}` — read order snapshot
     - `POST /internal/orders/{order_id}/mark_paid` — idempotent mark-paid
   - The coordinator code does not change; only the injected `OrderPort` implementation changes.

2. **`TxStorePort` implementation becomes local to the orchestrator**:
   - Phase 1: tx state lives in order's Redis via `order/store.py`.
   - Phase 2: the orchestrator gets its own Redis instance and owns all tx state (`tx:{tx_id}`, `tx_decision:{tx_id}`, `order_active_tx:{order_id}`, `order_commit_fence:{order_id}`) directly. The `TxStorePort` implementation is local Redis calls inside the orchestrator — no cross-service access.
   - The coordinator code does not change; only the injected `TxStorePort` implementation changes.

3. **Checkout route becomes a thin proxy in the order service**:
   - Phase 1: `POST /orders/checkout/{order_id}` lives in the order service, acquires the guard, calls the coordinator in-process, and clears the guard.
   - Phase 2: the order service's checkout route becomes a stateless proxy that forwards to the orchestrator via internal HTTP. The orchestrator owns the full lifecycle including guard acquisition/clearing, the already-paid fast path, and result-to-HTTP mapping. The order service just forwards the request and returns the orchestrator's response.
   - Why proxy instead of direct nginx routing: the orchestrator must not be publicly accessible from the internet. All external traffic enters through nginx → order service. The orchestrator is an internal-only service, reachable only by other services on the Docker network.

4. **`GET /internal/tx_decision/{tx_id}` moves to the orchestrator**:
   - Phase 1: served by the order service because tx state is in order's Redis.
   - Phase 2: served by the orchestrator because tx state is in the orchestrator's Redis. Participant `ORDER_SERVICE_URL` config changes to `ORCHESTRATOR_SERVICE_URL`.

### Extraction litmus test

If Phase 1 code cannot be moved by swapping `OrderPort` and `TxStorePort` implementations alone (plus the route-to-proxy change), the boundary is still too coupled and should be corrected before deeper implementation continues.
