# Phase 2 Extraction Notes: Standalone Orchestrator

## Purpose

This document captures the intended Phase 2 direction from `assignment.md` without expanding the current Phase 1 implementation scope too early.

It is not a full Phase 2 implementation plan.

Its purpose is to:

1. define what moves into the orchestrator
2. define what stays in the domain services
3. define which Phase 1 boundaries must remain stable to make extraction practical
4. reduce the risk that Phase 1 code choices create a costly rewrite later

## Assignment Context

Per `assignment.md`, Phase 2 (final deliverable) is due on **April 1, 2026**.

The core expectation is:

1. abstract SAGA and app-level 2PC into a separate software artifact called an orchestrator
2. rewrite the shopping-cart project so it uses that orchestrator instead of embedding two-phase-commit-like behavior directly in application code

This document exists to make the Phase 1 rebuild extraction-friendly, not to pre-build the full Phase 2 system now.

## Scope Boundary

In scope for this document:

1. architecture target for the orchestrator
2. stable interfaces and contracts
3. extraction sequence from the Phase 1 design
4. risks and constraints that Phase 1 must respect

Out of scope for this document:

1. production-ready deployment details for the new orchestrator service
2. choosing a message bus now
3. changing the current public API
4. implementing a different consistency model than the Phase 1 protocol core

## Phase 2 Goal

The Phase 2 goal is to relocate orchestration logic, not redesign transaction semantics from scratch.

That means:

1. the same two protocol families still exist:
   - SAGA
   - app-level 2PC
2. the main change is where that logic runs and how services talk to it
3. the public `order`, `payment`, and `stock` APIs remain stable

If Phase 2 requires rewriting the protocol state machine itself, the Phase 1 boundary was not clean enough.

## Target Runtime Shape

Target Phase 2 runtime:

1. `orchestrator` service:
   - owns protocol execution
   - owns transaction state machine
   - owns transaction recovery
   - publishes commands to participant RabbitMQ queues (same queues as Phase 1)
   - consumes replies from coordinator reply queue
2. `order` service:
   - remains the public order API
   - manages order-domain state only
   - delegates checkout orchestration to the orchestrator (via HTTP or RabbitMQ call)
3. `payment` service:
   - remains the payment-domain API
   - RabbitMQ consumer for transaction commands (unchanged from Phase 1)
4. `stock` service:
   - remains the stock-domain API
   - RabbitMQ consumer for transaction commands (unchanged from Phase 1)

The RabbitMQ-based architecture makes Phase 2 extraction simpler: participants already communicate through message queues, not in-process calls to the order service. Moving the coordinator into a separate service only changes who publishes to those queues.

## Responsibility Split

### Orchestrator Owns

1. protocol selection
2. transaction creation
3. transaction status transitions
4. durable transaction summary and decision markers
5. commit-fence handling
6. recovery and resume logic
7. participant coordination
8. deciding whether a transaction commits, aborts, compensates, or remains in recovery

### Order Service Owns

1. order creation
2. order lookup
3. adding items to orders
4. persisting `paid` on successful finalization
5. the public `POST /orders/checkout/{order_id}` entrypoint as a thin adapter

### Payment Service Owns

1. user creation
2. credit lookup
3. adding funds
4. durable participant-side holds and payment apply/reverse logic

### Stock Service Owns

1. item creation
2. stock lookup
3. stock add/subtract public operations
4. durable participant-side reservations and stock apply/release logic

## Stable Interfaces That Phase 1 Must Preserve

These are the interfaces the Phase 1 rebuild should treat as long-lived seams.

### 1. Coordinator Service API (in-process in Phase 1)

In Phase 1, this exists as a local code boundary.

In Phase 2, this becomes a remote orchestrator API.

Stable conceptual operations:

1. `execute_checkout(order_id, order_snapshot, mode)`
2. `resume_transaction(tx_id)`
3. `get_transaction_status(tx_id)` if needed for diagnosis

The exact transport can change later, but the call semantics should remain stable.

### 2. Participant Internal APIs

The participant-side internal operations should remain stable between phases.

Payment:

1. saga pay
2. saga refund
3. 2PC prepare
4. 2PC commit
5. 2PC abort

Stock:

1. saga reserve
2. saga release
3. 2PC prepare
4. 2PC commit
5. 2PC abort

If these are redesigned completely in Phase 2, the extraction cost will be much higher than necessary.

### 3. Result Semantics

The structured result model produced by the coordinator should remain stable:

1. success
2. business failure
3. in-progress / conflict
4. recovery-required or ambiguous-failure cases

Routes can keep mapping these to HTTP, but the internal meaning should not depend on Flask.

### 4. Transaction State Model

The meaning of transaction states should remain stable even if storage ownership moves.

That includes:

1. safe-to-reenter vs not-safe-to-reenter semantics
2. commit-fence meaning
3. recovery-required meaning
4. participant prepared/committed/aborted semantics

