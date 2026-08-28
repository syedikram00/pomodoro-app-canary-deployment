
import os
import random
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import FastAPI, HTTPException, Request, Response
from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST
from pydantic import BaseModel, Field

APP_LABEL = os.environ.get("APP_LABEL", "myapp")
FAIL_MODE = os.environ.get("FAIL_MODE", "false").lower() == "true"
FAIL_RATE = float(os.environ.get("FAIL_RATE", "0.3"))

app = FastAPI(title="Pomodoro Timer (canary demo app)")

# --- Prometheus metrics -----------------------------------------------------
# Labeled with `app` so the same metric name works for both the stable and
# canary Deployments -- the controller's PromQL query filters on this label.

REQUEST_COUNT = Counter(
    "http_requests_total",
    "Total HTTP requests",
    labelnames=["app", "method", "path", "status"],
)

REQUEST_LATENCY = Histogram(
    "http_request_duration_seconds",
    "HTTP request latency in seconds",
    labelnames=["app", "method", "path"],
)


@app.middleware("http")
async def metrics_and_failure_injection_middleware(request: Request, call_next):
  
    if request.url.path == "/metrics":
        return await call_next(request)

    start_time = time.perf_counter()

    if FAIL_MODE and random.random() < FAIL_RATE:
        duration = time.perf_counter() - start_time
        REQUEST_LATENCY.labels(APP_LABEL, request.method, request.url.path).observe(duration)
        REQUEST_COUNT.labels(APP_LABEL, request.method, request.url.path, "500").inc()
        return Response(content='{"detail":"injected failure (FAIL_MODE)"}', status_code=500, media_type="application/json")

    response = await call_next(request)

    duration = time.perf_counter() - start_time
    REQUEST_LATENCY.labels(APP_LABEL, request.method, request.url.path).observe(duration)
    REQUEST_COUNT.labels(APP_LABEL, request.method, request.url.path, str(response.status_code)).inc()

    return response


@app.get("/metrics")
def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


# --- Pomodoro timer logic (in-memory, single global session) ---------------

class SessionState(BaseModel):
    running: bool = False
    started_at: Optional[datetime] = None
    duration_minutes: Optional[int] = None


_session = SessionState()


class StartRequest(BaseModel):
    duration_minutes: int = Field(default=25, gt=0, le=180)


@app.post("/start")
def start_session(req: StartRequest):
    _session.running = True
    _session.started_at = datetime.now(timezone.utc)
    _session.duration_minutes = req.duration_minutes
    return {
        "message": "Pomodoro session started",
        "duration_minutes": req.duration_minutes,
        "started_at": _session.started_at.isoformat(),
    }


@app.get("/status")
def get_status():
    if not _session.running or _session.started_at is None or _session.duration_minutes is None:
        return {"running": False}

    end_time = _session.started_at + timedelta(minutes=_session.duration_minutes)
    now = datetime.now(timezone.utc)
    remaining = (end_time - now).total_seconds()

    if remaining <= 0:
        _session.running = False
        return {"running": False, "message": "Session finished"}

    return {
        "running": True,
        "duration_minutes": _session.duration_minutes,
        "remaining_seconds": int(remaining),
    }


@app.get("/healthz")
def healthz():
   
    return {"status": "ok"}