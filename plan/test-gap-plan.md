# Test Gap Plan

## Purpose

This plan captures the explicit test gaps identified while comparing `dds26-1` against the sibling `dds26-*` repos.

The goal is not to add more tests indiscriminately. The goal is to close the specific coverage holes that still leave correctness risks in:

- concurrent order mutation
- duplicate and in-flight checkout behavior
- invalid input handling
- participant idempotency and messaging integration
- crash and recovery convergence
- stress and consistency under load

## Current Position

`dds26-1` already has stronger protocol and recovery tests than the sibling repos:

- unit tests for coordinator protocol transitions
- unit tests for recovery worker behavior
- failure-mode tests for active-tx guard and stale replies

Those gaps were concentrated in places where the suite did not yet exercise the real system end-to-end or under concurrency pressure.

## Implementation Status

Implemented on 2026-03-11:

- `test/test_step4_order_api.py`
  - concurrent `addItem`
  - mutation rejection on paid/in-flight orders
  - same-order checkout storm
- `test/test_step4_stock_validation.py`
  - invalid numeric input coverage for stock endpoints
- `test/test_step4_payment_validation.py`
  - invalid numeric input coverage for payment endpoints
- `test/test_step4_order_http_helpers.py`
  - bounded internal HTTP failure behavior
- `test/test_step5_participant_workers.py`
  - real RabbitMQ worker replay/idempotency
  - malformed-message dead-lettering
  - `tx_not_found` release/commit no-op semantics
- `test/test_step5_recovery_convergence.py`
  - live-stack recovery convergence from `FAILED_NEEDS_RECOVERY` to terminal abort
  - guard cleanup and clean retry after recovery
- `test/test_stress_consistency.py`
  - repeatable stress/consistency harness
  - included in the default automated test run

Plan status:

- P0 gaps are covered by automated tests.
- P1 gaps are covered by automated tests.
- P2 stress coverage exists in the default automated test run.

## Missing Tests

### 1. Concurrent `addItem` on the same order

- Risk:
  - `order/app.py` currently uses a read-modify-write update for `/orders/addItem/...`.
  - Parallel requests can overwrite each other and corrupt `items` or `total_cost`.
- Test to add:
  - create one order
  - issue multiple concurrent `POST /orders/addItem/{order_id}/{item_id}/{quantity}` requests
  - assert all item additions are present
  - assert `total_cost` matches the sum of all accepted additions
- Recommended test type:
  - integration test through the gateway
- Priority:
  - P0

### 2. Order mutation after checkout starts or completes

- Risk:
  - the suite does not prove that a paid order or in-flight order cannot be mutated afterwards
  - this can become a free-item or inconsistent-total bug
- Tests to add:
  - reject `addItem` after a successful checkout
  - reject `addItem` while checkout is already in progress
  - verify a failed checkout leaves the order mutable only if that is the intended policy
- Recommended test type:
  - integration test through the gateway
- Priority:
  - P0

### 3. Invalid numeric inputs on public endpoints

- Risk:
  - current tests only use positive quantities and amounts
  - zero and negative values are not explicitly rejected by the suite
  - this leaves room for stock inflation, credit inflation, or negative total-cost orders
- Tests to add:
  - `/stock/add`, `/stock/subtract` with `0` and negative amounts
  - `/payment/add_funds`, `/payment/pay` with `0` and negative amounts
  - `/orders/addItem` with `0` and negative quantity
  - checkout of an order containing invalid line items must fail safely
- Recommended test type:
  - API integration tests
- Priority:
  - P0

### 4. Real participant-worker integration tests

- Risk:
  - coordinator logic is well unit-tested, but worker/message behavior is mostly covered indirectly
  - the suite does not directly prove participant idempotent replay, malformed-message handling, or queue-level behavior