### 5. Internal Decision Query API

The `GET /internal/tx_decision/{tx_id}` endpoint on the order service (or orchestrator in Phase 2) must remain stable. Participants use this for startup reconciliation of non-terminal local tx records.

## Data Ownership After Extraction

### Phase 1

In Phase 1:

1. `order` physically owns:
   - order-domain records
   - coordinator transaction records
   - coordinator metadata

This is acceptable because the coordinator is embedded.

### Phase 2

In Phase 2:

1. `order` should keep order-domain records
2. `orchestrator` should own coordinator transaction state
3. `payment` and `stock` should keep participant local transaction state

This means the Phase 1 code should already separate:

1. domain persistence interfaces
2. orchestration persistence interfaces

If those are still mixed, extraction becomes a rewrite.

## Extraction Strategy

The preferred migration path is incremental.

### Step 1: Keep the protocol core unchanged

Move the coordinator package mostly as-is into the new orchestrator service.

Do not redesign:

1. the state machine
2. the status taxonomy
3. the participant semantics

### Step 2: Replace in-process coordinator calls with network calls

The `order` checkout route should stop calling the coordinator in-process and instead call the orchestrator service.

The route should still remain thin:

1. validate request
2. load order snapshot
3. invoke orchestrator
4. map the structured result to HTTP

### Step 3: Move recovery with the coordinator

The recovery worker should move with the orchestrator.

That means:

1. the orchestrator becomes the owner of transaction resumption
2. `order` should no longer run the main transaction recovery loop

### Step 4: Keep participant APIs stable during the move

`payment` and `stock` should not need major behavioral rewrites if the participant contract is already clean.

### Step 5: Revalidate all Phase 1 invariants

Extraction is not complete until the same consistency guarantees still hold after the coordinator becomes remote.

## Design Rules That Protect Phase 2

These rules should be enforced already in Phase 1.

1. The coordinator package must not import Flask.
2. Protocol modules must not construct HTTP responses.
3. Route handlers must not contain protocol logic.
4. Recovery must go through coordinator-owned entrypoints.
5. Participant behavior must be reusable from any caller, not coupled to the `order` service.
6. Domain records and transaction-control records must remain conceptually separate.
7. Code duplication in protocol or recovery logic is a direct extraction risk.

## Temporary Phase 1 Assumptions

These assumptions are intentionally temporary and should be revisited in Phase 2.

1. The coordinator runs in the same deployable as `order`.
2. The `order` service physically stores coordinator transaction state.
3. The checkout route calls the coordinator in-process. In Phase 2, this becomes a network call.
4. Participant communication already uses RabbitMQ — this does NOT change in Phase 2.
5. Recovery runs as a single instance (no distributed leader lock). Scaling requires adding leader election.
6. Intermediate protocol steps are observed via application logs, not durable per-tx Redis logs. Phase 2 may introduce structured event storage if needed.

These are acceptable only because the code boundary is already separated.

## Deferred Decisions For Phase 2

These are valid future questions, but they are intentionally deferred now.

1. Whether the orchestrator should use direct HTTP or a message bus as transport
2. Whether SAGA and 2PC should remain startup-selected or become a richer orchestrator configuration concern
3. Whether the orchestrator should expose internal status/debug endpoints
4. Whether the orchestrator should use its own dedicated datastore or continue using Redis in a similar pattern

Deferring these choices is intentional. They should not distort the Phase 1 rebuild.

## Phase 2 Risks To Watch For

1. Re-introducing transport concerns into protocol code during extraction
2. Letting `order` keep partial orchestration logic "for convenience"
3. Splitting transaction ownership ambiguously between `order` and `orchestrator`
4. Rewriting participant APIs mid-extraction instead of preserving stable contracts
5. Adding a message bus before the remote synchronous path is functionally stable

## Acceptance Criteria For Phase 2 Readiness

The Phase 1 rebuild can be considered genuinely Phase-2-ready only if all of the following are true.

1. The coordinator can be moved into another service with mostly adapter and wiring changes.
2. Protocol code is transport-agnostic.
3. Recovery logic moves with the coordinator instead of being reimplemented elsewhere.
4. `order`, `payment`, and `stock` can keep their current public APIs.
5. The participant internal contracts are stable enough to be reused by the extracted orchestrator.
6. The same consistency invariants remain explainable before and after extraction.

## Relationship To The Phase 1 Plan

This document is subordinate to [plan/phase1-rebuild-plan.md](C:/Users/Theo/source/repos/dds26-1/plan/phase1-rebuild-plan.md).

Interpretation rule:

1. if a Phase 2 idea conflicts with Phase 1 correctness or delivery, Phase 1 wins now
2. if a Phase 1 implementation choice makes Phase 2 extraction obviously harder, that choice should be reconsidered before coding further
