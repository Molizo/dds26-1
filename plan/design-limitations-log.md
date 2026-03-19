# Design Limitations Log

## Purpose

This log records design limitations, why they occurred, what they impact, and the chosen mitigation.
It should be updated whenever implementation reveals a new constraint, incorrect assumption, or architectural mismatch.

Use this log to avoid repeating known mistakes and to justify walking back earlier decisions when needed.

## 2026-03-19 - A read-only orchestrator lock check is not enough to serialize order mutation against checkout

- Limitation encountered:
  - After moving the active checkout guard into orchestrator-owned Redis, a simple order-service `GET locked?` call before `addItem` leaves a race window where checkout can start after the read but before the order mutation commits.

- Why the current design caused it:
  - In Phase 1, `addItem` and checkout both observed the same Redis guard key locally from the order service.
  - In Phase 2, the authoritative guard moves to a different service and datastore, so a read-only remote check is no longer atomic with the order write.

- Impact:
  - Consistency risk: an item can be added after checkout begins but before the order is marked paid, creating a stale checkout snapshot or a free-item bug.

- Chosen mitigation or follow-up action:
  - Add an orchestrator-owned short-lived mutation guard in orchestrator Redis.
  - `addItem` acquires/releases that guard via internal orchestrator endpoints.
  - Checkout acquires the active-tx guard only when no mutation guard exists.
  - This keeps tx coordination state out of order Redis while preserving mutual exclusion between checkout and order mutation.

## 2026-03-11 - Live-stack transaction verification needs out-of-band observability

- Limitation encountered:
  - The public API exposes order domain state, but not coordinator tx state, active guards, or queue-level reply flow. That makes it impossible to prove recovery convergence and terminal cleanup from HTTP responses alone.

- Why the current design caused it:
  - Coordinator persistence deliberately lives behind internal Redis-backed storage and the external API avoids exposing orchestration internals.

- Impact:
  - Reliability risk: end-to-end tests can miss leaked non-terminal txs or stale guards if they only look at `200`/`400` route results.
  - Delivery risk: recovery testing becomes slower and more brittle when it has to infer internal state indirectly.

- Chosen mitigation or follow-up action:
  - Keep the external API unchanged.
  - Add test-only live-stack helpers that inspect RabbitMQ via the management API and inspect order-service coordinator state via `docker compose exec`.
  - Use those helpers for recovery and participant-worker integration tests instead of adding production debug endpoints.

## 2026-03-11 - Test coverage is strong on coordinator internals but incomplete on system concurrency and end-to-end recovery

- Limitation encountered:
  - The current suite covers coordinator protocol transitions and recovery logic well, but it does not explicitly test concurrent `addItem`, invalid numeric inputs, real participant replay/idempotency at the queue level, same-order checkout storms, or full recovery convergence in a live stack.

- Why the current design caused it:
  - Test design focused first on coordinator extraction and protocol safety.
  - This left external concurrency behavior and participant-level integration largely covered only indirectly.
  - The suite optimized for deterministic unit tests before adding targeted end-to-end concurrency checks.

- Impact:
  - Correctness risk: lost updates on order mutation can slip through.
  - Reliability risk: recovery may be correct in unit tests but insufficiently proven in the deployed stack.
  - Delivery risk: the project can appear well tested while still missing the highest-risk system behaviors.

- Chosen mitigation or follow-up action:
  - Track the missing cases in `plan/test-gap-plan.md`.
  - Prioritize tests for concurrent `addItem`, invalid numeric inputs, order mutation after checkout, and same-order checkout contention.
  - Add participant-worker integration tests and a real recovery convergence test before expanding load/stress coverage.

## 2026-03-02 - Earlier attempt solved the wrong phase first

- Limitation encountered:
  - A previous implementation pursued a RabbitMQ-centered architecture before satisfying the Phase 1 Flask + Redis deliverable.

- Why the current design caused it:
  - The design optimized for long-term event-driven architecture before meeting the current assignment boundary.
  - It treated future architecture as the immediate target instead of Phase 1's required scope.

- Impact:
  - Delivery risk: the implementation drifted from the rubric.
  - Complexity risk: extra infrastructure made reasoning and debugging harder.

- Chosen mitigation or follow-up action:
  - Keep Phase 1 limited to Flask + Redis coordination.
  - Defer standalone orchestration infrastructure to Phase 2.
  - Evaluate new design choices against the current deliverable before adopting them.

## 2026-03-02 - Missing 2PC made the phase incomplete

- Limitation encountered:
  - An earlier attempt implemented only SAGA and did not provide app-level 2PC.

- Why the current design caused it:
  - The design focused on one consistency mechanism and never reserved a clear abstraction for protocol selection.

- Impact:
  - Delivery risk: the Phase 1 requirement to implement both SAGA and 2PC was not met.
  - Learning risk: the codebase could not compare tradeoffs between the two modes.

- Chosen mitigation or follow-up action:
  - Make protocol selection a first-class design concern.
  - Require a startup flag with exactly two supported modes: `saga` and `2pc`.
  - Keep both protocols behind the same coordinator interface.

## 2026-03-02 - Coordinator logic was tangled with the order API layer

- Limitation encountered:
  - Checkout route code mixed HTTP behavior, distributed locking, transaction setup, protocol execution, and recovery entry logic.

- Why the current design caused it:
  - The coordinator lived inside route handlers instead of behind a separate application-service boundary.

- Impact:
  - Reliability risk: behavior became harder to reason about and easier to duplicate incorrectly.
  - Delivery risk: testing required full route-level execution for logic that should be unit-testable.
  - Phase 2 risk: extracting an orchestrator later would require rewriting core logic instead of relocating it.

- Chosen mitigation or follow-up action:
  - Create a coordinator layer with no Flask dependencies.
  - Restrict routes to validation, lock acquisition, coordinator invocation, and HTTP result mapping.
  - Keep protocol steps and recovery behind coordinator entrypoints.

## 2026-03-02 - Protocol modules depended on Flask and domain persistence details

- Limitation encountered:
  - Protocol code directly raised HTTP errors and mutated order persistence as part of protocol execution.

- Why the current design caused it:
  - The protocol layer was implemented as route-adjacent code instead of as a reusable state machine over abstract ports.

- Impact:
  - Coupling risk: protocol behavior could not be reused cleanly in recovery or a later external orchestrator.
  - Testing risk: protocol logic could not be validated independently from Flask request handling.

- Chosen mitigation or follow-up action:
  - Make protocol implementations return structured results instead of Flask responses.
  - Move order updates behind domain/store ports.
  - Keep transport-specific behavior in adapters only.

## 2026-03-02 - Recovery logic was duplicated across multiple code paths

- Limitation encountered:
  - Commit finalization and other recovery behavior ended up spread across route logic, protocol logic, and dedicated recovery code.

