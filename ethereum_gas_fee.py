#!/usr/bin/env python3
"""
Etherscan Gas Tracker (Production-grade v8)
===========================================

Features:
- Sync and async clients
- Retry with exponential jitter
- Circuit breaker: CLOSED / OPEN / HALF_OPEN
- Sync and async rate limiters
- Pydantic response validation
- Thread-safe metrics
- Graceful shutdown
- JSON / human-readable output
- Polling mode
- Optional proxy support
- Better API error classification

Install:
    pip install requests httpx pydantic tenacity
    # optional: pip install orjson

Usage:
    python etherscan_gas_tracker_improved.py --api-key YOUR_KEY
    python etherscan_gas_tracker_improved.py --api-key YOUR_KEY --json
    python etherscan_gas_tracker_improved.py --api-key YOUR_KEY --poll 10
    python etherscan_gas_tracker_improved.py --api-key YOUR_KEY --async-mode --poll 10
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import random
import signal
import sys
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, Optional

import httpx
import requests
from pydantic import BaseModel, Field, ValidationError

try:
    import orjson

    def json_dumps(data: Any) -> str:
        return orjson.dumps(data, option=orjson.OPT_INDENT_2).decode("utf-8")

except ImportError:

    def json_dumps(data: Any) -> str:
        return json.dumps(data, indent=2, ensure_ascii=False)


ETHERSCAN_API_URL = "https://api.etherscan.io/api"
MODULE = "gastracker"
ACTION = "gasoracle"

DEFAULT_CONNECT_TIMEOUT = 3.0
DEFAULT_READ_TIMEOUT = 10.0
DEFAULT_RETRIES = 5
DEFAULT_BACKOFF_BASE = 0.5
DEFAULT_BACKOFF_MAX = 10.0
DEFAULT_CIRCUIT_FAILURE_THRESHOLD = 5
DEFAULT_CIRCUIT_RECOVERY_TIMEOUT = 30.0
USER_AGENT = "EtherscanGasTracker/8.0"

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_INTERRUPT = 130


# =============================================================================
# Compatibility helpers for Pydantic v1/v2
# =============================================================================


def model_validate_compat(model: type[BaseModel], payload: Any) -> BaseModel:
    if hasattr(model, "model_validate"):
        return model.model_validate(payload)  # type: ignore[attr-defined]
    return model.parse_obj(payload)  # type: ignore[attr-defined]


def model_dump_compat(instance: BaseModel) -> Dict[str, Any]:
    if hasattr(instance, "model_dump"):
        return instance.model_dump()  # type: ignore[attr-defined]
    return instance.dict()  # type: ignore[attr-defined]


# =============================================================================
# Config
# =============================================================================


@dataclass(frozen=True)
class AppConfig:
    api_key: str
    retries: int = DEFAULT_RETRIES
    backoff_base: float = DEFAULT_BACKOFF_BASE
    backoff_max: float = DEFAULT_BACKOFF_MAX
    connect_timeout: float = DEFAULT_CONNECT_TIMEOUT
    read_timeout: float = DEFAULT_READ_TIMEOUT
    poll_interval: float = 0.0
    rate_limit_delay: float = 0.0
    circuit_failure_threshold: int = DEFAULT_CIRCUIT_FAILURE_THRESHOLD
    circuit_recovery_timeout: float = DEFAULT_CIRCUIT_RECOVERY_TIMEOUT
    verbose: bool = False
    json_output: bool = False
    async_mode: bool = False
    show_metrics: bool = False
    proxy: Optional[str] = None
    trust_env_proxy: bool = True

    def validate(self) -> None:
        if not self.api_key:
            raise ValueError("API key is empty")
        if self.retries < 1:
            raise ValueError("--retries must be >= 1")
        if self.backoff_base <= 0:
            raise ValueError("--backoff-base must be > 0")
        if self.backoff_max < self.backoff_base:
            raise ValueError("--backoff-max must be >= --backoff-base")
        if self.connect_timeout <= 0 or self.read_timeout <= 0:
            raise ValueError("timeouts must be > 0")
        if self.poll_interval < 0:
            raise ValueError("--poll must be >= 0")
        if self.rate_limit_delay < 0:
            raise ValueError("--rate-limit-delay must be >= 0")
        if self.circuit_failure_threshold < 1:
            raise ValueError("--circuit-failure-threshold must be >= 1")
        if self.circuit_recovery_timeout <= 0:
            raise ValueError("--circuit-recovery-timeout must be > 0")


# =============================================================================
# Logger
# =============================================================================


def configure_logger(verbose: bool, json_output: bool) -> logging.Logger:
    logger = logging.getLogger("etherscan_gas_tracker")
    logger.handlers.clear()
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)

    # Keep logs on stderr so --json stdout stays machine-readable.
    handler = logging.StreamHandler(sys.stderr)
    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(message)s",
        "%H:%M:%S",
    )
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    logger.propagate = False

    if json_output and not verbose:
        logger.setLevel(logging.WARNING)

    return logger


# =============================================================================
# Models
# =============================================================================


class GasPrices(BaseModel):
    safe_gwei: float = Field(..., ge=0)
    proposed_gwei: float = Field(..., ge=0)
    fast_gwei: float = Field(..., ge=0)
    base_fee_gwei: float = Field(..., ge=0)
    last_block: int = Field(..., ge=0)
    fetched_at: str


class EtherscanResponse(BaseModel):
    status: str
    message: str
    result: Any


# =============================================================================
# Metrics
# =============================================================================


class Metrics:
    def __init__(self) -> None:
        self.total_requests = 0
        self.successful_requests = 0
        self.failed_requests = 0
        self.total_latency_ms = 0.0
        self.last_latency_ms = 0.0
        self.last_error: Optional[str] = None
        self._lock = threading.Lock()

    def add_success(self, latency_ms: float) -> None:
        with self._lock:
            self.total_requests += 1
            self.successful_requests += 1
            self.total_latency_ms += latency_ms
            self.last_latency_ms = latency_ms
            self.last_error = None

    def add_failure(self, exc: BaseException) -> None:
        with self._lock:
            self.total_requests += 1
            self.failed_requests += 1
            self.last_error = f"{type(exc).__name__}: {exc}"

    @property
    def avg_latency_ms(self) -> float:
        with self._lock:
            if self.successful_requests == 0:
                return 0.0
            return self.total_latency_ms / self.successful_requests

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            avg = 0.0
            if self.successful_requests:
                avg = self.total_latency_ms / self.successful_requests
            return {
                "total_requests": self.total_requests,
                "successful_requests": self.successful_requests,
                "failed_requests": self.failed_requests,
                "avg_latency_ms": round(avg, 2),
                "last_latency_ms": round(self.last_latency_ms, 2),
                "last_error": self.last_error,
            }


# =============================================================================
# Circuit breaker
# =============================================================================


class CircuitState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    def __init__(self, failure_threshold: int, recovery_timeout: float) -> None:
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.failures = 0
        self.last_failure_time = 0.0
        self.state = CircuitState.CLOSED
        self._lock = threading.Lock()

    def allow_request(self) -> bool:
        with self._lock:
            if self.state == CircuitState.CLOSED:
                return True

            if self.state == CircuitState.OPEN:
                elapsed = time.monotonic() - self.last_failure_time
                if elapsed >= self.recovery_timeout:
                    self.state = CircuitState.HALF_OPEN
                    return True
                return False

            # HALF_OPEN allows one request attempt. In this single-request script
            # this is enough; for high concurrency you would add a probe lock.
            return True

    def record_success(self) -> None:
        with self._lock:
            self.failures = 0
            self.state = CircuitState.CLOSED

    def record_failure(self) -> None:
        with self._lock:
            self.failures += 1
            self.last_failure_time = time.monotonic()
            if self.failures >= self.failure_threshold:
                self.state = CircuitState.OPEN

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "state": self.state.value,
                "failures": self.failures,
                "failure_threshold": self.failure_threshold,
                "recovery_timeout_sec": self.recovery_timeout,
            }


# =============================================================================
# Rate limiters
# =============================================================================


class SyncRateLimiter:
    def __init__(self, delay: float) -> None:
        self.delay = delay
        self.last_call = 0.0
        self._lock = threading.Lock()

    def wait(self, shutdown_event: threading.Event) -> None:
        if self.delay <= 0:
            return

        with self._lock:
            elapsed = time.monotonic() - self.last_call
            wait_sec = max(0.0, self.delay - elapsed)
            if wait_sec > 0:
                shutdown_event.wait(wait_sec)
            self.last_call = time.monotonic()


class AsyncRateLimiter:
    def __init__(self, delay: float) -> None:
        self.delay = delay
        self.last_call = 0.0
        self._lock = asyncio.Lock()

    async def wait(self, shutdown_event: threading.Event) -> None:
        if self.delay <= 0:
            return

        async with self._lock:
            elapsed = time.monotonic() - self.last_call
            wait_sec = max(0.0, self.delay - elapsed)
            if wait_sec > 0:
                await async_sleep_interruptible(wait_sec, shutdown_event)
            self.last_call = time.monotonic()


# =============================================================================
# Exceptions
# =============================================================================


class APIError(Exception):
    pass


class RetryableAPIError(APIError):
    pass


class NonRetryableAPIError(APIError):
    pass


class CircuitBreakerOpen(Exception):
    pass


# =============================================================================
# Utils
# =============================================================================


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def resolve_api_key(cli_key: Optional[str]) -> str:
    key = cli_key or os.getenv("ETHERSCAN_API_KEY", "")
    key = key.strip()
    if not key:
        raise ValueError("Missing API key. Use --api-key or ETHERSCAN_API_KEY.")
    return key


def build_params(api_key: str) -> Dict[str, str]:
    return {
        "module": MODULE,
        "action": ACTION,
        "apikey": api_key,
    }


def classify_etherscan_error(message: str, result: Any) -> APIError:
    text = f"{message} {result}".lower()

    retry_markers = (
        "rate limit",
        "max rate limit",
        "busy",
        "timeout",
        "temporarily",
        "try again",
        "server too busy",
    )
    non_retry_markers = (
        "invalid api key",
        "missing api key",
        "invalid action",
        "invalid module",
    )

    if any(marker in text for marker in non_retry_markers):
        return NonRetryableAPIError(f"Etherscan API error: {message}; result={result}")
    if any(marker in text for marker in retry_markers):
        return RetryableAPIError(f"Etherscan temporary error: {message}; result={result}")
    return APIError(f"Etherscan API error: {message}; result={result}")


def parse_gas(payload: Dict[str, Any]) -> GasPrices:
    response = model_validate_compat(EtherscanResponse, payload)

    if response.status != "1":
        raise classify_etherscan_error(response.message, response.result)

    if not isinstance(response.result, dict):
        raise APIError(f"Unexpected result type: {type(response.result).__name__}")

    result = response.result

    try:
        gas = GasPrices(
            safe_gwei=float(result["SafeGasPrice"]),
            proposed_gwei=float(result["ProposeGasPrice"]),
            fast_gwei=float(result["FastGasPrice"]),
            base_fee_gwei=float(result["suggestBaseFee"]),
            last_block=int(result["LastBlock"]),
            fetched_at=utc_now_iso(),
        )
    except KeyError as exc:
        raise APIError(f"Missing Etherscan field: {exc}") from exc
    except (TypeError, ValueError) as exc:
        raise APIError(f"Invalid Etherscan gas payload: {result}") from exc

    return gas


def is_retryable_exception(exc: BaseException) -> bool:
    if isinstance(exc, NonRetryableAPIError):
        return False

    retryable_types = (
        RetryableAPIError,
        requests.Timeout,
        requests.ConnectionError,
        httpx.TimeoutException,
        httpx.NetworkError,
        httpx.RemoteProtocolError,
        httpx.PoolTimeout,
    )
    if isinstance(exc, retryable_types):
        return True

    if isinstance(exc, requests.HTTPError):
        status_code = getattr(exc.response, "status_code", None)
        return status_code == 429 or (status_code is not None and 500 <= status_code <= 599)

    if isinstance(exc, httpx.HTTPStatusError):
        status_code = exc.response.status_code
        return status_code == 429 or 500 <= status_code <= 599

    return False


def compute_backoff_delay(attempt_no: int, base: float, max_delay: float) -> float:
    # Exponential backoff with jitter. attempt_no starts from 1 after the first failed attempt.
    raw = min(max_delay, base * (2 ** max(0, attempt_no - 1)))
    return min(max_delay, raw * random.uniform(0.5, 1.5))


def sleep_interruptible(seconds: float, shutdown_event: threading.Event) -> None:
    shutdown_event.wait(max(0.0, seconds))


async def async_sleep_interruptible(seconds: float, shutdown_event: threading.Event) -> None:
    deadline = time.monotonic() + max(0.0, seconds)
    while not shutdown_event.is_set():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        await asyncio.sleep(min(remaining, 0.25))


# =============================================================================
# Base client
# =============================================================================


class BaseEtherscanClient:
    def __init__(self, config: AppConfig, logger: logging.Logger) -> None:
        self.config = config
        self.logger = logger
        self.metrics = Metrics()
        self.breaker = CircuitBreaker(
            failure_threshold=config.circuit_failure_threshold,
            recovery_timeout=config.circuit_recovery_timeout,
        )
        self.params = build_params(config.api_key)

    def metrics_snapshot(self) -> Dict[str, Any]:
        return {
            "metrics": self.metrics.snapshot(),
            "circuit_breaker": self.breaker.snapshot(),
        }


# =============================================================================
# Sync client
# =============================================================================


class SyncEtherscanClient(BaseEtherscanClient):
    def __init__(self, config: AppConfig, logger: logging.Logger, shutdown_event: threading.Event) -> None:
        super().__init__(config, logger)
        self.shutdown_event = shutdown_event
        self.rate_limiter = SyncRateLimiter(config.rate_limit_delay)
        self.session = requests.Session()
        self.session.trust_env = config.trust_env_proxy
        self.session.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Accept": "application/json",
                "Connection": "keep-alive",
            }
        )
        self.proxies = None
        if config.proxy:
            self.proxies = {"http": config.proxy, "https": config.proxy}

    def close(self) -> None:
        self.session.close()

    def fetch_gas_prices(self) -> GasPrices:
        last_exc: Optional[BaseException] = None

        for attempt_no in range(1, self.config.retries + 1):
            try:
                return self._fetch_once()
            except BaseException as exc:
                last_exc = exc
                self.metrics.add_failure(exc)
                self.breaker.record_failure()

                if attempt_no >= self.config.retries or not is_retryable_exception(exc):
                    raise

                delay = compute_backoff_delay(
                    attempt_no=attempt_no,
                    base=self.config.backoff_base,
                    max_delay=self.config.backoff_max,
                )
                self.logger.warning(
                    "retryable error on attempt %s/%s: %s; sleeping %.2fs",
                    attempt_no,
                    self.config.retries,
                    exc,
                    delay,
                )
                sleep_interruptible(delay, self.shutdown_event)

        raise RuntimeError(f"request failed: {last_exc}")

    def _fetch_once(self) -> GasPrices:
        if not self.breaker.allow_request():
            raise CircuitBreakerOpen("Circuit breaker is open")

        self.rate_limiter.wait(self.shutdown_event)
        if self.shutdown_event.is_set():
            raise KeyboardInterrupt

        request_id = str(uuid.uuid4())[:8]
        start = time.perf_counter()
        self.logger.debug("[%s] sync request started", request_id)

        response = self.session.get(
            ETHERSCAN_API_URL,
            params=self.params,
            timeout=(self.config.connect_timeout, self.config.read_timeout),
            proxies=self.proxies,
        )
        response.raise_for_status()

        latency_ms = (time.perf_counter() - start) * 1000
        payload = response.json()
        gas = parse_gas(payload)

        self.metrics.add_success(latency_ms)
        self.breaker.record_success()
        self.logger.debug("[%s] sync request finished in %.2f ms", request_id, latency_ms)
        return gas


# =============================================================================
# Async client
# =============================================================================


class AsyncEtherscanClient(BaseEtherscanClient):
    def __init__(self, config: AppConfig, logger: logging.Logger, shutdown_event: threading.Event) -> None:
        super().__init__(config, logger)
        self.shutdown_event = shutdown_event
        self.rate_limiter = AsyncRateLimiter(config.rate_limit_delay)

        timeout = httpx.Timeout(
            timeout=None,
            connect=config.connect_timeout,
            read=config.read_timeout,
            write=config.read_timeout,
            pool=config.connect_timeout,
        )

        self.client = httpx.AsyncClient(
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/json",
            },
            timeout=timeout,
            proxy=config.proxy,
            trust_env=config.trust_env_proxy,
        )

    async def close(self) -> None:
        await self.client.aclose()

    async def fetch_gas_prices(self) -> GasPrices:
        last_exc: Optional[BaseException] = None

        for attempt_no in range(1, self.config.retries + 1):
            try:
                return await self._fetch_once()
            except BaseException as exc:
                last_exc = exc
                self.metrics.add_failure(exc)
                self.breaker.record_failure()

                if attempt_no >= self.config.retries or not is_retryable_exception(exc):
                    raise

                delay = compute_backoff_delay(
                    attempt_no=attempt_no,
                    base=self.config.backoff_base,
                    max_delay=self.config.backoff_max,
                )
                self.logger.warning(
                    "retryable error on attempt %s/%s: %s; sleeping %.2fs",
                    attempt_no,
                    self.config.retries,
                    exc,
                    delay,
                )
                await async_sleep_interruptible(delay, self.shutdown_event)

        raise RuntimeError(f"request failed: {last_exc}")

    async def _fetch_once(self) -> GasPrices:
        if not self.breaker.allow_request():
            raise CircuitBreakerOpen("Circuit breaker is open")

        await self.rate_limiter.wait(self.shutdown_event)
        if self.shutdown_event.is_set():
            raise KeyboardInterrupt

        request_id = str(uuid.uuid4())[:8]
        start = time.perf_counter()
        self.logger.debug("[%s] async request started", request_id)

        response = await self.client.get(ETHERSCAN_API_URL, params=self.params)
        response.raise_for_status()

        latency_ms = (time.perf_counter() - start) * 1000
        payload = response.json()
        gas = parse_gas(payload)

        self.metrics.add_success(latency_ms)
        self.breaker.record_success()
        self.logger.debug("[%s] async request finished in %.2f ms", request_id, latency_ms)
        return gas


# =============================================================================
# Output
# =============================================================================


def render_output(data: GasPrices, json_output: bool, metrics: Optional[Dict[str, Any]] = None) -> None:
    payload = model_dump_compat(data)
    if metrics is not None:
        payload["runtime"] = metrics

    if json_output:
        print(json_dumps(payload), flush=True)
        return

    print("\nEthereum Gas Prices")
    print("-" * 44)
    print(f"Safe       : {data.safe_gwei:.2f} gwei")
    print(f"Proposed   : {data.proposed_gwei:.2f} gwei")
    print(f"Fast       : {data.fast_gwei:.2f} gwei")
    print(f"Base Fee   : {data.base_fee_gwei:.2f} gwei")
    print(f"Last Block : {data.last_block}")
    print(f"Fetched At : {data.fetched_at}")

    if metrics:
        m = metrics["metrics"]
        cb = metrics["circuit_breaker"]
        print("\nRuntime")
        print("-" * 44)
        print(f"Requests   : {m['total_requests']} total / {m['successful_requests']} ok / {m['failed_requests']} failed")
        print(f"Latency    : {m['last_latency_ms']:.2f} ms last / {m['avg_latency_ms']:.2f} ms avg")
        print(f"Circuit    : {cb['state']} / failures {cb['failures']}/{cb['failure_threshold']}")


# =============================================================================
# CLI
# =============================================================================


shutdown_event = threading.Event()


def signal_handler(signum: int, _frame: Any) -> None:
    shutdown_event.set()
    if signum == signal.SIGTERM:
        raise SystemExit(EXIT_INTERRUPT)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Fetch Ethereum gas prices from Etherscan gasoracle API."
    )

    parser.add_argument("--api-key", help="Etherscan API key. Can also use ETHERSCAN_API_KEY.")
    parser.add_argument("--json", action="store_true", help="Print JSON instead of human-readable output.")
    parser.add_argument("--verbose", action="store_true", help="Enable debug logs.")
    parser.add_argument("--async-mode", action="store_true", help="Use httpx.AsyncClient instead of requests.Session.")
    parser.add_argument("--poll", type=float, default=0.0, help="Polling interval in seconds. 0 = single run.")
    parser.add_argument("--rate-limit-delay", type=float, default=0.0, help="Minimum delay between API calls in seconds.")
    parser.add_argument("--retries", type=int, default=DEFAULT_RETRIES, help="Retry attempts for retryable errors.")
    parser.add_argument("--backoff-base", type=float, default=DEFAULT_BACKOFF_BASE, help="Initial retry backoff in seconds.")
    parser.add_argument("--backoff-max", type=float, default=DEFAULT_BACKOFF_MAX, help="Max retry backoff in seconds.")
    parser.add_argument("--connect-timeout", type=float, default=DEFAULT_CONNECT_TIMEOUT, help="Connect timeout in seconds.")
    parser.add_argument("--read-timeout", type=float, default=DEFAULT_READ_TIMEOUT, help="Read timeout in seconds.")
    parser.add_argument("--circuit-failure-threshold", type=int, default=DEFAULT_CIRCUIT_FAILURE_THRESHOLD)
    parser.add_argument("--circuit-recovery-timeout", type=float, default=DEFAULT_CIRCUIT_RECOVERY_TIMEOUT)
    parser.add_argument("--show-metrics", action="store_true", help="Include runtime metrics in output.")
    parser.add_argument("--proxy", help="Explicit HTTP/HTTPS proxy URL, e.g. http://user:pass@host:port")
    parser.add_argument("--no-env-proxy", action="store_true", help="Ignore HTTP_PROXY/HTTPS_PROXY environment variables.")

    return parser


def config_from_args(args: argparse.Namespace) -> AppConfig:
    config = AppConfig(
        api_key=resolve_api_key(args.api_key),
        retries=args.retries,
        backoff_base=args.backoff_base,
        backoff_max=args.backoff_max,
        connect_timeout=args.connect_timeout,
        read_timeout=args.read_timeout,
        poll_interval=args.poll,
        rate_limit_delay=args.rate_limit_delay,
        circuit_failure_threshold=args.circuit_failure_threshold,
        circuit_recovery_timeout=args.circuit_recovery_timeout,
        verbose=args.verbose,
        json_output=args.json,
        async_mode=args.async_mode,
        show_metrics=args.show_metrics,
        proxy=args.proxy,
        trust_env_proxy=not args.no_env_proxy,
    )
    config.validate()
    return config


def should_continue(config: AppConfig) -> bool:
    return config.poll_interval > 0 and not shutdown_event.is_set()


def run_sync(config: AppConfig, logger: logging.Logger) -> int:
    client = SyncEtherscanClient(config, logger, shutdown_event)
    try:
        while not shutdown_event.is_set():
            data = client.fetch_gas_prices()
            metrics = client.metrics_snapshot() if config.show_metrics else None
            render_output(data, config.json_output, metrics)

            if not should_continue(config):
                break
            sleep_interruptible(config.poll_interval, shutdown_event)
    finally:
        client.close()
    return EXIT_INTERRUPT if shutdown_event.is_set() else EXIT_OK


async def run_async(config: AppConfig, logger: logging.Logger) -> int:
    client = AsyncEtherscanClient(config, logger, shutdown_event)
    try:
        while not shutdown_event.is_set():
            data = await client.fetch_gas_prices()
            metrics = client.metrics_snapshot() if config.show_metrics else None
            render_output(data, config.json_output, metrics)

            if not should_continue(config):
                break
            await async_sleep_interruptible(config.poll_interval, shutdown_event)
    finally:
        await client.close()
    return EXIT_INTERRUPT if shutdown_event.is_set() else EXIT_OK


def cli(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    logger = configure_logger(args.verbose, args.json)

    try:
        config = config_from_args(args)
        logger.debug("config: %s", asdict(config) | {"api_key": "***"})

        if config.async_mode:
            return asyncio.run(run_async(config, logger))
        return run_sync(config, logger)

    except KeyboardInterrupt:
        shutdown_event.set()
        logger.warning("Interrupted")
        return EXIT_INTERRUPT
    except ValidationError as exc:
        logger.error("Validation error: %s", exc)
    except NonRetryableAPIError as exc:
        logger.error("Non-retryable API error: %s", exc)
    except CircuitBreakerOpen as exc:
        logger.error("Circuit breaker open: %s", exc)
    except Exception as exc:
        logger.error("Fatal: %s", exc, exc_info=args.verbose)

    return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(cli())