- Tests to add:
  - publish real `hold` commands to stock and payment command queues
  - assert success/failure reply payloads and resulting Redis state
  - replay the same `tx_id` and assert no double side effects
  - inject malformed messages and assert they are rejected safely
  - verify release/commit no-op semantics for `tx_not_found`
- Recommended test type:
  - integration tests against RabbitMQ + service workers
- Priority:
  - P1

### 5. End-to-end recovery convergence

- Risk:
  - we unit-test recovery behavior and separately test some failure modes
  - we do not yet prove that a real non-terminal transaction converges after service restart or worker recovery
- Tests to add:
  - force checkout into `FAILED_NEEDS_RECOVERY`
  - restart the failed participant or restore broker connectivity
  - assert the recovery worker resumes the transaction
  - assert final state becomes terminal and guard/fence cleanup occurs
  - verify no double charge and no stock leak after recovery
- Recommended test type:
  - docker-compose integration test
- Priority:
  - P1

### 6. Same-order checkout storm

- Risk:
  - current tests prove one retry conflict but not high-contention behavior on the same order
  - this leaves a gap around duplicate charge or duplicate stock subtraction under bursty retries
- Tests to add:
  - multiple concurrent `POST /orders/checkout/{same_order_id}` calls
  - assert at most one logical commit path succeeds
  - assert the final stock and credit deltas occur exactly once
  - assert losers receive conflict or failure responses quickly
- Recommended test type:
  - integration test through the gateway
- Priority:
  - P1

### 7. Internal HTTP timeout and bounded-failure behavior

- Risk:
  - order-service helper calls currently have no timeout or retry policy
  - without tests, a hanging dependency can hang request threads indefinitely
- Tests to add:
  - simulate an unreachable stock or payment dependency for `addItem` and checkout-related calls
  - assert failure happens within a bounded time
  - assert no partial state is left behind
- Recommended test type:
  - integration test with dependency stop or blackhole simulation
- Priority:
  - P1

### 8. Stress and consistency verification

- Risk:
  - the suite is strong on protocol edge cases but weak on prolonged concurrency evidence
  - sibling repos, especially `dds26-team9`, at least attempt consistency and contention checks under load
- Tests to add:
  - repeated concurrent checkouts against shared stock/users
  - post-run invariant checks:
    - no negative stock
    - no negative credit
    - money deducted matches committed purchases
    - stock deducted matches committed purchases
    - no orders left in non-terminal coordinator states
- Recommended test type:
  - stress script plus deterministic consistency verifier
- Priority:
  - P2

## Existing Coverage We Should Not Duplicate

- `_aggregate_items(...)` is already covered in `test/test_step2_coordinator.py`
- coordinator state-machine transitions already have strong unit coverage
- recovery worker guard and lock behavior already has direct unit coverage
- stale hold-reply and active guard regressions already have dedicated failure-mode coverage

New tests should target uncovered system behaviors rather than re-testing those internals.

## Recommended Delivery Order

1. Add concurrent `addItem` tests.
2. Add invalid input tests for all public mutation endpoints.
3. Add order-mutation-after-checkout tests.
4. Add same-order checkout storm test.
5. Add participant-worker integration tests for hold/release/commit replay behavior.
6. Add end-to-end recovery convergence tests.
7. Add bounded-failure tests for dependency timeouts.
8. Add repeatable stress plus consistency verification scripts.

## Exit Criteria

This plan is complete when:

- each P0 and P1 gap has at least one automated test
- the new tests fail on a real regression instead of only asserting status codes
- consistency checks validate stock, credit, and final order state together
- recovery tests prove convergence to a terminal state after an induced failure
- stress tests are documented, runnable, and included in the normal automated run

Current status:

- Complete. The exit criteria above are now satisfied.

## Notes

- The highest-value implementation follow-up is likely to be concurrent `addItem` protection, because the missing test exposes a real correctness risk rather than a hypothetical one.
- Participant integration tests should remain small and surgical; they are meant to validate queue semantics and idempotency, not replace the coordinator suite.