- Why the current design caused it:
  - There was no single coordinator-owned recovery API that all callers used.

- Impact:
  - Consistency risk: one path can diverge from another under failure.
  - Maintenance risk: fixes need to be applied in multiple places.

- Chosen mitigation or follow-up action:
  - Centralize recovery through `CoordinatorService.resume_transaction(...)`.
  - Keep one shared implementation of 2PC finalization behavior.
  - Let background workers and request-time healing call the same recovery entrypoint.

## 2026-03-02 - Request-time healing can conflict with lease-based locking

- Limitation encountered:
  - Long request-time retries can outlive lock leases and weaken mutual exclusion.

- Why the current design caused it:
  - Healing loops in synchronous HTTP handlers keep the request open while distributed state remains unsettled.

- Impact:
  - Consistency risk: concurrent checkout attempts can overlap if lock ownership becomes ambiguous.
  - Performance risk: slow requests tie up workers and inflate tail latency.

- Chosen mitigation or follow-up action:
  - Keep request-time healing bounded and narrow.
  - Allow inline healing only for the 2PC commit-fence case after a known commit decision.
  - Move all other repair to background recovery.

## 2026-03-02 - Treating recovery-needed states as terminal is unsafe

- Limitation encountered:
  - States that still require recovery can be mistaken for safe terminal states.

- Why the current design caused it:
  - The design used one concept of "terminal" for both flow completion and permission to start a new checkout.

- Impact:
  - Consistency risk: a second checkout can start before the first transaction has truly stabilized.

- Chosen mitigation or follow-up action:
  - Distinguish between:
    - flow terminal states
    - safe-to-reenter states
  - Keep `order_active_tx` in place until the transaction reaches a state that is explicitly safe for new checkout attempts.

## 2026-03-02 - Domain state and orchestration state were too tightly mixed

- Limitation encountered:
  - Order data and coordinator metadata were stored through a tightly coupled persistence design.

- Why the current design caused it:
  - The same storage layer evolved to hold both business records and transaction-control records without a clear separation of concern.

- Impact:
  - Maintainability risk: changes to transaction control can accidentally affect order-domain code.
  - Phase 2 risk: moving the coordinator later becomes harder because storage responsibilities are unclear.

- Chosen mitigation or follow-up action:
  - Separate domain persistence from orchestration persistence at the code boundary.
  - Use explicit keyspaces and repository functions for each concern.
  - Keep the order service as the physical owner of both stores in Phase 1, but not as one conceptual data model.

## 2026-03-02 - Timestamp ordering is a poor primary fit for Phase 1 2PC

- Limitation encountered:
  - Using a Redis atomic counter for timestamp ordering as the main checkout concurrency policy was considered for 2PC coordination.

- Why the current design causes it:
  - Timestamp ordering is attractive because it avoids explicit lock acquisition logic, but it does not naturally model the durable `prepare -> decision -> commit/abort` lifecycle required by 2PC participants.

- Impact:
  - Consistency risk: conflict resolution becomes retry-heavy and harder to reason about during crashes.
  - Reliability risk: prepared-but-undecided participant state is less explicit than reservation-based holds.
  - Delivery risk: the implementation becomes harder to explain and verify during the course evaluation.

- Chosen mitigation or follow-up action:
  - Use reservation-based strict 2PL semantics at participants for Phase 1 2PC.
  - Let `prepare` create durable holds on stock and funds.
  - Keep Redis counters only for monotonic sequencing, event ordering, or diagnostics, not as the primary concurrency policy.

## 2026-03-02 - Summary flags alone are not enough for crash recovery

- Limitation encountered:
  - A design that stores only current status flags can lose important context about what happened immediately before a crash.

- Why the current design causes it:
  - Summary fields compress state for fast reads, but they remove the step-by-step history needed to distinguish similar-looking failure windows.

- Impact:
  - Reliability risk: recovery may need to guess which step was last durably completed.
  - Delivery risk: debugging rollback and compensation bugs becomes slower and less defensible.

- Chosen mitigation or follow-up action:
  - Keep the compact canonical transaction record as the fast-path summary.
  - Add an append-only per-transaction step log for each meaningful transition.
  - Use the step log during recovery when the summary record is ambiguous.

## 2026-03-02 - Lease locks, active-tx guards, and prepared holds must remain separate

**SUPERSEDED on 2026-03-05**: The lease lock and active-tx guard have been merged into a single mechanism (durable active-tx pointer with TTL). See entry "2026-03-05 - Three separate locking mechanisms create interaction surface area" below.

- Limitation encountered:
  - It is easy to conflate request mutual exclusion, transaction ownership, and prepared resource protection into one lock concept.

- Why the current design causes it:
  - All three mechanisms seem related to "locking," but they protect different things and live at different durability levels.

- Impact:
  - Consistency risk: clearing a request lock or losing a lease can accidentally be treated as permission to begin a new checkout.
  - Reliability risk: rollback and recovery logic can release the wrong protection at the wrong time.

- Original mitigation (now superseded):
  - Keep the per-order lease lock only for short request-path mutual exclusion.
  - Keep a separate durable active transaction guard until the tx is truly safe to re-enter.
  - Keep prepared holds in participant stores as durable resource reservations until `commit` or `abort`.

- Revised mitigation (2026-03-05):
  - Merge lease lock and active-tx guard into one mechanism: `SET order_active_tx:{order_id} {tx_id} NX EX {ttl}`.
  - Keep prepared holds as a separate mechanism in participant stores.
  - Two mechanisms instead of three reduces interaction surface area.

## 2026-03-02 - Reused-order benchmarks can inflate throughput with cheap `200` responses

- Limitation encountered:
  - Benchmark scenarios that repeatedly call checkout on the same order pool can report very high throughput because already-paid orders return cheap `200` no-op responses.

- Why the current design causes it:
  - The API contract intentionally treats already-paid checkout as a successful idempotent outcome.
  - Reused-order load mixes real distributed work with very cheap no-op requests.

- Impact:
  - Performance risk: headline request throughput can look much better than real committed checkout throughput.
  - Delivery risk: performance claims can be misleading if the metric is not named precisely.

- Chosen mitigation or follow-up action:
  - Keep already-paid checkout as `200` because the contract is correct.
  - Explicitly optimize the already-paid fast path for raw request throughput.
  - Track and report headline request throughput separately from committed checkout throughput.

## 2026-03-02 - Per-item protocol chatter can dominate latency on large orders

- Limitation encountered:
  - Large orders can generate too many internal protocol calls and log writes if the coordinator handles each raw line item separately.

- Why the current design causes it:
  - A naive implementation may process raw order lines rather than the aggregated item snapshot and may log every microscopic step.

- Impact:
  - Performance risk: tail latency grows quickly and can break the request budget under stress.

