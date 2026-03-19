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

1. `orchestrator` service (internal only — not publicly accessible):
   - receives checkout requests proxied from the order service
   - owns protocol execution (SAGA + 2PC, selected by `CHECKOUT_PROTOCOL` flag)
   - owns transaction state machine and all tx persistence (own Redis instance)
   - owns active-tx guard acquisition and clearing
   - owns transaction recovery (background worker)
   - publishes commands to participant RabbitMQ queues (same queues as Phase 1)
   - consumes replies from per-process exclusive reply queues (same pattern as Phase 1)
   - calls order service via `OrderPort` (HTTP) to read orders and mark paid
   - serves `GET /internal/tx_decision/{tx_id}` for participant reconciliation
2. `order` service:
   - remains the public order API for all `/orders/*` routes
   - `POST /orders/checkout/{order_id}` is a thin proxy: forwards to orchestrator via internal HTTP, returns the orchestrator's response
   - manages order-domain state only (own Redis instance, same as Phase 1)
   - exposes internal APIs: `GET /internal/orders/{order_id}`, `POST /internal/orders/{order_id}/mark_paid`
   - no longer contains any checkout logic, transaction state, or recovery — only the proxy route
3. `nginx` gateway:
   - routes all `/orders/*` → order service (unchanged from Phase 1)
   - routes `/stock/*` → stock service
   - routes `/payment/*` → payment service
   - the orchestrator is NOT exposed through nginx — it is internal only
4. `payment` service:
   - remains the payment-domain API
   - RabbitMQ consumer for transaction commands (unchanged from Phase 1)
   - queries orchestrator for `tx_decision` on startup reconciliation
5. `stock` service:
   - remains the stock-domain API
   - RabbitMQ consumer for transaction commands (unchanged from Phase 1)
   - queries orchestrator for `tx_decision` on startup reconciliation
6. `RabbitMQ`:
   - same queue topology as Phase 1: `stock.commands`, `payment.commands`, per-process reply queues, DLQs
   - the only difference is that the orchestrator (not the order service) publishes to command queues

The RabbitMQ-based architecture makes extraction simple: participants already communicate through message queues. Moving the coordinator into a separate service only changes who publishes to those queues and who consumes the replies.

## Responsibility Split

### Orchestrator Owns

1. checkout execution — receives proxied requests from the order service's `POST /orders/checkout/{order_id}` route
2. protocol selection (`CHECKOUT_PROTOCOL` flag)
3. transaction creation and all tx persistence (own Redis: `tx:{tx_id}`, `tx_decision:{tx_id}`)
4. transaction status transitions and state machine
5. active-tx guard lifecycle (`order_active_tx:{order_id}` in orchestrator Redis)
6. commit-fence handling (`order_commit_fence:{order_id}` in orchestrator Redis)
7. order-mutation guard lifecycle (`order_mutation_guard:{order_id}` in orchestrator Redis)
8. recovery worker and `resume_transaction` logic
9. participant coordination via RabbitMQ
10. deciding whether a transaction commits, aborts, compensates, or remains in recovery
11. `GET /internal/tx_decision/{tx_id}` endpoint
12. internal mutation-guard endpoints used by the order service before mutating an order
13. already-paid fast path (reads order via `OrderPort`, returns 200 if paid with no tx overhead)

### Order Service Owns

1. all public `/orders/*` routes, including the checkout proxy
2. `POST /orders/checkout/{order_id}` — thin proxy to orchestrator (forwards request, returns response)
3. order creation, lookup, and item management (`/orders/create`, `/orders/find`, `/orders/addItem`, `/orders/removeItem`)
4. order-domain persistence (own Redis: `order:{order_id}`)
5. `POST /internal/orders/{order_id}/mark_paid` — called by orchestrator via `OrderPort`
6. `GET /internal/orders/{order_id}` — called by orchestrator via `OrderPort`
7. `addItem` checks with the orchestrator before mutating an order so checkout and mutation remain mutually exclusive after the active-tx guard moves out of order Redis

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

## Stable Interfaces That Phase 1 Preserves

Phase 1 defines concrete port interfaces and contracts that Phase 2 relies on. These are the long-lived seams.

### 1. Coordinator Port Interfaces (defined in `coordinator/ports.py`)

The coordinator accesses all external state through two abstract interfaces. Phase 2 extraction works by swapping their implementations.

#### `OrderPort`

```python
class OrderPort(Protocol):
    def read_order(self, order_id: str) -> Optional[OrderSnapshot]: ...
    def mark_paid(self, order_id: str, tx_id: str) -> bool: ...
```

