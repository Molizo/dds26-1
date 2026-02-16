# Scaling Plan for 50k Concurrent Requests

To reach 50k concurrent requests under the assignment constraints (keep API, guarantee consistency, handle failures, bonus for event-driven in `Group Projects DDS.md:17`, `Group Projects DDS.md:93`, `Group Projects DDS.md:105`, `Group Projects DDS.md:113`), expand this project in 4 layers:

1. Fix correctness bottlenecks before scaling
- Make stock/payment updates atomic (current read-modify-write races in `stock/app.py:98` and `payment/app.py:95`).
- Make checkout idempotent (current flow can double-charge/double-subtract on retries in `order/app.py:150`).
- Add request timeouts/retries with backoff for inter-service calls (currently no timeout in `order/app.py:110`).

2. Replace synchronous checkout with event-driven Saga
- Keep `POST /orders/checkout/{order_id}` externally unchanged.
- Internally: write `PENDING` transaction state, publish `CheckoutRequested` event, process via worker(s), then set final `SUCCESS/FAILED`.
- Use compensation events (`StockReserved -> PaymentFailed -> StockRelease`) and durable logs (outbox/inbox) for crash recovery.
- This directly matches the assignment's consistency and fault-tolerance goals.

3. Scale runtime and infrastructure for high concurrency
- Stop routing internal calls through gateway (`order/app.py:18`); use service-to-service DNS or events.
- Move to async serving (`Quart/FastAPI` or Gunicorn async worker class), because current K8s args default to single worker per pod (`k8s/order-app.yaml:39`, `k8s/stock-app.yaml:39`, `k8s/user-app.yaml:39`).
- Increase replicas and add autoscaling (currently `replicas: 1` in `k8s/order-app.yaml:19`, `k8s/stock-app.yaml:19`, `k8s/user-app.yaml:19`).
- Tune ingress/gateway for connection fan-in (current `worker_connections 2048` in `gateway_nginx.conf:1` is too low for 50k).

4. Data and recovery architecture
- Use separate Redis clusters (or at least separate instances) for order/payment/stock domains; avoid one shared hotspot.
- Use Redis Streams/Kafka for queueing + consumer groups + replay.
- Add a reconciliation worker that scans `PENDING` checkouts and completes/compensates after crashes (needed for container-kill tests in `Group Projects DDS.md:145`).

Minimal target flow:

```text
Client -> Ingress -> Order API -> CheckoutRequested (stream)
Worker consumes -> reserve stock (atomic) -> charge payment (atomic)
Success => mark order paid
Failure => compensate + mark failed
```