- Chosen mitigation or follow-up action:
  - Aggregate order lines by `item_id` before protocol execution.
  - Prefer batched internal stock work per transaction.
  - Keep tx logs focused on meaningful transition boundaries instead of excessively granular noise.

## 2026-03-02 - Recovery workers need single-active coordination when order is scaled

- Limitation encountered:
  - Multiple `order` replicas can each start a recovery loop and race to resume the same transactions.

- Why the current design causes it:
  - Embedding recovery in the `order` service is simple, but it creates coordination risk once the service is horizontally scaled.

- Impact:
  - Consistency risk: duplicate resume/commit/abort attempts can race and produce hard-to-debug recovery behavior.
  - Performance risk: unnecessary duplicated recovery work consumes capacity on the busiest service.

- Chosen mitigation or follow-up action:
  - Require a distributed leader lock for the recovery worker.
  - Allow only one active recovery scanner at a time across `order` replicas.

## 2026-03-02 - Summary and tx-log updates must not diverge

**SUPERSEDED on 2026-03-05**: The per-transaction Redis step log has been replaced with structured application-level logging. The divergence problem no longer exists because there is only one durable source of truth (the tx summary record + decision marker). See entry "2026-03-05 - Per-transaction Redis step log adds unnecessary write overhead" below.

- Limitation encountered:
  - A transaction summary record and an append-only tx log can contradict each other if updated separately for the same transition.

- Why the current design causes it:
  - Adding a tx step log improves observability, but it also creates a second source of truth unless writes are coordinated.

- Impact:
  - Reliability risk: recovery can still face ambiguous crash windows despite having more recorded data.

- Original mitigation (now superseded):
  - Update the summary and the corresponding log entry in one atomic write unit for the same transition.
  - Treat divergence between the two as a correctness bug, not a normal recovery state.

- Revised mitigation (2026-03-05):
  - Eliminate the second durable source of truth entirely. Use application logs for intermediate steps; use only the tx summary + decision marker for recovery.

## 2026-03-02 - Prepared holds need cleanup and participant-side reconciliation

- Limitation encountered:
  - Durable prepared holds can become stranded after crashes if no participant-side cleanup or reconciliation exists.

- Why the current design causes it:
  - Reservation-based 2PC protects correctness, but abandoned holds reduce availability until they are resolved.

- Impact:
  - Availability risk: stock and funds can remain unnecessarily unavailable.
  - Performance risk: backlog grows when later requests conflict with stale holds.

- Chosen mitigation or follow-up action:
  - Add participant startup reconciliation for non-terminal local transactions.
  - Add bounded stale-hold cleanup that respects known coordinator decisions.

## 2026-03-02 - A message bus would add complexity before it improves the Phase 1 hot path

**REVERSED on 2026-03-05**: RabbitMQ is now used for internal coordinator↔participant communication. See entry "2026-03-05 - RabbitMQ adopted for internal transport with parallel fan-out" below.

- Limitation encountered:
  - A message bus was considered for the checkout path to improve replayability, recovery, and load distribution.

- Why the current design causes it:
  - Durable messaging is attractive because it offers redelivery and decoupling, especially for SAGA-style workflows.
  - However, the checkout API is still externally synchronous and 2PC still needs a synchronous logical decision point.

- Impact:
  - Performance risk: extra publish/consume/reply hops increase tail latency on the hot path.
  - Delivery risk: the team can drift into a larger infrastructure project before stabilizing Phase 1 correctness.

- Original mitigation (now reversed):
  - Keep the Phase 1 checkout path on direct internal HTTP.

- Revised mitigation (2026-03-05):
  - Use RabbitMQ for parallel coordinator↔participant communication.
  - The coordinator blocks on reply queues (no Redis polling loop like attempt 1).
  - The latency cost of broker hops is offset by parallel fan-out to stock and payment.
  - The assignment rewards event-driven architecture and the lecturer advises for it.

## 2026-03-02 - Full event sourcing would overshoot the current scope

- Limitation encountered:
  - Full event sourcing across services was considered after the tx-log discussion.

- Why the current design causes it:
  - Append-only logs are useful for recovery and auditability, which makes a fully event-sourced model appear attractive.

- Impact:
  - Delivery risk: rebuilding all reads and writes around replay semantics would significantly expand scope.
  - Maintainability risk: operational complexity rises before the core Phase 1 behaviors are proven.

- Chosen mitigation or follow-up action:
  - Keep a hybrid model:
    - compact summary record for fast reads
    - append-only per-transaction step log for recovery and debugging
  - Avoid turning business-state reads into replay-based reconstruction in Phase 1.

## 2026-03-02 - Throughput goals can become misleading without named metrics

- Limitation encountered:
  - The same benchmark can mix cheap already-paid no-op responses with real distributed checkouts and still report one throughput number.

- Why the current design causes it:
  - The API correctly treats already-paid checkout as `200`, and some load scenarios heavily reuse order IDs.

- Impact:
  - Performance risk: optimization decisions can chase the wrong bottleneck.
  - Delivery risk: results can sound stronger than the actual committed-work capacity of the system.

- Chosen mitigation or follow-up action:
  - Track and report:
    - headline request throughput
    - committed checkout throughput
  - Use both numbers intentionally instead of merging them into one misleading metric.

## 2026-03-02 - Backward compatibility would add cost without helping the MVP

- Limitation encountered:
  - Preserving compatibility with previous Redis schemas and transaction formats was considered while the design is still evolving.

- Why the current design causes it:
  - When iterating on transaction storage, key layouts and record shapes naturally change as failure cases become clearer.

- Impact:
  - Delivery risk: migration code and compatibility branches would consume time without improving the proof-of-concept deliverable.
  - Reliability risk: extra compatibility logic in the persistence layer makes the most failure-sensitive code harder to reason about.

- Chosen mitigation or follow-up action:
  - Explicitly allow schema-breaking data model changes during the rebuild.
  - Treat the system as a clean-slate MVP deployment with empty state at startup.
  - Do not require backward-compatible migrations across development iterations.

## 2026-03-02 - Mixed protocol deployments would expand the failure matrix unnecessarily

- Limitation encountered:
  - Running some services in SAGA mode and others in 2PC mode at the same time was considered.

- Why the current design causes it:
  - Because both protocols exist in one codebase, it is easy to imagine per-service configuration drift or intentional mixed combinations.

- Impact:
  - Consistency risk: transaction semantics become harder to define and verify.
  - Delivery risk: the test and failure matrix grows significantly without helping the Phase 1 requirement.

- Chosen mitigation or follow-up action:
  - Make protocol mode deployment-wide.
  - Require that all services in one running stack use the same transaction mode.
  - Treat mixed SAGA/2PC service combinations as out of scope.