- Phase 1: direct Redis calls in `order/store.py`.
- Phase 2: HTTP calls to internal order-service APIs (see "New Internal APIs" below).

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

- Phase 1: direct Redis calls in `order/store.py`, all keys in order's Redis.
- Phase 2: local Redis calls inside the orchestrator against the orchestrator's own Redis instance. No cross-service access.

### 2. Coordinator Service API

In Phase 1, this is an in-process call. In Phase 2, it becomes a remote call.

Stable operations:

1. `execute_checkout(order_id, order_snapshot, mode)`
2. `resume_transaction(tx_id)`
3. `get_transaction_status(tx_id)` if needed for diagnosis

### 3. Participant Message Contract

Participants consume from RabbitMQ and implement three idempotent operations:

- `hold` (SAGA reserve / 2PC prepare)
- `release` (SAGA compensate / 2PC abort)
- `commit` (SAGA commit / 2PC commit)

Message types are `msgspec.Struct` definitions in `common/models.py`. Participants do not know or care whether the coordinator is embedded or remote. This contract is unchanged in Phase 2.

### 4. Result Semantics

The structured result model produced by the coordinator remains stable:

1. success
2. business failure
3. in-progress / conflict
4. recovery-required or ambiguous-failure cases

Routes map these to HTTP. The internal meaning does not depend on Flask.

### 5. Transaction State Model

The meaning of transaction states remains stable even as storage ownership moves:

1. safe-to-reenter vs not-safe-to-reenter semantics
2. commit-fence meaning
3. recovery-required meaning
4. participant prepared/committed/aborted semantics

### 6. Internal Decision Query API

`GET /internal/tx_decision/{tx_id}` — participants use this for startup reconciliation of non-terminal local tx records.

