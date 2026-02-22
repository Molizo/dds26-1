# Repository Guidelines

## Project Structure & Module Organization
This repository contains three Python microservices: `order/`, `payment/`, and `stock/`. Each service has `app.py`, `Dockerfile`, and `requirements.txt`. Shared environment settings live in `env/*.env`. Integration tests are in `test/` (`test_microservices.py`, `utils.py`). Deployment assets are:
- `docker-compose.yml`
- `k8s/*.yaml`
- `helm-config/*.yaml` and `deploy-charts-*.sh`

## Build, Test, and Development Commands
- `python -m venv .venv; .\.venv\Scripts\Activate.ps1; pip install -r requirements.txt`  
  Create a local virtual environment and install dependencies.
- `docker compose up --build`  
  Build and run gateway and Redis.
- `docker compose down -v`  
  Stop containers and remove volumes.
- `python -m unittest discover -s test -p "test_*.py"`  
  Run integration tests (stack up on `http://127.0.0.1:8000`).
- `kubectl apply -f k8s`  
  Apply Kubernetes manifests.

## Coding Style & Naming Conventions
Use PEP 8: 4-space indentation, `snake_case` for functions/variables, and `PascalCase` for typed models. Keep constants uppercase. Preserve route prefixes (`/stock`, `/payment`, `/orders`) because tests and gateway routing depend on them.

## Assignment Alignment (Required)
Treat `assignment.md` as a source of truth for functional scope and grading priorities. When contributing:
- keep the external API stable (paths and semantics)
- keep success/failure responses in `2xx`/`4xx` ranges as specified
- prioritize consistency and fault handling in checkout/payment/stock flows
- do not merge conflicting deliverable changes without team agreement
- backward compatibility and data migrations are not required; the system is run from scratch on an empty DB

## Planning & Design-Limitation Log
When a `plan/` folder exists, use it as context. Keep a design-limitation issue log in `plan/` (for example `plan/design-limitations-log.md`) that records:
- limitation encountered
- why the current design caused it
- impact (consistency, performance, reliability, or delivery)
- chosen mitigation or follow-up action
Use this log to avoid repeated mistakes. Recommend walking back prior design decisions when better alternatives exist.

## Testing Guidelines
Tests use `unittest` and run end-to-end through the gateway. Name files `test_*.py` and methods `test_*`. Keep tests isolated by creating fresh users/items/orders per case, and extend `test/utils.py` for new endpoint helpers.

## Commit & Pull Request Guidelines
Current history uses short, imperative commit subjects (for example “Initial commit”). Follow that style and keep commits scoped to one concern. PRs should include:
- a brief summary of behavior changes
- linked issue/task (if available)
- test evidence (commands run and pass/fail result)
- notes for config/deployment changes (`docker-compose`, `k8s`, or Helm values)
- updates to top-level `contributions.txt` when team contributions change