## 2026-03-02 - Architecture boundaries were defined too late in attempt 2

- Limitation encountered:
  - The implementation first accumulated substantial protocol logic in large service files and only later split it into smaller modules.

- Why the current design causes it:
  - File-level refactoring happened after the main transaction behavior already existed, so the resulting module split improved organization but did not fully change the dependency shape.

- Impact:
  - Maintainability risk: route code, protocol logic, and recovery concerns remain coupled even after refactoring.
  - Delivery risk: late boundary fixes consume time after correctness bugs already exist.

- Chosen mitigation or follow-up action:
  - Define coordinator, protocol, store, and adapter boundaries before implementing most transaction logic.
  - Treat boundary design as an up-front architecture task, not cleanup after the behavior already exists.

## 2026-03-02 - Recovery was added too late instead of shaping the initial model

- Limitation encountered:
  - Attempt 2 implemented most protocol behavior before a dedicated recovery engine fully existed.

- Why the current design causes it:
  - Recovery was treated as a later hardening phase rather than a first-class part of transaction design.

- Impact:
  - Reliability risk: the state machine is more likely to contain ambiguous or under-specified crash windows.
  - Delivery risk: late recovery additions often expose foundational modeling flaws and force patches.

- Chosen mitigation or follow-up action:
  - Design transaction states and transitions from the beginning around resumability after crashes.
  - Require recovery behavior to be part of the core protocol design, not an optional later add-on.

## 2026-03-02 - Status groups must distinguish completion from safe re-entry

- Limitation encountered:
  - Attempt 2 initially reused one notion of "terminal" for multiple meanings.

- Why the current design causes it:
  - It is easy to classify statuses only by whether the current flow stopped, while forgetting that checkout re-entry safety is a separate concern.

- Impact:
  - Consistency risk: new work can begin before old work is actually safe to replace.

- Chosen mitigation or follow-up action:
  - Define status groups explicitly for:
    - flow progression
    - final business outcome
    - safe-to-reenter eligibility
  - Never assume those categories are interchangeable.

## 2026-03-02 - Optimization caches must not become correctness authorities

- Limitation encountered:
  - Time-based "recent checkout" behavior can drift into influencing duplicate-checkout semantics.

- Why the current design causes it:
  - Caches are easy to add for throttling or user experience, but once they influence client-visible outcomes they can start acting like a source of truth.

- Impact:
  - Consistency risk: duplicate behavior can depend on cache timing instead of durable business state.
  - Reliability risk: cache expiration can change semantics without any real state change.

- Chosen mitigation or follow-up action:
  - Keep correctness decisions based only on durable business and transaction state.
  - Treat recency caches as optional performance hints only.

## 2026-03-02 - Ambiguous participant outcomes need explicit modeling

- Limitation encountered:
  - Timeout cases can be neither clearly prepared nor clearly unprepared, yet simple state models encourage collapsing them into one boolean-like outcome.

- Why the current design causes it:
  - Simple prepared flags are convenient, but they do not naturally encode uncertainty after transport failures.

- Impact:
  - Reliability risk: recovery must infer too much from compressed state.
  - Consistency risk: abort or resume behavior can be based on an assumption rather than an explicitly modeled ambiguity.

- Chosen mitigation or follow-up action:
  - Treat uncertain participant outcomes as a distinct recovery concern.
  - Do not equate "unknown but maybe prepared" with "definitely prepared" unless the design is intentionally conservative and documents that choice.

## 2026-03-02 - Too many separate durable writes create unnecessary crash windows

- Limitation encountered:
  - Coordinator transitions in attempt 2 were sometimes split across multiple durable writes that represented one logical decision.

- Why the current design causes it:
  - It is easy to write status, decision, and fence markers as separate steps without first designing the minimum atomic transition unit.

- Impact:
  - Reliability risk: more small in-between states must be recovered correctly.
  - Maintenance risk: recovery logic becomes more complex because it must understand more partial states.

- Chosen mitigation or follow-up action:
  - Minimize the number of durable writes per logical coordinator transition.
  - Introduce atomic transition helpers early and treat them as foundational infrastructure.

## 2026-03-02 - Single-replica assumptions hide later scaling risks

- Limitation encountered:
  - Request-path correctness was considered before cross-replica recovery coordination.

- Why the current design causes it:
  - It is natural to validate the single-instance case first and postpone replica coordination.

- Impact:
  - Consistency risk: scaling can introduce duplicate recovery work and new race conditions that were invisible in single-replica testing.
  - Delivery risk: late scaling fixes often arrive after the core recovery code is already written.

- Chosen mitigation or follow-up action:
  - Include replica-safe coordination in the initial recovery design.
  - Treat "works with one replica" as insufficient for architecture sign-off when scaling is in scope.

## 2026-03-02 - Black-box tests alone miss state-machine bugs

- Limitation encountered:
  - Strong end-to-end regression tests were added before focused recovery and state-semantics tests existed.

- Why the current design causes it:
  - Integration tests are more intuitive to write first because they resemble the public behavior, but they often miss internal state-classification bugs.

- Impact:
  - Reliability risk: state-machine mistakes can survive despite broad black-box coverage.

- Chosen mitigation or follow-up action:
  - Add unit tests for:
    - transaction state transitions
    - recovery semantics
    - re-entry conditions
  - Treat these as first-class tests, not optional supplements.

## 2026-03-02 - Tolerant tests can hide unresolved semantic decisions

- Limitation encountered:
  - Some exploratory tests accepted multiple outcomes in concurrent scenarios because the intended semantics were not fully frozen yet.

- Why the current design causes it:
  - Flexible assertions reduce flakiness during exploration, but they can also allow unresolved design questions to persist unnoticed.

- Impact:
  - Delivery risk: the implementation can "pass tests" while still lacking a crisp behavioral contract.

- Chosen mitigation or follow-up action:
  - Freeze key client-visible semantics early.
  - Use strict assertions except where multiple outcomes are intentionally part of the design.
  - Document any intentionally tolerated ambiguity explicitly.

## 2026-03-02 - Aggregation alone does not solve protocol chatter

- Limitation encountered:
  - Aggregating duplicate order lines reduces one class of waste, but the protocol can still remain too chatty if it performs per-item remote work afterward.

- Why the current design causes it:
  - It is easy to stop at item aggregation and treat the biggest correctness issue as solved, while the latency cost of per-item internal calls remains.

- Impact:
  - Performance risk: large orders still cause high internal round-trip counts and poor tail latency.

- Chosen mitigation or follow-up action:
  - Keep aggregation as the minimum baseline.
  - Design internal participant APIs so batched transaction work is possible.
  - Review performance at the level of remote operations per logical checkout, not just order-line correctness.

## 2026-03-02 - Local fixes should trigger model updates, not just patches

