# Further Refactor / Simplification Plan

## Summary

The next cleanup pass should focus on reducing duplicated internal interfaces, removing repeated remote reads, and separating runtime wiring from request handling. The main simplification targets are:

- the order snapshot shape, which currently exists in multiple internal forms
- the checkout path, which still reads the order twice on the happy path
- duplicated AMQP reply-consumer and worker bootstrap code
- app modules that still mix HTTP routing, MQ transport, dependency wiring, and domain logic
- duplicated live-stack test helpers and mocks

Recommended implementation order:

1. canonical order snapshot + checkout boundary cleanup
2. thin app modules + explicit runtime services
3. shared AMQP / worker scaffolding
4. coordinator helper cleanup + test harness cleanup

## Key Changes

### Pass 1: Canonical Order Snapshot + Checkout Boundary

- Introduce one canonical internal order snapshot type in `common/` and use it everywhere internal code currently uses separate snapshot structs.
- Keep message envelopes separate, but make internal MQ replies carry that canonical snapshot type directly.
- Change `CoordinatorService.execute_checkout(...)` so it accepts a prepared snapshot instead of re-reading the order itself.
- Make orchestrator checkout handling do the single authoritative `read_order`, handle `not_found` and `already_paid`, acquire the active guard, and then call the coordinator with that snapshot.
- Keep recovery paths using `OrderPort.read_order(...)`; only the request-time checkout path loses the duplicate read.

### Pass 2: Thin App Modules + Explicit Runtime Services

- Extract order-to-orchestrator MQ calls and mutation-guard behavior out of `order/app.py` into a dedicated internal port/service object.
- Move the `addItem` optimistic write loop into store/service code that returns structured results instead of calling `abort()` from helper logic.
- Keep `order/app.py` limited to request parsing, validation, and HTTP response mapping.
- Move orchestrator runtime wiring out of `orchestrator/app.py` into an explicit runtime/service object that owns the tx-store adapter, order port, coordinator instance, and command handlers.
- Move `_TxStoreImpl` out of the Flask app module.
- Remove current `ModuleNotFoundError` import fallbacks by injecting handler/runtime dependencies into workers during startup instead of importing Flask-app symbols from worker code.

### Pass 3: Shared AMQP / Worker Scaffolding

- Create one shared helper for exclusive auto-delete reply-consumer threads: queue declare, QoS, reconnect/backoff, and thread startup live in one place.
- Keep correlation semantics local: `common/rpc.py` keeps one-request/one-reply correlation, while `coordinator/messaging.py` keeps phase-aware multi-reply correlation.
- Extract shared internal-RPC worker plumbing for order/orchestrator: decode, validate `reply_to` and `correlation_id`, ack/nack, and publish correlated reply.
- Extract shared participant-worker plumbing for stock/payment: decode, ack/nack, and publish participant reply.
- Move duplicated `_get_positive_int_env(...)` logic into one common config/env helper.
- Centralize repeated gunicorn `post_fork` bootstrap patterns and leave only service-specific startup behavior in each service config.

### Pass 4: Coordinator Helper Cleanup + Test Harness Cleanup

- Keep SAGA and 2PC in one coordinator class, but factor repeated transport mechanics into private helpers.
- Extract helpers for building stock/payment participant commands, computing expected services for a phase, publishing and waiting for one phase, and applying reply outcomes back onto the tx state.
- Do not split protocol execution into a class hierarchy; simplify repeated mechanics only.
- Move shared test doubles into `test/helpers`, including the mock tx store, mock order port, and fake watch redis.
- Split `test_step2_coordinator.py` by subject area and reuse shared doubles instead of keeping one large fixture-heavy file.
- Make `test_checkout_failure_modes.py` reuse `test/live_stack_utils.py` instead of duplicating Docker and RabbitMQ helpers.

## Test Plan

- Unit: canonical order snapshot serializes and deserializes through internal MQ replies without field drift.
- Unit: checkout happy path performs exactly one `read_order(...)`; recovery still performs reads as needed.
- Unit: internal order/orchestrator workers still nack malformed messages and publish replies with correct correlation ids.
- Unit: `addItem` keeps the same missing-order, already-paid, conflict, and guard-blocked behavior.
- Unit: coordinator helper extraction preserves all existing SAGA and 2PC transitions, including `mark_paid_indeterminate`.
- Integration: existing Step 2, Step 3, and Step 4 test suites remain green after each pass.
- Live stack: participant-worker, checkout-failure-mode, and recovery-convergence tests pass after shared helper consolidation.

## Assumptions and Defaults

- Public HTTP API, RabbitMQ queue names, Redis key names, status codes, and protocol semantics remain unchanged.
- This is a deeper cleanup pass, but not a package-layout rewrite; Docker and Gunicorn entrypoints should stay stable unless a specific refactor requires a small wiring change.
- `CoordinatorService` remains the single owner of protocol execution.
- Participant command/reply models stay separate from the internal order/orchestrator RPC models.
