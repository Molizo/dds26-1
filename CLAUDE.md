# CLAUDE.md — dds26-1

## Project Overview
Distributed Data Systems course project: three Python/Flask microservices (`order`, `payment`, `stock`) backed by Redis, implementing checkout consistency via SAGA and 2PC.

- **Phase 1 deadline**: 13 March — embed coordinator in `order` service, fault-tolerant under single-container failure
- **Phase 2 deadline**: 1 April — extract coordinator into standalone orchestrator

Current work is on branch `phase-1-attempt-3`.

## Project Structure
```
order/app.py          Flask app, Redis client, checkout coordinator
payment/app.py        Flask app, Redis client
stock/app.py          Flask app, Redis client
env/*.env             Redis env vars for docker-compose
docker-compose.yml    Local dev (builds + runs gateway + Redis)
k8s/*.yaml            Kubernetes manifests
helm-config/          Helm values for Redis + ingress-nginx
test/                 unittest end-to-end tests (run via gateway)
plan/                 Design docs and limitation log
```

## Key Conventions
- **Language**: Python, Flask (or compatible async variant), Redis
- **Style**: PEP 8, 4-space indent, `snake_case` functions/vars, `PascalCase` typed models, UPPERCASE constants
- **Serialization**: `msgspec` (msgpack) for Redis values
- **Route prefixes**: `/orders`, `/stock`, `/payment` — never change these (gateway routing + tests depend on them)
- **Status codes**: `2xx` success, `4xx` failure — no exceptions

## External API (must not change)
See `assignment.md` for the full spec. Key routes:
- `POST /orders/create/{user_id}` → `{"order_id": ...}`
- `GET /orders/find/{order_id}` → `{order_id, paid, items, user_id, total_cost}`
- `POST /orders/addItem/{order_id}/{item_id}/{quantity}`
- `POST /orders/checkout/{order_id}`
- `GET /stock/find/{item_id}` → `{stock, price}`
- `POST /stock/subtract/{item_id}/{amount}`
- `POST /stock/add/{item_id}/{amount}`
- `POST /stock/item/create/{price}` → `{"item_id": ...}`
- `POST /payment/pay/{user_id}/{amount}`
- `POST /payment/add_funds/{user_id}/{amount}` → `{"done": true/false}`
- `POST /payment/create_user` → `{"user_id": ...}`
- `GET /payment/find_user/{user_id}` → `{user_id, credit}`

## Development Commands
```bash
# Local venv
python -m venv .venv && .\.venv\Scripts\Activate.ps1 && pip install -r requirements.txt

# Run stack (gateway + Redis)
docker compose up --build

# Stop and clean volumes
docker compose down -v

# Run tests (stack must be up at http://127.0.0.1:8000)
python -m unittest discover -s test -p "test_*.py"

# Kubernetes
kubectl apply -f k8s
```

## Design Constraints (from plan/)
- Coordinator lives inside `order` service for Phase 1 — kept as a separate code boundary for easy Phase 2 extraction
- Both SAGA and 2PC must exist in the codebase
- Recovery must be explicit and durable (logged to Redis)
- System starts from a clean DB — no backward-compat migrations needed
- See `plan/design-limitations-log.md` for known issues and mitigations

## Testing
- Tests in `test/test_microservices.py` use `unittest`, run end-to-end through gateway
- Create fresh users/items/orders per test case — no shared state
- Add endpoint helpers to `test/utils.py`

## Commit Style
Short imperative subjects scoped to one concern, e.g.:
```
Add SAGA rollback for stock subtraction
```
PRs include: behavior summary, test evidence, config/deployment notes.