- Limitation encountered:
  - Attempt 2's evolution shows a pattern of discovering issues and adding local fixes after the fact.

- Why the current design causes it:
  - Under time pressure, the immediate bugfix is often easier than revisiting the underlying transaction model.

- Impact:
  - Maintainability risk: the system can accumulate patches that work individually while the conceptual model remains weak.
  - Delivery risk: similar bugs reappear in adjacent edge cases.

- Chosen mitigation or follow-up action:
  - When a bug exposes a model weakness, update:
    - the architecture plan
    - the invariants
    - the tests
    before or alongside the local fix
  - Prefer strengthening the model over accumulating one-off repairs.

## 2026-03-05 - RabbitMQ adopted for internal transport with parallel fan-out

- Limitation encountered:
  - The previous plan used serial direct HTTP calls for coordinator↔participant communication. This was the dominant latency bottleneck (attempt 2 made serial stock→payment calls). The plan also rejected message brokers despite the assignment explicitly rewarding event-driven architectures.

- Why the current design caused it:
  - The plan was conservative after attempt 1's scope drift into RabbitMQ infrastructure. The overcorrection was to avoid any message broker entirely.

- Impact:
  - **Performance risk (high)**: serial HTTP calls double the latency of every checkout.
  - **Grading risk (high)**: the assignment explicitly says "Event-driven asynchronous architectures...will get extra points" and the lecturer advises for a message broker. Ignoring both is a bad tradeoff for a course project.

- Chosen mitigation or follow-up action:
  - Adopt RabbitMQ as internal transport. Coordinator publishes commands in parallel to stock and payment queues.
  - Coordinator blocks on a reply queue (bounded timeout) — no Redis polling loop like attempt 1.
  - Preserve attempt 1's good patterns: durable persistent messages, DLQ, thread-local publisher connections, Lua-based idempotent consumers.
  - Avoid attempt 1's mistakes: no separate worker process, no polling loop, no complex retry-queue topology.
  - Updated in `plan/phase1-rebuild-plan.md`: decision record reversed, architecture and protocol sections updated.

## 2026-03-05 - Parallel execution requires handling partial success in both SAGA and 2PC

- Limitation encountered:
  - When stock and payment execute in parallel, either one can succeed while the other fails. The previous serial design (stock first, then payment) only needed to handle "stock succeeded, payment failed" — not the reverse.

- Why the current design causes it:
  - Parallel fan-out means both success and failure can arrive from either participant independently.

- Impact:
  - **Consistency risk (medium)**: partial success scenarios must be compensated correctly in both directions (stock OK + payment fail, payment OK + stock fail).

- Chosen mitigation or follow-up action:
  - SAGA handles both partial-success directions: if stock succeeds but payment fails → release stock; if payment succeeds but stock fails → refund payment.
  - 2PC: if either prepare fails or times out → abort both. The commit decision is only written when both prepare replies are positive.
  - Recovery handles all four partial states (both succeed, both fail, stock-only, payment-only).
  - Updated in `plan/phase1-rebuild-plan.md`: SAGA and 2PC protocol sections updated.

## 2026-03-05 - Attempt 2's FAILED_NEEDS_RECOVERY bug must not recur

- Limitation encountered:
  - In attempt 2, `FAILED_NEEDS_RECOVERY` was included in `TERMINAL_TX_STATUSES`, which caused `is_terminal_status()` to return true. This cleared the active-tx guard, allowing a second concurrent checkout to start while the first was still recovering. This caused duplicate charges and stock drift.

- Why the current design caused it:
  - The status taxonomy conflated "flow stopped" with "safe to re-enter." `FAILED_NEEDS_RECOVERY` means the flow stopped, but the transaction is NOT safe to re-enter.

- Impact:
  - **Consistency risk (critical)**: duplicate charges, duplicate stock subtractions, and inconsistent order state.

- Chosen mitigation or follow-up action:
  - `FAILED_NEEDS_RECOVERY` is explicitly NOT in the terminal status set.
  - The active-tx guard is only cleared when the transaction reaches `COMPLETED` or `ABORTED`.
  - A dedicated test must assert this invariant.
  - Updated in `plan/phase1-rebuild-plan.md`: step 2 acceptance criteria and testing priorities.

## 2026-03-05 - Participant read-modify-write operations are non-atomic

- Limitation encountered:
  - All three services use a `GET` then `SET` pattern for stock subtract/add and credit pay/add_funds. Two concurrent calls can both read the same value, both compute independently, and both write — silently losing one operation.

- Why the current design caused it:
  - The template code uses simple `db.get()` / `db.set()` without any atomicity mechanism. This was inherited from the starter project and never fixed.

- Impact:
  - **Consistency risk (critical)**: under benchmark load, stock and credit balances will drift. This makes any higher-level consistency protocol (SAGA or 2PC) meaningless — the foundation is broken.

- Chosen mitigation or follow-up action:
  - Replace all participant read-modify-write operations with Lua scripts that perform read, check, modify, and write atomically on the Redis server.
  - This applies to both public endpoints and internal transaction endpoints.
  - Updated in `plan/phase1-rebuild-plan.md`: added as non-negotiable #15 and explicit decision record.

## 2026-03-05 - Internal calls routed through nginx gateway add unnecessary latency

- Limitation encountered:
  - The order service calls stock and payment through `GATEWAY_URL` (the nginx gateway), adding an extra network hop and serialization cycle to every internal call.

- Why the current design caused it:
  - The template only provided `GATEWAY_URL` as the service discovery mechanism. Internal direct URLs were never configured.

- Impact:
  - **Performance risk (high)**: every checkout makes multiple internal HTTP calls. Each one goes client → nginx → service instead of client → service. Under load, nginx becomes a bottleneck for internal traffic.

- Chosen mitigation or follow-up action:
  - Add `STOCK_SERVICE_URL` and `PAYMENT_SERVICE_URL` environment variables to docker-compose.
  - Internal calls use these direct URLs. Gateway is only for external clients.
  - Updated in `plan/phase1-rebuild-plan.md`: added as non-negotiable #16 and explicit decision record.

## 2026-03-05 - Gunicorn worker count of 2 is a severe throughput bottleneck

- Limitation encountered:
  - docker-compose runs all services with `-w 2`. With synchronous blocking HTTP calls, 2 workers means at most 2 concurrent checkouts.

- Why the current design caused it:
  - Default worker count was never tuned for the synchronous call pattern.

- Impact:
  - **Performance risk (critical)**: the plan targets ~10k req/s but 2 sync workers can barely handle 50-100 real checkouts/s. This is the dominant bottleneck before any protocol overhead.

- Chosen mitigation or follow-up action:
  - Increase to at least 8 workers for order service, 4 for stock and payment.
  - Tune further based on benchmark results.
  - Async workers (gevent/eventlet) remain a later optimization option.
  - Updated in `plan/phase1-rebuild-plan.md`: added as non-negotiable #17 and explicit decision record.

