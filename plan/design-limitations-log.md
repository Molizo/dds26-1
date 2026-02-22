# Design Limitations Log

## 2026-02-22 - Synchronous checkout vs failure recovery latency

- Limitation encountered:
  - Synchronous checkout semantics conflict with slow dependency recovery during container failures.

- Why current design causes it:
  - Request path is required to return final `2xx/4xx`, but crashed participants may not recover within user-facing timeout budgets.

- Impact:
  - Reliability and delivery: long waits can trigger timeouts; immediate failure without persisted recovery risks inconsistent partial states.

- Chosen mitigation/follow-up:
  - Block/retry inline for up to 30 seconds.
  - If still unresolved, return `4xx`.
  - Persist non-terminal tx state and continue background recovery to terminal consistency.
  - Re-evaluate timeout after benchmark/fault-injection evidence.

## 2026-02-22 - App-level 2PC on Redis (no XA)

- Limitation encountered:
  - Native XA/true distributed transaction support is unavailable in current Redis-based architecture.

- Why current design causes it:
  - Services use independent Redis databases with application-managed coordination.

- Impact:
  - Consistency and reliability: in-doubt transactions are possible on coordinator/participant crashes.

- Chosen mitigation/follow-up:
  - Implement durable app-level `prepare/commit/abort`.
  - Use presumed-abort recovery policy.
  - Enforce participant idempotency keyed by `tx_id`.
  - Add startup and periodic recovery scans to resolve non-terminal transactions.

## 2026-02-22 - Redis database outage during in-flight checkout

- Limitation encountered:
  - A participant or coordinator database can fail in the middle of checkout, leaving a transaction step outcome unknown to callers.

- Why current design causes it:
  - Services use independent Redis instances and distributed coordination over network calls; partial progress can be durable in one place while another write fails.

- Impact:
  - Consistency and reliability: ambiguous request outcomes, in-doubt prepared branches, and stranded non-terminal transactions if recovery is not explicit.

- Chosen mitigation/follow-up:
  - Treat DB failure as unknown or incomplete, never implicit success.
  - Bound inline retries to 30 seconds, then return `4xx` and continue background recovery.
  - Require participant durable write before acknowledging `prepare`, `commit`, or `abort`.
  - Run startup plus periodic transaction recovery and reconciliation after DB restart.
  - Add fault tests that kill `order-db`, `payment-db`, and `stock-db` during critical protocol windows.
