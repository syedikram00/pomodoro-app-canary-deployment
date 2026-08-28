# Canary Deployment with Automated Rollback

A Kubernetes-native canary deployment system that automatically detects a bad release and rolls it back — no human watching a dashboard, no manual `kubectl scale`. A lightweight Python controller continuously polls Prometheus for the canary's error rate and scales it to zero the moment it breaches a configured threshold.

Built to explore the mechanics behind tools like Argo Rollouts and Flagger, hand-rolled from first principles rather than adopted off the shelf — so every piece of the decision logic is something I designed and can explain, not configuration for someone else's controller.

## Why this project

Every real deployment eventually ships something broken. The question isn't whether that happens, it's how fast it's caught and how much damage it does before that happens. Canary releases (exposing a new version to a small slice of traffic before a full rollout) combined with automated rollback (reacting to bad metrics without waiting for a human to notice) is standard practice at companies running Kubernetes at any real scale — this project builds a minimal, understandable version of that pattern.

## Architecture

```
                    ┌─────────────────┐
   Traffic ────────▶│  NGINX Ingress   │
                    └────────┬─────────┘
                    90% ──────┼────── 10% (canary weight)
                        ▼            ▼
              ┌──────────────┐  ┌──────────────┐
              │ Stable Svc   │  │ Canary Svc   │
              │ (2 replicas) │  │ (2 replicas) │
              └──────────────┘  └──────────────┘
                        │            │
                        └─────┬──────┘
                               ▼
                    Prometheus (scrapes /metrics
                    from both, labeled by app)
                               │
                               ▼
                 Canary Controller (polls every 30s)
                    - runs PromQL error-rate query
                    - compares against threshold
                    - on breach: patches canary
                      Deployment to 0 replicas
```

**Both stable and canary run the exact same container image.** They're differentiated entirely by environment variables (`APP_LABEL`, `FAIL_MODE`, `FAIL_RATE`) — no separate builds for "the good version" and "the bad version." This mirrors how real canary releases usually work: a new *version* of an app, not a manually-broken copy of it.

## Components

- **Pomodoro timer app** (FastAPI) — deliberately minimal business logic. Two endpoints (`/start`, `/status`), in-memory session state. The app itself isn't the point; it's a controllable target for testing the deployment pipeline.
- **NGINX Ingress** — two `Ingress` objects (stable + canary), with the canary object carrying `nginx.ingress.kubernetes.io/canary` and `canary-weight` annotations to split traffic.
- **Prometheus** (`kube-prometheus-stack` via Helm) — scrapes both services' `/metrics` endpoints via a `ServiceMonitor`, storing `http_requests_total` and `http_request_duration_seconds` labeled by `app` (`myapp-stable` / `myapp-canary`), `method`, `path`, and `status`.
- **Canary controller** (Python) — a standalone script, containerized and run as its own single-replica Deployment with a dedicated `ServiceAccount`. Polls Prometheus on a fixed interval and executes the rollback directly against the Kubernetes API.

## The core decision: what to measure

Of the four "golden signals" (latency, traffic, errors, saturation), I chose to build around **error rate** first. Saturation is an infrastructure-pressure signal (CPU/memory), not something that reliably indicates "we shipped bad code" — it was deprioritized deliberately, not overlooked. Latency is a natural second signal (planned as a future addition, see below), but error rate is the most direct proxy for "this version is broken" and the simplest place to prove the mechanism works end-to-end.

The controller's PromQL query:

```promql
(
  sum(rate(http_requests_total{app="myapp-canary", status=~"5.."}[5m])) or vector(0)
)
/
sum(rate(http_requests_total{app="myapp-canary"}[5m]))
```

This computes the ratio of 5xx responses to total responses for the canary, as a rate over a 5-minute window (not a raw counter — raw counts are meaningless without knowing traffic volume).

## A real bug I hit, and what it taught me

The first version of this query didn't have the `or vector(0)` clause. With `FAIL_MODE` off, there are zero 5xx responses, which means the numerator's label filter (`status=~"5.."`) matches *no time series at all* — not a series with value `0`, but no series. PromQL's division operator does vector matching: if one side of a division is completely empty, the *entire expression* returns empty, even if the other side has real data.

