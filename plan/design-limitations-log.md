# Design Limitations Log

## Purpose

This log records design limitations, why they occurred, what they impact, and the chosen mitigation.
It should be updated whenever implementation reveals a new constraint, incorrect assumption, or architectural mismatch.

Use this log to avoid repeating known mistakes and to justify walking back earlier decisions when needed.

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

- Limitation encountered:
  - It is easy to conflate request mutual exclusion, transaction ownership, and prepared resource protection into one lock concept.

- Why the current design causes it:
  - All three mechanisms seem related to "locking," but they protect different things and live at different durability levels.

- Impact:
  - Consistency risk: clearing a request lock or losing a lease can accidentally be treated as permission to begin a new checkout.
  - Reliability risk: rollback and recovery logic can release the wrong protection at the wrong time.

- Chosen mitigation or follow-up action:
  - Keep the per-order lease lock only for short request-path mutual exclusion.
  - Keep a separate durable active transaction guard until the tx is truly safe to re-enter.
  - Keep prepared holds in participant stores as durable resource reservations until `commit` or `abort`.

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

- Limitation encountered:
  - A transaction summary record and an append-only tx log can contradict each other if updated separately for the same transition.

- Why the current design causes it:
  - Adding a tx step log improves observability, but it also creates a second source of truth unless writes are coordinated.

- Impact:
  - Reliability risk: recovery can still face ambiguous crash windows despite having more recorded data.

- Chosen mitigation or follow-up action:
  - Update the summary and the corresponding log entry in one atomic write unit for the same transition.
  - Treat divergence between the two as a correctness bug, not a normal recovery state.

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

- Limitation encountered:
  - A message bus was considered for the checkout path to improve replayability, recovery, and load distribution.

- Why the current design causes it:
  - Durable messaging is attractive because it offers redelivery and decoupling, especially for SAGA-style workflows.
  - However, the checkout API is still externally synchronous and 2PC still needs a synchronous logical decision point.

- Impact:
  - Performance risk: extra publish/consume/reply hops increase tail latency on the hot path.
  - Delivery risk: the team can drift into a larger infrastructure project before stabilizing Phase 1 correctness.

- Chosen mitigation or follow-up action:
  - Keep the Phase 1 checkout path on direct internal HTTP.
  - Use durable Redis transaction state plus a tx step log for replay and recovery.
  - Revisit a message bus later only for background repair, async SAGA variants, or a later-phase architecture.

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