- Phase 1: served by the order service (tx state in order's Redis).
- Phase 2: served by the orchestrator (tx state in orchestrator's Redis). Participants' `ORDER_SERVICE_URL` config changes to `ORCHESTRATOR_SERVICE_URL`.

## Data Ownership After Extraction

### Phase 1

In Phase 1, the order service's Redis physically stores everything:

- order-domain records (`order:{order_id}`)
- coordinator tx records (`tx:{tx_id}`)
- coordinator metadata (`tx_decision:{tx_id}`, `order_active_tx:{order_id}`, `order_commit_fence:{order_id}`)

This is acceptable because the coordinator is embedded. The `TxStorePort` interface already separates access so the coordinator never calls Redis directly.

### Phase 2

In Phase 2, storage splits:

| Owner | Data | Redis instance |
|-------|------|----------------|
| `order` | order-domain records (`order:{order_id}`) | order Redis (existing) |
| `orchestrator` | tx records, decision markers, active-tx guards, mutation guards, commit fences | orchestrator Redis (new) |
| `payment` | user records, participant tx records | payment Redis (existing) |
| `stock` | item records, participant tx records | stock Redis (existing) |

The orchestrator gets its own Redis instance and owns all coordinator state. The `TxStorePort` implementation becomes local Redis calls inside the orchestrator — no cross-service Redis access. The `OrderPort` implementation swaps from direct Redis to HTTP calls to the order service.

This split is mechanical because Phase 1 already separates domain persistence from orchestration persistence behind port interfaces.

## Extraction Strategy

The migration is incremental and driven by the port interfaces.

### Step 1: Create the orchestrator service scaffold

1. New service in `orchestrator/` with its own `Dockerfile`, Flask app, and gunicorn config.
2. Add a dedicated Redis instance for the orchestrator in `docker-compose.yml`.
3. Move `coordinator/` into the orchestrator service. The package moves as-is — protocol logic, state machine, recovery, and messaging are unchanged.
4. Copy `common/` as a shared dependency (same as Phase 1).

### Step 2: Convert the checkout route to a proxy

The order service's `POST /orders/checkout/{order_id}` route becomes a thin proxy that forwards to the orchestrator:

```python
@app.route('/orders/checkout/<order_id>', methods=['POST'])
def checkout(order_id):
    resp = requests.post(f'{ORCHESTRATOR_URL}/checkout/{order_id}')
    return resp.content, resp.status_code, resp.headers.items()
```

The orchestrator exposes an internal checkout endpoint (e.g., `POST /checkout/{order_id}`) that contains the same logic that was previously in-process: validate order (via `OrderPort`), check fast path, acquire guard, run protocol, clear guard, return result.

Why proxy instead of direct nginx routing:

1. The orchestrator must not be publicly accessible from the internet. It is an internal-only service, reachable only by other services on the Docker network.
2. The nginx gateway config stays simple — all `/orders/*` routes go to the order service, no split routing needed.
3. The order service retains ownership of all public `/orders/*` routes, which matches the assignment's service-per-route-prefix model.

Add `ORCHESTRATOR_URL` to the order service's environment in `docker-compose.yml`.

### Step 3: Implement new `OrderPort` (HTTP) inside the orchestrator

The orchestrator can no longer call `order/store.py` directly. Implement an HTTP-backed `OrderPort`:

```python
class HttpOrderPort:
    def __init__(self, order_service_url: str): ...
    def read_order(self, order_id: str) -> Optional[OrderSnapshot]:
        # GET {order_service_url}/internal/orders/{order_id}
    def mark_paid(self, order_id: str, tx_id: str) -> bool:
        # POST {order_service_url}/internal/orders/{order_id}/mark_paid
```

Add corresponding internal endpoints to the order service:

- `GET /internal/orders/{order_id}` — returns order snapshot (items, user_id, total_cost, paid)
- `POST /internal/orders/{order_id}/mark_paid` — idempotent mark-paid, body includes `tx_id` for audit

### Step 4: Implement local `TxStorePort` inside the orchestrator

The `TxStorePort` implementation becomes direct Redis calls against the orchestrator's own Redis instance. This is structurally identical to the Phase 1 implementation in `order/store.py` — just pointing at a different Redis. All tx keys (`tx:{tx_id}`, `tx_decision:{tx_id}`, `order_active_tx:{order_id}`, `order_commit_fence:{order_id}`) now live in orchestrator Redis.

### Step 5: Move guard ownership to the orchestrator

In Phase 1, the checkout route acquires the active-tx guard before calling the coordinator. In Phase 2:

1. The orchestrator owns both acquisition and clearing of the active-tx guard (it lives in orchestrator Redis now).
2. The orchestrator's checkout route acquires the guard, runs the protocol, and clears the guard on terminal state.
3. No split ownership — the orchestrator is the single owner of the entire checkout lifecycle.
4. Because `addItem` still lives in the order service, the orchestrator also owns a short-lived order-mutation guard used by the order service before mutating an order. Checkout must not start while a mutation guard exists, and order mutation must not start while an active checkout guard exists.

### Step 6: Move recovery with the coordinator

1. The recovery worker moves into the orchestrator service.
2. The order service no longer runs any transaction recovery logic.
3. The orchestrator scans its own Redis for non-terminal transactions and re-publishes commands to the same RabbitMQ queues.

### Step 7: Move `GET /internal/tx_decision/{tx_id}` to the orchestrator

1. The orchestrator serves this endpoint (tx state is in its Redis now).
2. Update `ORDER_SERVICE_URL` to `ORCHESTRATOR_SERVICE_URL` in stock and payment service configs.
3. Participants query the orchestrator for decision reconciliation on startup.

### Step 8: Strip checkout logic from the order service

1. Replace the checkout route in `order/routes/public.py` with the thin proxy (Step 2).
2. Delete all coordinator imports, recovery worker startup, and `CHECKOUT_PROTOCOL` handling from `order/app.py`.
3. The order service becomes a CRUD service for orders (create, find, addItem, removeItem) plus the proxy route and two internal endpoints. It has no knowledge of SAGA, 2PC, tx state, or recovery.

### Step 9: Keep participant services unchanged

Stock and payment services require zero code changes:

- They consume from the same RabbitMQ queues (`stock.commands`, `payment.commands`).
- They reply to the same `reply_to` queue specified in each message.
- They use the same Lua scripts and idempotent message handlers.
- The only config change is the URL for `tx_decision` queries (Step 7).

### Step 10: Revalidate all Phase 1 invariants

Extraction is not complete until the same consistency guarantees hold after the coordinator becomes remote:

1. Run the full test suite against the new topology.
2. Run the consistency benchmark with zero stock/payment drift.
3. Run fault-injection tests (kill orchestrator mid-checkout → recovery converges after restart).
4. Verify the already-paid fast path works in the orchestrator (reads order via `OrderPort`, returns 200 if paid).

## Design Rules That Protect Phase 2

These rules are enforced in Phase 1 and must remain true through extraction.

1. The coordinator package must not import Flask.
2. The coordinator must access order domain state exclusively through `OrderPort`.
3. The coordinator must access tx persistence exclusively through `TxStorePort`.
4. Protocol modules must not construct HTTP responses.
5. Route handlers must not contain protocol logic.
6. Recovery must go through coordinator-owned entrypoints (`resume_transaction`).
7. Participant behavior must be reusable from any caller, not coupled to the `order` service.
8. Domain records and transaction-control records must remain separated (different port interfaces, different Redis instances in Phase 2).
9. Code duplication in protocol or recovery logic is a direct extraction risk.

## Temporary Phase 1 Assumptions

These assumptions are intentionally temporary. Each has a known Phase 2 resolution.

| # | Phase 1 assumption | Phase 2 resolution |
|---|---|---|
| 1 | Coordinator runs in the same deployable as `order` | Moves to standalone orchestrator service |
| 2 | `order` Redis physically stores all tx state | Orchestrator gets its own Redis instance |
| 3 | Checkout route lives in order service and calls coordinator in-process | Checkout route becomes a thin proxy to the orchestrator via internal HTTP; orchestrator owns the full lifecycle |
| 4 | `OrderPort` is a direct Redis call | Swaps to HTTP call to order service internal API |
| 5 | `TxStorePort` points at order's Redis | Points at orchestrator's own Redis (structurally identical code) |
| 6 | `GET /internal/tx_decision/{tx_id}` served by order service | Moves to orchestrator; participant config updated |
| 7 | Recovery runs as a single instance (no distributed leader lock) | Scaling the orchestrator requires adding leader election |
| 8 | Participant communication uses RabbitMQ | **Unchanged in Phase 2** — same queues, same messages |
| 9 | Intermediate protocol steps observed via app logs only | Phase 2 may introduce structured event storage if needed |

These are acceptable because all coordinator state access goes through port interfaces, making the swap mechanical.

## Decided Since Phase 1 Planning

1. **Orchestrator datastore**: the orchestrator gets its own Redis instance. All tx state moves there. This is decided — not deferred.
2. **Guard ownership**: the orchestrator owns both acquisition and clearing of the active-tx guard. This is decided.
3. **`tx_decision` endpoint**: moves to the orchestrator. Participants point at the orchestrator URL. This is decided.
4. **Checkout routing**: the order service proxies `POST /orders/checkout/{order_id}` to the orchestrator via internal HTTP. The orchestrator is not publicly accessible. This is decided.
5. **Already-paid fast path**: lives in the orchestrator (reads order via `OrderPort`, returns 200 if paid). The orchestrator owns the full checkout lifecycle, including the no-op path. This is decided.

## Remaining Deferred Decisions For Phase 2

These are valid future questions that do not need to be answered until Phase 2 implementation.

1. Whether SAGA and 2PC should remain startup-selected or become a richer orchestrator configuration concern
2. Whether the orchestrator should expose internal status/debug endpoints beyond `tx_decision`

Deferring these choices is intentional. They should not distort the Phase 1 rebuild.

## Phase 2 Risks To Watch For

1. Re-introducing transport concerns into protocol code during extraction (bypassing ports "just for this one case")
2. Letting `order` keep partial orchestration logic "for convenience" (e.g., guard management, recovery fragments, fast-path checks)
3. Splitting transaction ownership ambiguously between `order` and `orchestrator` — all tx state must live in orchestrator Redis, no hybrid
4. Rewriting participant Lua scripts or message handlers mid-extraction instead of preserving stable contracts
5. Proxy route in order service must be truly stateless — no guard management, no order validation beyond forwarding, no result interpretation beyond passing through the response

## Acceptance Criteria For Phase 2 Readiness

The Phase 1 rebuild can be considered genuinely Phase-2-ready only if all of the following are true.

1. The coordinator can be moved into the orchestrator service by swapping `OrderPort` and `TxStorePort` implementations alone. If any protocol or recovery code must be modified, the boundary is too coupled.
2. Protocol code depends only on port interfaces and `common/` — no Flask, no direct Redis, no order-specific imports.
3. Recovery logic moves with the coordinator without reimplementation.
4. The public API remains unchanged — same routes, same response shapes. The nginx config is unchanged; the order service proxies checkout internally.
5. The participant RabbitMQ message contract (`common/models.py`) is unchanged — participants need zero code changes.
6. The same consistency invariants remain explainable before and after extraction.
7. The only new code needed in the order service is two internal endpoints (`read_order`, `mark_paid`) and a thin proxy route replacing the in-process coordinator call.

## Relationship To The Phase 1 Plan

This document is subordinate to [plan/phase1-rebuild-plan.md](C:/Users/Theo/source/repos/dds26-1/plan/phase1-rebuild-plan.md).

Interpretation rule:

1. if a Phase 2 idea conflicts with Phase 1 correctness or delivery, Phase 1 wins now
2. if a Phase 1 implementation choice makes Phase 2 extraction obviously harder, that choice should be reconsidered before coding further