## 2026-03-05 - Per-transaction Redis step log adds unnecessary write overhead

- Limitation encountered:
  - The original plan required an append-only per-transaction step log in Redis with ~20 event types and per-entry metadata. Each checkout would generate 6-10 durable writes just for observability.

- Why the current design caused it:
  - The plan wanted to avoid the "summary flags alone are insufficient for recovery" problem from attempt 2. A full step log was the proposed solution.

- Impact:
  - **Performance risk (medium)**: the step log writes add material Redis pressure per checkout under load.
  - **Complexity risk**: the atomic-write requirement (summary + log in one `MULTI`/`EXEC`) adds implementation surface area.

- Chosen mitigation or follow-up action:
  - Replace the Redis step log with structured application-level logging (stdout/gunicorn).
  - Recovery depends only on the tx summary record and the durable decision marker.
  - Debugging intermediate steps uses application logs, which are cheap and don't add Redis pressure.
  - Updated in `plan/phase1-rebuild-plan.md`: decision record revised.

## 2026-03-05 - Three separate locking mechanisms create interaction surface area

- Limitation encountered:
  - The original plan defined three locks: request lease lock, active transaction guard, and prepared holds. Each has different lifetime, durability, and clearing semantics. The interactions between them (lease expires while guard is set, guard cleared while lease is held by another request) are a source of subtle correctness bugs.

- Why the current design caused it:
  - The lease lock and active-tx guard were designed to solve different problems (request mutual exclusion vs. transaction ownership), but in practice the active-tx guard already provides mutual exclusion.

- Impact:
  - **Consistency risk (medium)**: more locking primitives means more edge cases to reason about and test correctly.
  - **Complexity risk**: three mechanisms interacting under crash, timeout, and concurrency scenarios is harder to verify than two.

- Chosen mitigation or follow-up action:
  - Merge the lease lock and active-tx guard into one mechanism: a durable active-tx pointer with TTL.
  - `SET order_active_tx:{order_id} {tx_id} NX EX {ttl}` provides both mutual exclusion and durable ownership.
  - TTL handles crash cleanup. Recovery worker handles stuck transactions.
  - This reduces the locking model from three mechanisms to two (active-tx guard + prepared holds).
  - Updated in `plan/phase1-rebuild-plan.md`: locking section revised.

## 2026-03-05 - SAGA recovery direction after successful charge was unspecified

- Limitation encountered:
  - The plan did not specify whether SAGA recovery should complete forward or compensate backward when the transaction crashed after successfully charging payment but before marking the order as paid.

- Why the current design caused it:
  - The plan focused on the general compensation model without distinguishing recovery direction based on how far the saga progressed.

- Impact:
  - **Consistency risk (medium)**: compensating backward (refunding) after a successful charge risks the refund itself failing, leaving the system in a worse state. Completing forward (marking paid) is safer because the charge already happened.

- Chosen mitigation or follow-up action:
  - SAGA recovery completes forward after a successful charge.
  - SAGA recovery compensates backward only when charge has not yet happened.
  - Updated in `plan/phase1-rebuild-plan.md`: SAGA protocol section and recovery model updated.

## 2026-03-05 - Stale-hold cleanup was underspecified without a coordinator query channel

- Limitation encountered:
  - The plan said participants should do "bounded stale-hold cleanup that respects known coordinator decisions" but participants have no direct access to coordinator state (which lives in a different Redis instance).

- Why the current design caused it:
  - The plan assumed participants could somehow know coordinator decisions without specifying the communication channel.

- Impact:
  - **Consistency risk (medium)**: a participant that unilaterally releases a prepared hold could violate a commit decision.
  - **Availability risk**: without any cleanup mechanism, stale holds block resources indefinitely.

- Chosen mitigation or follow-up action:
  - Stale-hold cleanup is coordinator-driven: the recovery worker issues explicit abort calls.
  - For participant-side startup reconciliation, the order service exposes `GET /internal/tx_decision/{tx_id}`.
  - Participants never unilaterally abort holds when the decision is unknown.
  - Updated in `plan/phase1-rebuild-plan.md`: participant and locking sections updated.

## 2026-03-05 - 10-step implementation program was too granular for the timeline

- Limitation encountered:
  - The plan defined 10 implementation steps, each with detailed validation matrices and acceptance criteria. With 8 days until the Phase 1 deadline, this is roughly 1 step per day with no slack for debugging or rework.

- Why the current design caused it:
  - The plan was written defensively after two failed attempts, optimizing for process rigor over delivery speed.

- Impact:
  - **Delivery risk (high)**: the overhead of per-step validation ceremonies can consume more time than the implementation itself. Steps with no meaningful independent deliverable (e.g., "freeze the contract" as step 1, "build data model" as step 2) create artificial boundaries.

- Chosen mitigation or follow-up action:
  - Consolidated to 5 steps: (1) foundation + atomic participants, (2) coordinator + both protocols, (3) recovery, (4) validation + benchmarking, (5) fault tolerance.
  - Same correctness gates, fewer artificial boundaries.
  - Updated in `plan/phase1-rebuild-plan.md`: implementation program revised.

## 2026-03-05 - Replica-safe recovery leader lock is premature for Phase 1

- Limitation encountered:
  - The plan required a distributed leader lock for the recovery worker to handle multiple `order` replicas. But docker-compose runs a single order-service instance and the benchmark runs locally.

- Why the current design caused it:
  - The plan was designed to handle horizontal scaling from the start, which is good long-term but premature when basic SAGA/2PC correctness doesn't exist yet.

- Impact:
  - **Delivery risk (medium)**: implementing and testing distributed leader election consumes time that should go toward getting basic protocol correctness working.

- Chosen mitigation or follow-up action:
  - Implement single-instance recovery first.
  - Document the scaling limitation explicitly.
  - Add the leader lock only if order service is actually scaled.
  - Updated in `plan/phase1-rebuild-plan.md`: recovery model updated.

## 2026-03-06 - Multiple order workers can duplicate recovery for the same tx

- Limitation encountered:
  - With gunicorn worker processes, each worker can run a recovery scanner and pick the same stale transaction at the same time.

- Why the current design caused it:
  - Recovery runs inside the order service process model.
  - The active-tx guard prevents different txs on one order but does not by itself serialize recovery attempts for the same `tx_id`.

- Impact:
  - **Reliability risk (medium)**: duplicate `resume_transaction` invocations increase race surface and message churn.
  - **Performance risk (medium)**: repeated publish/wait cycles add avoidable load during failure recovery.