The symptom was confusing: Prometheus's Targets page showed everything `UP`, manual queries for the raw metric returned real data with correct labels, but the controller's own query kept logging "no data" forever. The fix was forcing the numerator to fall back to a literal `0` when its filter matches nothing:

```promql
(sum(rate(...)) or vector(0)) / sum(rate(...))
```

This is a real PromQL gotcha, not specific to this project — it's the kind of thing that bites people building their first alerting rules in production.

## Verification — Phase 1 (baseline, no failure)

With both stable and canary running healthy (`FAIL_MODE=false` on both), traffic was sent through the Ingress and counted directly from pod logs:

| | Requests | Share |
|---|---|---|
| Stable (2 pods) | ~1,807 | ~89.1% |
| Canary (2 pods) | ~220 | ~10.9% |

Configured canary weight was 10% — the observed split lines up closely, confirming NGINX's canary routing was working correctly before any failure-detection logic was tested.

## Verification — Phase 2 (rollback under failure)

**Test 1 — unambiguous failure (`FAIL_RATE=1.0`, `ERROR_THRESHOLD=1.0`):** with the canary set to fail 100% of requests, the controller detected the breach and scaled the canary to zero on its very next poll cycle. This first test intentionally used the least ambiguous possible signal — the goal was to prove the full chain (traffic → metrics → query → threshold check → RBAC-authorized scale action) worked before testing anything more realistic.

**Test 2 — realistic partial failure (`FAIL_RATE=0.3`, `ERROR_THRESHOLD=0.10`):** with the canary failing 30% of its own requests, the controller correctly measured an error rate in that range and triggered rollback once it crossed the 10% threshold:

```
Current canary error rate: 19.16% (threshold: 10.00%)
Error threshold breached -- scaling canary deployment 'canary-app-deployment' to 0 replicas
Rollback complete: canary-app-deployment scaled to 0 replicas
```

Both tests confirmed the controller's RBAC (a dedicated `ServiceAccount` with a namespace-scoped `Role` granting only `get`/`patch` on `deployments/scale` — not a `ClusterRole`, and not broader permissions than the one action it performs) was correctly configured on the first real attempt.

## Design decisions worth explaining

- **Ingress-based traffic splitting over a service mesh.** A service mesh (Istio/Linkerd) offers richer traffic control, but is significant operational overhead this project didn't need — NGINX's canary annotations achieve the same outcome for a straightforward percentage-based split.
- **Scale-to-zero over deleting the canary Ingress.** Scaling to zero is reversible, visible via `kubectl get deployments`, and doesn't touch networking config — rollback and traffic routing stay as separate concerns.
- **Single-shot controller, not continuous reconciliation.** The controller exits after triggering one rollback, rather than running a full state machine with re-promotion logic. This was a deliberate scope boundary for a demo project — see Future Improvements.
- **Hand-rolled controller instead of adopting Argo Rollouts/Flagger.** The point of this project was understanding the mechanism, not shipping a production tool — in a real team setting, adopting the mature, battle-tested tool would be the right call over maintaining a custom one.

## Stack

Kubernetes (`kind`), Helm, NGINX Ingress, Prometheus (`kube-prometheus-stack`), Python (FastAPI for the app, `requests` + the official `kubernetes` client for the controller), Docker.

## Future improvements

- **A second, more realistic failure scenario** using a genuine planted logic bug in the application code (an edge case that raises an unhandled exception under specific input) instead of the `FAIL_MODE` environment-variable toggle used for testing here — to validate detection against a bug shape closer to what actually ships in production.
- **Latency as a second analysis signal**, alongside error rate, using `http_request_duration_seconds`.
- **Re-promotion / reset logic** — currently, after a rollback, the canary Deployment has to be manually scaled back up for the next test. A production version would need a full state machine (promote on success, not just rollback on failure).
- **Hand-scoped RBAC audit** and moving from a `Deployment` to a `Job` for the controller, since it's designed to run once and exit rather than be continuously restarted.
- **Gradual weight increase** (5% → 25% → 50% → 100%) on success, rather than a single fixed 10% split throughout.