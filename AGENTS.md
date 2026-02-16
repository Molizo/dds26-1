# Repository Guidelines

## Assignment-First Principles
- Treat `Group Projects DDS.md` as the source of truth for design and evaluation criteria.
- Do not change externally visible API routes, request/response fields, or 2xx/4xx behavior expected by the benchmark.
- Prioritize consistency first (no money/stock loss), then performance and elasticity.
- Aim for the most fault-tolerant and scalable design that is feasible for the project timeline; avoid unnecessary architectural complexity.

## Project Structure & Module Organization
- `order/`, `payment/`, `stock/`: Flask microservices (`app.py`), per-service `Dockerfile`, and `requirements.txt`.
- `test/`: end-to-end API checks (`test_microservices.py`) plus HTTP helpers (`utils.py`).
- `env/`: Redis connection env files for Docker Compose.
- `k8s/`: Kubernetes manifests for services/deployments and ingress.
- `helm-config/`: Helm values for Redis and ingress-nginx.
- Root files: `docker-compose.yml`, `gateway_nginx.conf`, deployment helper scripts, and course docs.

## Build, Test, and Development Commands
- Install local Python deps:
```bash
pip install -r requirements.txt
```
- Run stack locally (gateway + services + Redis instances):
```bash
docker-compose up --build
```
- Run tests (with stack running):
```bash
python -m unittest discover -s test -p "test_*.py"
```
- Deploy Redis + ingress charts:
```bash
bash deploy-charts-minikube.sh
bash deploy-charts-cluster.sh
```
- Apply Kubernetes manifests:
```bash
kubectl apply -f k8s/
```

## Coding Style & Naming Conventions
- Python: 4-space indentation, `snake_case` for functions/variables, `UpperCamelCase` for `Struct` types.
- Keep service logic in `*/app.py`; prefer small helper functions for DB and HTTP calls.
- Preserve the external API paths and response semantics required by the assignment (2xx success, 4xx failure).
- Route names may include mixed case when required by the given API (for example, `/orders/addItem/...`).

## Testing Guidelines
- Framework: standard `unittest` with real HTTP calls through `http://127.0.0.1:8000`.
- Add tests under `test/` named `test_*.py`; keep helpers in `test/utils.py`.
- For transaction changes, include failure-path tests (out-of-stock, insufficient credit, retry/idempotency behavior).

## Commit & Pull Request Guidelines
- Use short imperative commit subjects (seen in history: “Initial commit”, “Updated gitignore and added project description”).
- Keep subject lines specific and under ~72 characters.
- PR checklist:
1. What changed and why.
2. Consistency/fault-tolerance impact.
3. How to run and results of relevant tests.
4. Any config or deployment manifest changes (`docker-compose`, `k8s`, `helm-config`).

## Plan Maintenance Rules
- Treat `plans/Final_Redis_RabbitMQ_Implementation_Plan.md` as a living plan.
- Update the plan immediately when one of these happens:
1. A phase is blocked by an unexpected issue that changes scope, order, or feasibility.
2. Test/load/failure evidence contradicts a core assumption.
3. A better approach is chosen for performance or fault tolerance.
- When updating the plan:
1. Add a dated entry to the plan's change log with the issue, decision, and rationale.
2. Update impacted phase steps, exit criteria, and test scenarios in the same commit.
3. Preserve hard constraints unless explicitly re-approved:
   - external API contract,
   - 2xx/4xx semantics,
   - assignment alignment,
   - agreed environment assumptions.
4. If design opinion changes, document what changed, why it changed, and what risk is introduced/removed.
- Prefer the smallest safe next-step replan instead of broad rewrites.

## Security & Configuration Tips
- Do not commit real credentials; current Redis password values are dev defaults.
- Keep environment-specific overrides in env files or cluster secrets, not hardcoded in app logic.