- Chosen mitigation or follow-up action:
  - Add a best-effort per-tx recovery lock key (`tx_recovery_lock:{tx_id}`) with TTL in Redis.
  - Recovery scanner acquires this lock before resuming a stale tx and releases it afterward.
  - Keep all repair logic centralized in `CoordinatorService.resume_transaction(...)`; the lock only serializes who is allowed to invoke it.

## 2026-03-06 - Kubernetes manifests drifted from docker-compose runtime topology

- Limitation encountered:
  - The `k8s/` manifests used legacy service naming and infrastructure wiring (`user-service`, shared `redis-master`) that no longer matched the active docker-compose deployment (`payment-service`, per-service Redis, RabbitMQ-backed coordination).

- Why the current design caused it:
  - Compose and Kubernetes configs evolved independently, so naming and environment contracts diverged over time.

- Impact:
  - **Reliability risk (high)**: services can start with wrong dependencies or missing env wiring (RabbitMQ URLs, protocol flags), causing startup/runtime failures.
  - **Delivery risk (medium)**: local validation in compose does not transfer to Kubernetes, increasing deployment troubleshooting time.

- Chosen mitigation or follow-up action:
  - Align Kubernetes manifests to compose conventions:
  - Rename `user-service` to `payment-service`.
  - Use compose-equivalent service env vars and gunicorn command lines.
  - Add dedicated Redis deployments/services (`order-db`, `stock-db`, `payment-db`).
  - Add RabbitMQ deployment/service with compose-equivalent defaults.
  - Keep ingress route prefixes (`/orders`, `/stock`, `/payment`) unchanged.

## 2026-03-06 - Shared Redis requires isolation beyond host-level separation

**SUPERSEDED on 2026-03-06** by "Dedicated Redis per service retained for environment parity" below.

- Limitation encountered:
  - When all services use one Redis instance, overlapping key names (for example batch-init keys like `"0"`, `"1"`) can collide.

- Why the current design caused it:
  - The service data model does not namespace top-level keys across services by default.

- Impact:
  - **Consistency risk (high)**: order, stock, and payment data can overwrite each other in the same logical Redis DB.

- Chosen mitigation or follow-up action:
  - Keep one shared Redis host.
  - Isolate services with separate logical DB indexes (`order=0`, `stock=1`, `payment=2`).
  - Point all Kubernetes services to the same Redis host (`redis-master`).

## 2026-03-06 - Dedicated Redis per service retained for environment parity

- Limitation encountered:
  - A shared-Redis topology in Kubernetes diverged from the active docker-compose topology and introduced an extra DB-index coordination concern.

- Why the current design caused it:
  - Shared Redis reduces infrastructure count, but this codebase currently assumes service-level storage isolation and uses overlapping key patterns.

- Impact:
  - **Reliability risk (medium)**: accidental DB-index misconfiguration can silently mix data across services.
  - **Delivery risk (medium)**: deployment behavior differs between compose and Kubernetes, making debugging less predictable.

- Chosen mitigation or follow-up action:
  - Revert Kubernetes to dedicated Redis instances (`order-db`, `stock-db`, `payment-db`) with `REDIS_DB=0` per service.
  - Keep shared-Redis as an optional future optimization only if key-prefixing or stricter config guards are added.

## 2026-03-06 - Shared Redis topology should not coexist with per-service Redis deployments

- Limitation encountered:
  - The cluster had both Helm Redis (`redis-master`/`redis-replicas`) and separate `order-db`/`stock-db`/`payment-db` deployments at the same time.

- Why the current design caused it:
  - Topology changes were applied incrementally without fully removing obsolete resources.

- Impact:
  - **Delivery risk (medium)**: unclear operational ownership and wasted cluster resources.
  - **Reliability risk (medium)**: confusion about which Redis path is authoritative for application traffic.

- Chosen mitigation or follow-up action:
  - Standardize on shared Helm Redis for application state.
  - Point all services to `redis-master` and isolate keys via DB indexes (`order=0`, `stock=1`, `payment=2`).
  - Remove obsolete service-local Redis deployments/services from manifests and live cluster.

## 2026-03-06 - CPU autoscaling needs both metrics-server and CPU requests

- Limitation encountered:
  - HPA cannot scale by CPU when the Metrics API is unavailable or when target deployments do not define CPU requests.

- Why the current design caused it:
  - The base cluster setup did not include `metrics-server`.
  - Service deployments had no resource requests, so utilization percentages were undefined for HPA decisions.

- Impact:
  - **Reliability risk (medium)**: autoscaling objects exist but remain non-functional.
  - **Performance risk (medium)**: services stay fixed at one replica under load, causing avoidable saturation.

- Chosen mitigation or follow-up action:
  - Add `metrics-server` installation to `deploy-charts-cluster.sh`.
  - Add CPU/memory requests and limits to order, stock, and payment deployments.
  - Add `autoscaling/v2` HPA resources with CPU utilization targets for all three service deployments.

## 2026-03-06 - Redis write nodes should not be horizontally autoscaled by pod count

- Limitation encountered:
  - Naively applying HPA to standalone Redis write deployments can create multiple independent primaries behind one service and split state.

- Why the current design caused it:
  - The service-level Redis instances are single-node primaries without replication/sentinel orchestration.

- Impact:
  - **Consistency risk (high)**: requests can hit different Redis pods with divergent data.

- Chosen mitigation or follow-up action:
  - Keep service-local Redis primaries single-instance.
  - Introduce Redis scaling on the Bitnami replication chart by enabling replica autoscaling (`replica.autoscaling.*`) in `helm-config/redis-helm-values.yaml`.
  - Keep app-tier HPA independent and cap order-service HPA max replicas at 15.

## 2026-03-06 - Shared state infrastructure became the dominant checkout bottleneck

- Limitation encountered:
  - Locust checkout-only load saturated shared `redis-master` and single-node RabbitMQ, driving median checkout latency into multi-second range and capping throughput.

- Why the current design caused it:
  - Redis for order/stock/payment was consolidated onto one primary.
  - RabbitMQ ran as a single deployment pod for all command/reply traffic.
  - Participant consumer flow control stayed at a conservative prefetch setting.

- Impact:
  - **Performance risk (high)**: throughput collapsed under queueing despite app-tier HPA scaling.
  - **Reliability risk (medium)**: one overloaded infra pod became a system-wide chokepoint.

- Chosen mitigation or follow-up action:
  - Revert Kubernetes back to dedicated Redis instances per service (`order-db`, `stock-db`, `payment-db`) to remove cross-service write contention.
  - Replace plain RabbitMQ deployment with Bitnami RabbitMQ Helm cluster at odd cardinality (`replicaCount=3`).
  - Increase participant-side parallelism via higher stock/payment gunicorn workers and configurable RabbitMQ consumer prefetch.
  - Keep Redis primaries at a single replica count in HPA (`maxReplicas=1`) to avoid split-brain writes.

