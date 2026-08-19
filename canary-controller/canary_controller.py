"""
Canary Rollback Controller

Continuously polls Prometheus for the canary deployment's error rate.
If the error rate breaches the configured threshold over the configured
window, scales the canary Deployment to 0 replicas (effectively pulling
it out of rotation) and stops.
"""

import logging
import os
import sys
import time

import requests
from kubernetes import client, config
from kubernetes.client.rest import ApiException

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("canary-controller")


def load_config():
    return {
        "prometheus_url": os.environ.get("PROMETHEUS_URL", "http://localhost:9090"),
        "namespace": os.environ.get("NAMESPACE", "default"),
        "canary_deployment": os.environ.get("CANARY_DEPLOYMENT", "myapp-canary"),
        "canary_app_label": os.environ.get("CANARY_APP_LABEL", "myapp-canary"),
        "error_threshold": float(os.environ.get("ERROR_THRESHOLD", "1.0")),
        "window": os.environ.get("WINDOW", "5m"),
        "poll_interval_seconds": int(os.environ.get("POLL_INTERVAL_SECONDS", "30")),
    }


def build_error_rate_query(app_label: str, window: str) -> str:
    """
    error_rate = failed_requests / total_requests, both as rates over `window`.

    sum(rate(http_requests_total{app="<app_label>", status=~"5.."}[<window>]))
    /
    sum(rate(http_requests_total{app="<app_label>"}[<window>]))
    """
    failed = f'sum(rate(http_requests_total{{app="{app_label}", status=~"5.."}}[{window}]))'
    total = f'sum(rate(http_requests_total{{app="{app_label}"}}[{window}]))'
    return f"({failed}) / ({total})"


def query_prometheus(prometheus_url: str, promql: str) -> float | None:
    """
    Runs an instant query against Prometheus. Returns the scalar result,
    or None if there's no data yet (e.g. zero traffic in the window --
    division by zero in PromQL just returns no result, not an error).
    """
    url = f"{prometheus_url}/api/v1/query"
    try:
        resp = requests.get(url, params={"query": promql}, timeout=10)
        resp.raise_for_status()
    except requests.RequestException as e:
        log.error("Prometheus query failed: %s", e)
        return None

    data = resp.json()
    if data.get("status") != "success":
        log.error("Prometheus returned non-success status: %s", data)
        return None

    result = data["data"]["result"]
    if not result:
        # No data -- most likely zero traffic to the canary in this window.
        return None

    # Instant query on a scalar expression returns a single series
    # with value = [timestamp, "string_value"]
    value_str = result[0]["value"][1]
    try:
        return float(value_str)
    except (TypeError, ValueError):
        log.error("Could not parse Prometheus value: %r", value_str)
        return None


def scale_canary_to_zero(apps_v1: client.AppsV1Api, namespace: str, deployment_name: str):
    log.warning(
        "Error threshold breached -- scaling canary deployment '%s' in namespace '%s' to 0 replicas",
        deployment_name,
        namespace,
    )
    patch_body = {"spec": {"replicas": 0}}
    try:
        apps_v1.patch_namespaced_deployment_scale(
            name=deployment_name,
            namespace=namespace,
            body=patch_body,
        )
        log.info("Rollback complete: %s scaled to 0 replicas", deployment_name)
    except ApiException as e:
        log.error("Failed to scale down canary deployment: %s", e)
        raise


def load_kube_client() -> client.AppsV1Api:
    """
    Tries in-cluster config first (when running as a Pod), falls back to
    local kubeconfig (when running on your machine against a real cluster).
    """
    try:
        config.load_incluster_config()
        log.info("Loaded in-cluster Kubernetes config")
    except config.ConfigException:
        config.load_kube_config()
        log.info("Loaded local kubeconfig")
    return client.AppsV1Api()


def main():
    cfg = load_config()
    log.info("Canary controller starting with config: %s", cfg)

    promql = build_error_rate_query(cfg["canary_app_label"], cfg["window"])
    log.info("Using PromQL query: %s", promql)

    apps_v1 = load_kube_client()

    while True:
        error_rate = query_prometheus(cfg["prometheus_url"], promql)

        if error_rate is None:
            log.info("No data yet for canary (likely zero traffic in window) -- skipping this check")
        else:
            log.info(
                "Current canary error rate: %.2f%% (threshold: %.2f%%)",
                error_rate * 100,
                cfg["error_threshold"] * 100,
            )
            if error_rate >= cfg["error_threshold"]:
                scale_canary_to_zero(apps_v1, cfg["namespace"], cfg["canary_deployment"])
                log.info("Rollback triggered -- controller exiting")
                sys.exit(0)

        time.sleep(cfg["poll_interval_seconds"])


if __name__ == "__main__":
    main()