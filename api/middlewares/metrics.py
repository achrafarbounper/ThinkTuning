# project/api/middlewares/metrics.py

import logging
import time

from fastapi import Request
from prometheus_client import Counter, Histogram

logger = logging.getLogger("thinktuning.api")
logger.setLevel(logging.INFO)

REQUEST_COUNTER = Counter(
    "http_requests_total",
    "Total number of HTTP requests processed by the API.",
    labelnames=("method", "path", "status_code"),
)
REQUEST_LATENCY = Histogram(
    "http_request_duration_seconds",
    "Latency of HTTP requests in seconds.",
    labelnames=("method", "path", "status_code"),
)


async def request_metrics_middleware(request: Request, call_next):
    start_time = time.perf_counter()
    route = request.scope.get("route")
    request_path = getattr(route, "path", request.url.path)
    client_ip = request.client.host if request.client else "unknown"

    try:
        response = await call_next(request)
    except Exception:
        duration = time.perf_counter() - start_time
        status_code = 500
        REQUEST_COUNTER.labels(method=request.method, path=request_path, status_code=str(status_code)).inc()
        REQUEST_LATENCY.labels(method=request.method, path=request_path, status_code=str(status_code)).observe(duration)
        logger.exception(
            "http_request method=%s path=%s status=%s duration_ms=%.3f client_ip=%s",
            request.method,
            request_path,
            status_code,
            duration * 1000,
            client_ip,
        )
        raise

    duration = time.perf_counter() - start_time
    status_code = response.status_code
    REQUEST_COUNTER.labels(method=request.method, path=request_path, status_code=str(status_code)).inc()
    REQUEST_LATENCY.labels(method=request.method, path=request_path, status_code=str(status_code)).observe(duration)
    logger.info(
        "http_request method=%s path=%s status=%s duration_ms=%.3f client_ip=%s",
        request.method,
        request_path,
        status_code,
        duration * 1000,
        client_ip,
    )
    return response