## 2026-03-06 - Bitnami chart defaults referenced unavailable public image tags

- Limitation encountered:
  - RabbitMQ StatefulSet pods failed with `ImagePullBackOff` for default chart image tags (`bitnami/rabbitmq` and `bitnami/os-shell`).

- Why the current design caused it:
  - The chart defaults target Bitnami image tags that are no longer publicly pullable in this environment.

- Impact:
  - **Delivery risk (high)**: deployment appears successful at Helm level but leaves broker unavailable at runtime.
  - **Reliability risk (high)**: all checkout traffic depending on RabbitMQ fails or retries indefinitely.

- Chosen mitigation or follow-up action:
  - Override chart images to `bitnamilegacy/rabbitmq` and `bitnamilegacy/os-shell`.
  - Set `global.security.allowInsecureImages=true` as required by chart validation for non-default image registries.
  - Roll the stuck RabbitMQ pod after upgrade so it picks up the new StatefulSet template.

## 2026-03-06 - HPA semantics are misleading for single-primary Redis

- Limitation encountered:
  - `order-db-hpa` repeatedly reported as "struggling" while `maxReplicas=1`, even though pod scaling is intentionally disabled to avoid split-brain writes.

- Why the current design caused it:
  - Utilization-based HPA (`% of CPU request`) is a poor signal for a fixed single-primary datastore.
  - Single-primary Redis cannot be safely scaled by replica count behind one write service in this topology.

- Impact:
  - **Reliability risk (medium)**: operators can misinterpret HPA alerts as an autoscaling failure instead of a topology limit.
  - **Performance risk (medium)**: effort goes into tuning HPA thresholds instead of addressing vertical capacity and workload profile.

- Chosen mitigation or follow-up action:
  - Keep `order-db` fixed at one replica.
  - Tune `order-db-hpa` to absolute CPU (`averageValue`) for observability instead of percentage of request.
  - Increase `order-db` vertical capacity and run Redis in no-persistence mode for PoC load tests.

## 2026-03-06 - Gunicorn sync workers serialized checkout concurrency

- Limitation encountered:
  - Order service used gunicorn sync workers (`-w 10`), meaning each process could handle only one request at a time. Since checkout blocks on `Event.wait()` during RabbitMQ round-trips (~50–150 ms per phase, two phases per checkout), each worker was idle-waiting for most of the request lifecycle. With 12 HPA pods × 10 workers = 120 max concurrent checkouts. At ~100 ms per checkout, theoretical throughput capped at ~1200 RPS.

- Why the current design caused it:
  - The default gunicorn worker class (`sync`) was inherited from earlier development where blocking I/O was not the dominant cost. Checkout's wait-for-reply pattern turns every worker process into a single-threaded blocking slot.

- Impact:
  - **Performance risk (high)**: checkout throughput was bounded by process count × round-trip time, not by CPU or network capacity. Adding more pods only partially helped because each pod's 10 workers were underutilized during waits.

- Chosen mitigation or follow-up action:
  - Switch order service to `gthread` worker class: `-w 4 --threads 10 -k gthread`.
  - Each pod now handles 40 concurrent requests (4 processes × 10 threads). 12 pods × 40 = 480 concurrent checkouts — a 4× increase.
  - Thread-safety is already ensured: publisher connections are thread-local, correlation map is lock-protected, Redis client is thread-safe.
  - Stock and payment services remain sync workers — their HTTP endpoints do not block on RabbitMQ (only the background consumer thread does).

## 2026-03-06 - Excessive per-step Redis writes in coordinator checkout path

- Limitation encountered:
  - A successful SAGA checkout performed ~12 individual Redis writes: `create_tx`, `update_tx` (HOLDING), `update_tx` (stock_held), `update_tx` (payment_held), `update_tx` (HELD), `set_decision`, `set_commit_fence`, `update_tx` (COMMITTING), `update_tx` (committed flags), `mark_paid`, `update_tx` (COMPLETED), `clear_commit_fence`. Each write is a separate network round-trip to `order-db`.

- Why the current design caused it:
  - The coordinator persisted every incremental state change for crash-recovery safety. Per-reply `update_tx` calls (stock_held, payment_held) and separate `set_decision` / `set_commit_fence` / `update_tx` calls were designed for maximum recoverability but created unnecessary round-trips when both replies arrive in the same `wait_for_replies` call.

- Impact:
  - **Performance risk (high)**: at 800 RPS, ~9600 Redis writes/sec just from tx management, contributing to `order-db` CPU saturation.
  - **Latency risk (medium)**: each round-trip adds ~0.5–1 ms, so 4 extra writes add 2–4 ms per checkout.

- Chosen mitigation or follow-up action:
  - Batch per-reply held flags into the decision-phase `update_tx` call (both `_run_saga` and `_run_2pc`). Flags are set in memory immediately but persisted once at the decision boundary.
  - Pipeline `set_decision` + `set_commit_fence` + `update_tx` into a single Redis pipeline (`set_decision_fence_and_update_tx`) for the commit path. Pipeline `set_decision` + `update_tx` into `set_decision_and_update_tx` for the abort/compensate path.
  - Happy-path SAGA reduced from ~12 to ~8 Redis writes (~33% reduction). Each pipeline saves 1–2 network round-trips.
  - Recovery correctness is preserved: the decision marker and tx record are written atomically in the same pipeline, and the held flags are always persisted before any commit/compensate commands are published.

## 2026-03-06 - TxStore optimization introduced an incompatible coordinator contract

- Limitation encountered:
  - Coordinator commit/abort paths started calling `set_decision_and_update_tx` and `set_decision_fence_and_update_tx` directly, which fails for valid legacy `TxStore` implementations that only provide `set_decision`, `set_commit_fence`, and `update_tx`.

- Why the current design caused it:
  - The write-optimization change implicitly treated new combined methods as mandatory and did not include an adapter/fallback path.

- Impact:
  - **Reliability risk (high)**: checkout paths can fail with `AttributeError` at runtime on stores or mocks that were previously compatible.
  - **Delivery risk (medium)**: coordinator protocol unit tests break unless every `TxStore` implementation is updated in lockstep.

- Chosen mitigation or follow-up action:
  - Add coordinator-local compatibility helpers that prefer combined methods when available.
  - Fallback to the legacy sequence (`set_decision`, optional `set_commit_fence`, then `update_tx`) when combined methods are absent.
  - Keep the optimization for Redis-backed stores while preserving portability to existing `TxStore` implementations.

## Update Rules

When adding a new entry:

1. State the limitation concretely.
2. Explain which design choice caused it.
3. Name the impact category:
   - consistency
   - performance
   - reliability
   - delivery
4. Record the chosen mitigation or the follow-up decision.
5. If the mitigation changes the active plan, update `plan/phase1-rebuild-plan.md` in the same change.
