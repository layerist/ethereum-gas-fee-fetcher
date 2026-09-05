#!/usr/bin/env python3

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import os
import random
import signal
import sys
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
from enum import Enum
from typing import Any, Mapping, Optional, TypeVar

import httpx
import requests
from pydantic import BaseModel, Field, ValidationError

try:
    import orjson
except ImportError:  # pragma: no cover - optional dependency
    orjson = None


API_URL_V2 = "https://api.etherscan.io/v2/api"
MODULE = "gastracker"
ACTION = "gasoracle"
DEFAULT_CHAIN_ID = "1"

DEFAULT_CONNECT_TIMEOUT = 5.0
DEFAULT_READ_TIMEOUT = 15.0
DEFAULT_RETRIES = 5
DEFAULT_BACKOFF_BASE = 0.5
DEFAULT_BACKOFF_MAX = 15.0
DEFAULT_CIRCUIT_FAILURE_THRESHOLD = 5
DEFAULT_CIRCUIT_RECOVERY_TIMEOUT = 30.0
DEFAULT_RATE_LIMIT_DELAY = 0.35  # conservative default for Free plan
USER_AGENT = "EtherscanGasTracker/11.0"

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_CONFIG = 2
EXIT_INTERRUPT = 130

TModel = TypeVar("TModel", bound=BaseModel)


# =============================================================================
# JSON / Pydantic compatibility
# =============================================================================


def json_dumps(data: Any, *, indent: bool = True) -> str:
    if orjson is not None:
        option = orjson.OPT_INDENT_2 if indent else 0
        return orjson.dumps(data, option=option, default=str).decode("utf-8")
    return json.dumps(data, indent=2 if indent else None, ensure_ascii=False, default=str)


def model_validate_compat(model: type[TModel], payload: Any) -> TModel:
    validator = getattr(model, "model_validate", None)
    if validator is not None:
        return validator(payload)
    return model.parse_obj(payload)


def model_dump_compat(instance: BaseModel) -> dict[str, Any]:
    dumper = getattr(instance, "model_dump", None)
    if dumper is not None:
        return dumper(mode="json")
    return instance.dict()


# =============================================================================
# Configuration
# =============================================================================


@dataclass(frozen=True, slots=True)
class AppConfig:
    api_key: str
    api_url: str = API_URL_V2
    chain_id: str = DEFAULT_CHAIN_ID
    retries: int = DEFAULT_RETRIES
    backoff_base: float = DEFAULT_BACKOFF_BASE
    backoff_max: float = DEFAULT_BACKOFF_MAX
    connect_timeout: float = DEFAULT_CONNECT_TIMEOUT
    read_timeout: float = DEFAULT_READ_TIMEOUT
    poll_interval: float = 0.0
    rate_limit_delay: float = DEFAULT_RATE_LIMIT_DELAY
    circuit_failure_threshold: int = DEFAULT_CIRCUIT_FAILURE_THRESHOLD
    circuit_recovery_timeout: float = DEFAULT_CIRCUIT_RECOVERY_TIMEOUT
    verbose: bool = False
    json_output: bool = False
    jsonl_output: bool = False
    async_mode: bool = False
    show_metrics: bool = False
    continue_on_error: bool = False
    proxy: Optional[str] = None
    trust_env_proxy: bool = True

    def validate(self) -> None:
        if not self.api_key.strip():
            raise ValueError("API key is empty")
        if not self.api_url.startswith(("https://", "http://")):
            raise ValueError("--api-url must start with http:// or https://")
        if not self.chain_id.isdigit() or int(self.chain_id) <= 0:
            raise ValueError("--chain-id must be a positive integer")
        numeric = {
            "--backoff-base": self.backoff_base,
            "--backoff-max": self.backoff_max,
            "--connect-timeout": self.connect_timeout,
            "--read-timeout": self.read_timeout,
            "--poll": self.poll_interval,
            "--rate-limit-delay": self.rate_limit_delay,
            "--circuit-recovery-timeout": self.circuit_recovery_timeout,
        }
        for name, value in numeric.items():
            if not math.isfinite(value):
                raise ValueError(f"{name} must be finite")
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
        if self.json_output and self.jsonl_output:
            raise ValueError("--json and --jsonl are mutually exclusive")


# =============================================================================
# Logging
# =============================================================================


def configure_logger(verbose: bool, machine_output: bool) -> logging.Logger:
    logger = logging.getLogger("etherscan_gas_tracker")
    logger.handlers.clear()
    logger.propagate = False
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        logging.Formatter("%(asctime)s | %(levelname)-8s | %(message)s", "%Y-%m-%d %H:%M:%S")
    )
    logger.addHandler(handler)

    if machine_output and not verbose:
        logger.setLevel(logging.WARNING)
    return logger


# =============================================================================
# Models
# =============================================================================


class EtherscanEnvelope(BaseModel):
    status: str
    message: str
    result: Any


class GasPrices(BaseModel):
    chain_id: str
    safe_gwei: Decimal = Field(..., ge=0)
    proposed_gwei: Decimal = Field(..., ge=0)
    fast_gwei: Decimal = Field(..., ge=0)
    base_fee_gwei: Decimal = Field(..., ge=0)
    last_block: int = Field(..., ge=0)
    gas_used_ratio: tuple[Decimal, ...] = ()
    fetched_at: str


# =============================================================================
# Exceptions
# =============================================================================


class TrackerError(Exception):
    """Base application exception."""


class APIError(TrackerError):
    pass


class RetryableAPIError(APIError):
    def __init__(
        self,
        message: str,
        *,
        retry_after: Optional[float] = None,
        circuit_failure: bool = False,
    ) -> None:
        super().__init__(message)
        self.retry_after = retry_after
        self.circuit_failure = circuit_failure


class NonRetryableAPIError(APIError):
    pass


class CircuitBreakerOpen(TrackerError):
    pass


class ShutdownRequested(TrackerError):
    pass


@dataclass(slots=True)
class AttemptFailure:
    error: Exception
    retry_after: Optional[float] = None


# =============================================================================
# Metrics
# =============================================================================


class Metrics:
    def __init__(self) -> None:
        self.logical_requests = 0
        self.successful_requests = 0
        self.failed_requests = 0
        self.http_attempts = 0
        self.retry_attempts = 0
        self.circuit_blocked = 0
        self.rate_limited_responses = 0
        self.total_latency_ms = 0.0
        self.last_latency_ms = 0.0
        self.last_error: Optional[str] = None
        self._lock = threading.Lock()

    def begin_request(self) -> None:
        with self._lock:
            self.logical_requests += 1

    def add_attempt(self, *, retry: bool) -> None:
        with self._lock:
            self.http_attempts += 1
            if retry:
                self.retry_attempts += 1

    def add_circuit_blocked(self) -> None:
        with self._lock:
            self.circuit_blocked += 1

    def add_rate_limited_response(self) -> None:
        with self._lock:
            self.rate_limited_responses += 1

    def finish_success(self, latency_ms: float) -> None:
        with self._lock:
            self.successful_requests += 1
            self.total_latency_ms += latency_ms
            self.last_latency_ms = latency_ms
            self.last_error = None

    def finish_failure(self, exc: Exception) -> None:
        with self._lock:
            self.failed_requests += 1
            self.last_error = f"{type(exc).__name__}: {exc}"

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            avg = (
                self.total_latency_ms / self.successful_requests
                if self.successful_requests
                else 0.0
            )
            return {
                "logical_requests": self.logical_requests,
                "successful_requests": self.successful_requests,
                "failed_requests": self.failed_requests,
                "http_attempts": self.http_attempts,
                "retry_attempts": self.retry_attempts,
                "circuit_blocked": self.circuit_blocked,
                "rate_limited_responses": self.rate_limited_responses,
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
    """Thread-safe breaker that permits only one HALF_OPEN probe."""

    def __init__(self, failure_threshold: int, recovery_timeout: float) -> None:
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.failures = 0
        self.opened_at: Optional[float] = None
        self.state = CircuitState.CLOSED
        self._probe_in_flight = False
        self._lock = threading.Lock()

    def acquire_permission(self) -> None:
        with self._lock:
            now = time.monotonic()
            if self.state == CircuitState.CLOSED:
                return

            if self.state == CircuitState.OPEN:
                assert self.opened_at is not None
                if now - self.opened_at < self.recovery_timeout:
                    remaining = self.recovery_timeout - (now - self.opened_at)
                    raise CircuitBreakerOpen(
                        f"circuit is open; retry in approximately {remaining:.1f}s"
                    )
                self.state = CircuitState.HALF_OPEN
                self._probe_in_flight = False

            if self._probe_in_flight:
                raise CircuitBreakerOpen("HALF_OPEN probe is already in progress")
            self._probe_in_flight = True

    def record_success(self) -> None:
        with self._lock:
            self.failures = 0
            self.opened_at = None
            self.state = CircuitState.CLOSED
            self._probe_in_flight = False

    def record_failure(self) -> None:
        with self._lock:
            if self.state == CircuitState.HALF_OPEN:
                self.failures = max(self.failures, self.failure_threshold)
                self.state = CircuitState.OPEN
                self.opened_at = time.monotonic()
                self._probe_in_flight = False
                return

            self.failures += 1
            if self.failures >= self.failure_threshold:
                self.state = CircuitState.OPEN
                self.opened_at = time.monotonic()
            self._probe_in_flight = False

    def record_neutral(self) -> None:
        """Release a HALF_OPEN probe for a non-transient/application error.

        A valid HTTP response such as 401/403 proves that the remote endpoint is
        reachable, so it must not leave the breaker stuck in HALF_OPEN. It also
        must not count as an infrastructure failure.
        """
        with self._lock:
            if self.state == CircuitState.HALF_OPEN:
                self.state = CircuitState.CLOSED
                self.opened_at = None
                self.failures = 0
            self._probe_in_flight = False

    def cancel_permission(self) -> None:
        """Release a probe when no attempt was completed (for example shutdown)."""
        with self._lock:
            self._probe_in_flight = False

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            retry_in = None
            if self.state == CircuitState.OPEN and self.opened_at is not None:
                retry_in = max(0.0, self.recovery_timeout - (time.monotonic() - self.opened_at))
            return {
                "state": self.state.value,
                "failures": self.failures,
                "failure_threshold": self.failure_threshold,
                "recovery_timeout_sec": self.recovery_timeout,
                "retry_in_sec": round(retry_in, 2) if retry_in is not None else None,
            }


# =============================================================================
# Rate limiters and shutdown-aware sleeping
# =============================================================================


class SyncRateLimiter:
    def __init__(self, delay: float) -> None:
        self.delay = delay
        self._next_allowed = 0.0
        self._lock = threading.Lock()

    def wait(self, shutdown_event: threading.Event) -> None:
        if self.delay <= 0:
            return
        with self._lock:
            now = time.monotonic()
            wait_sec = max(0.0, self._next_allowed - now)
            if wait_sec and shutdown_event.wait(wait_sec):
                raise ShutdownRequested
            self._next_allowed = max(time.monotonic(), self._next_allowed) + self.delay


class AsyncRateLimiter:
    def __init__(self, delay: float) -> None:
        self.delay = delay
        self._next_allowed = 0.0
        self._lock = asyncio.Lock()

    async def wait(self, shutdown_event: threading.Event) -> None:
        if self.delay <= 0:
            return
        async with self._lock:
            now = time.monotonic()
            wait_sec = max(0.0, self._next_allowed - now)
            if wait_sec:
                await async_sleep_interruptible(wait_sec, shutdown_event)
            self._next_allowed = max(time.monotonic(), self._next_allowed) + self.delay


def sleep_interruptible(seconds: float, shutdown_event: threading.Event) -> None:
    if shutdown_event.wait(max(0.0, seconds)):
        raise ShutdownRequested


async def async_sleep_interruptible(seconds: float, shutdown_event: threading.Event) -> None:
    deadline = time.monotonic() + max(0.0, seconds)
    while True:
        if shutdown_event.is_set():
            raise ShutdownRequested
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        await asyncio.sleep(min(remaining, 0.2))


# =============================================================================
# Parsing and error classification
# =============================================================================


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def resolve_api_key(cli_key: Optional[str]) -> str:
    value = (cli_key or os.getenv("ETHERSCAN_API_KEY", "")).strip()
    if not value:
        raise ValueError("Missing API key. Use --api-key or ETHERSCAN_API_KEY.")
    return value


def build_params(config: AppConfig) -> dict[str, str]:
    return {
        "chainid": config.chain_id,
        "module": MODULE,
        "action": ACTION,
        "apikey": config.api_key,
    }


def decimal_value(value: Any, field_name: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise APIError(f"invalid decimal in {field_name}: {value!r}") from exc
    if not parsed.is_finite() or parsed < 0:
        raise APIError(f"invalid non-finite or negative value in {field_name}: {value!r}")
    return parsed


def classify_etherscan_error(message: Any, result: Any) -> APIError:
    text = f"{message} {result}".strip()
    lowered = text.lower()

    non_retry_markers = (
        "invalid api key",
        "missing api key",
        "invalid action",
        "invalid module",
        "invalid chain",
        "unsupported chain",
        "not a valid chainid",
    )
    retry_markers = (
        "rate limit",
        "max rate limit",
        "temporarily unavailable",
        "server too busy",
        "busy",
        "timeout",
        "timed out",
        "try again",
    )

    if any(marker in lowered for marker in non_retry_markers):
        return NonRetryableAPIError(f"Etherscan rejected the request: {text}")
    if any(marker in lowered for marker in retry_markers):
        return RetryableAPIError(f"temporary Etherscan error: {text}")
    return APIError(f"Etherscan API error: {text}")


def parse_gas(payload: Mapping[str, Any], chain_id: str) -> GasPrices:
    try:
        envelope = model_validate_compat(EtherscanEnvelope, payload)
    except ValidationError as exc:
        raise RetryableAPIError(
            f"invalid Etherscan response envelope: {exc}", circuit_failure=True
        ) from exc
    if envelope.status != "1":
        raise classify_etherscan_error(envelope.message, envelope.result)
    if not isinstance(envelope.result, Mapping):
        raise APIError(f"unexpected result type: {type(envelope.result).__name__}")

    result = envelope.result
    required = (
        "SafeGasPrice",
        "ProposeGasPrice",
        "FastGasPrice",
        "suggestBaseFee",
        "LastBlock",
    )
    missing = [name for name in required if name not in result]
    if missing:
        raise APIError(f"missing Etherscan fields: {', '.join(missing)}")

    ratios: tuple[Decimal, ...] = ()
    raw_ratios = result.get("gasUsedRatio")
    if isinstance(raw_ratios, str) and raw_ratios.strip():
        ratios = tuple(
            decimal_value(part.strip(), "gasUsedRatio")
            for part in raw_ratios.split(",")
            if part.strip()
        )

    try:
        last_block = int(str(result["LastBlock"]))
    except (TypeError, ValueError) as exc:
        raise APIError(f"invalid LastBlock: {result['LastBlock']!r}") from exc

    try:
        return GasPrices(
            chain_id=chain_id,
            safe_gwei=decimal_value(result["SafeGasPrice"], "SafeGasPrice"),
            proposed_gwei=decimal_value(result["ProposeGasPrice"], "ProposeGasPrice"),
            fast_gwei=decimal_value(result["FastGasPrice"], "FastGasPrice"),
            base_fee_gwei=decimal_value(result["suggestBaseFee"], "suggestBaseFee"),
            last_block=last_block,
            gas_used_ratio=ratios,
            fetched_at=utc_now_iso(),
        )
    except ValidationError as exc:
        raise RetryableAPIError(
            f"invalid Etherscan gas payload: {exc}", circuit_failure=True
        ) from exc


def parse_retry_after(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    value = value.strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=timezone.utc)
            return max(0.0, (retry_at - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return None


def classify_http_status(status_code: int, body_preview: str) -> APIError:
    message = f"HTTP {status_code} from Etherscan"
    if body_preview:
        message += f": {body_preview}"
    if status_code == 429:
        # The endpoint is alive and explicitly throttling us. Retry it, but do
        # not treat it as an infrastructure outage for the circuit breaker.
        return RetryableAPIError(message, circuit_failure=False)
    if 500 <= status_code <= 599:
        return RetryableAPIError(message, circuit_failure=True)
    if status_code in (401, 403, 404):
        return NonRetryableAPIError(message)
    return APIError(message)


def is_retryable_exception(exc: Exception) -> bool:
    if isinstance(exc, NonRetryableAPIError):
        return False
    return isinstance(
        exc,
        (
            RetryableAPIError,
            requests.Timeout,
            requests.ConnectionError,
            httpx.TimeoutException,
            httpx.NetworkError,
            httpx.RemoteProtocolError,
            httpx.PoolTimeout,
        ),
    )


def is_circuit_failure(exc: Exception) -> bool:
    if isinstance(exc, RetryableAPIError):
        return exc.circuit_failure
    return isinstance(
        exc,
        (
            requests.Timeout,
            requests.ConnectionError,
            httpx.TimeoutException,
            httpx.NetworkError,
            httpx.RemoteProtocolError,
            httpx.PoolTimeout,
        ),
    )


def compute_backoff_delay(
    attempt_no: int,
    base: float,
    max_delay: float,
    retry_after: Optional[float] = None,
) -> float:
    cap = min(max_delay, base * (2 ** max(0, attempt_no - 1)))
    jittered = random.uniform(0.0, cap)  # full jitter
    if retry_after is None:
        return jittered
    # Retry-After is a server-side minimum. Do not silently cap it with
    # --backoff-max; doing so can cause another immediate 429/503.
    return max(jittered, retry_after)


def safe_body_preview(text: str, limit: int = 300) -> str:
    compact = " ".join(text.split())
    return compact[:limit]


# =============================================================================
# Clients
# =============================================================================


class BaseEtherscanClient:
    def __init__(self, config: AppConfig, logger: logging.Logger) -> None:
        self.config = config
        self.logger = logger
        self.metrics = Metrics()
        self.breaker = CircuitBreaker(
            config.circuit_failure_threshold,
            config.circuit_recovery_timeout,
        )
        self.params = build_params(config)

    def metrics_snapshot(self) -> dict[str, Any]:
        return {
            "metrics": self.metrics.snapshot(),
            "circuit_breaker": self.breaker.snapshot(),
        }

    def _retry_delay(self, attempt_no: int, failure: AttemptFailure) -> float:
        return compute_backoff_delay(
            attempt_no,
            self.config.backoff_base,
            self.config.backoff_max,
            failure.retry_after,
        )


class SyncEtherscanClient(BaseEtherscanClient):
    def __init__(
        self,
        config: AppConfig,
        logger: logging.Logger,
        shutdown_event: threading.Event,
    ) -> None:
        super().__init__(config, logger)
        self.shutdown_event = shutdown_event
        self.rate_limiter = SyncRateLimiter(config.rate_limit_delay)
        self.session = requests.Session()
        self.session.trust_env = config.trust_env_proxy
        self.session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})
        self.proxies = (
            {"http": config.proxy, "https": config.proxy} if config.proxy else None
        )

    def close(self) -> None:
        self.session.close()

    def fetch_gas_prices(self) -> GasPrices:
        self.metrics.begin_request()
        logical_start = time.perf_counter()
        last_error: Optional[Exception] = None

        try:
            for attempt_no in range(1, self.config.retries + 1):
                try:
                    gas = self._fetch_once(retry=attempt_no > 1)
                    self.metrics.finish_success((time.perf_counter() - logical_start) * 1000)
                    return gas
                except ShutdownRequested:
                    raise
                except Exception as exc:
                    last_error = exc
                    failure = AttemptFailure(exc, getattr(exc, "retry_after", None))
                    if attempt_no >= self.config.retries or not is_retryable_exception(exc):
                        raise
                    delay = self._retry_delay(attempt_no, failure)
                    self.logger.warning(
                        "attempt %d/%d failed: %s; retrying in %.2fs",
                        attempt_no,
                        self.config.retries,
                        exc,
                        delay,
                    )
                    sleep_interruptible(delay, self.shutdown_event)
        except Exception as exc:
            self.metrics.finish_failure(exc)
            raise

        error = last_error or RuntimeError("request failed without an exception")
        self.metrics.finish_failure(error)
        raise error

    def _fetch_once(self, *, retry: bool) -> GasPrices:
        try:
            self.breaker.acquire_permission()
        except CircuitBreakerOpen:
            self.metrics.add_circuit_blocked()
            raise
        try:
            self.rate_limiter.wait(self.shutdown_event)
            if self.shutdown_event.is_set():
                raise ShutdownRequested

            start = time.perf_counter()
            self.metrics.add_attempt(retry=retry)
            response = self.session.get(
                self.config.api_url,
                params=self.params,
                timeout=(self.config.connect_timeout, self.config.read_timeout),
                proxies=self.proxies,
            )
            if not 200 <= response.status_code < 300:
                error = classify_http_status(
                    response.status_code,
                    safe_body_preview(response.text),
                )
                if isinstance(error, RetryableAPIError):
                    error.retry_after = parse_retry_after(response.headers.get("Retry-After"))
                    if response.status_code == 429:
                        self.metrics.add_rate_limited_response()
                raise error
            try:
                payload = response.json()
            except requests.JSONDecodeError as exc:
                raise RetryableAPIError(
                    f"invalid JSON response: {safe_body_preview(response.text)}",
                    circuit_failure=True,
                ) from exc
            gas = parse_gas(payload, self.config.chain_id)
        except ShutdownRequested:
            self.breaker.cancel_permission()
            raise
        except Exception as exc:
            if is_circuit_failure(exc):
                self.breaker.record_failure()
            else:
                self.breaker.record_neutral()
            raise
        else:
            self.breaker.record_success()
            self.logger.debug(
                "sync HTTP attempt completed in %.2f ms",
                (time.perf_counter() - start) * 1000,
            )
            return gas


class AsyncEtherscanClient(BaseEtherscanClient):
    def __init__(
        self,
        config: AppConfig,
        logger: logging.Logger,
        shutdown_event: threading.Event,
    ) -> None:
        super().__init__(config, logger)
        self.shutdown_event = shutdown_event
        self.rate_limiter = AsyncRateLimiter(config.rate_limit_delay)
        timeout = httpx.Timeout(
            connect=config.connect_timeout,
            read=config.read_timeout,
            write=config.read_timeout,
            pool=config.connect_timeout,
        )
        self.client = httpx.AsyncClient(
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            timeout=timeout,
            proxy=config.proxy,
            trust_env=config.trust_env_proxy,
            follow_redirects=False,
        )

    async def close(self) -> None:
        await self.client.aclose()

    async def fetch_gas_prices(self) -> GasPrices:
        self.metrics.begin_request()
        logical_start = time.perf_counter()
        last_error: Optional[Exception] = None

        try:
            for attempt_no in range(1, self.config.retries + 1):
                try:
                    gas = await self._fetch_once(retry=attempt_no > 1)
                    self.metrics.finish_success((time.perf_counter() - logical_start) * 1000)
                    return gas
                except ShutdownRequested:
                    raise
                except Exception as exc:
                    last_error = exc
                    failure = AttemptFailure(exc, getattr(exc, "retry_after", None))
                    if attempt_no >= self.config.retries or not is_retryable_exception(exc):
                        raise
                    delay = self._retry_delay(attempt_no, failure)
                    self.logger.warning(
                        "attempt %d/%d failed: %s; retrying in %.2fs",
                        attempt_no,
                        self.config.retries,
                        exc,
                        delay,
                    )
                    await async_sleep_interruptible(delay, self.shutdown_event)
        except Exception as exc:
            self.metrics.finish_failure(exc)
            raise

        error = last_error or RuntimeError("request failed without an exception")
        self.metrics.finish_failure(error)
        raise error

    async def _fetch_once(self, *, retry: bool) -> GasPrices:
        try:
            self.breaker.acquire_permission()
        except CircuitBreakerOpen:
            self.metrics.add_circuit_blocked()
            raise
        try:
            await self.rate_limiter.wait(self.shutdown_event)
            if self.shutdown_event.is_set():
                raise ShutdownRequested

            start = time.perf_counter()
            self.metrics.add_attempt(retry=retry)
            response = await self.client.get(self.config.api_url, params=self.params)
            if not 200 <= response.status_code < 300:
                error = classify_http_status(
                    response.status_code,
                    safe_body_preview(response.text),
                )
                if isinstance(error, RetryableAPIError):
                    error.retry_after = parse_retry_after(response.headers.get("Retry-After"))
                    if response.status_code == 429:
                        self.metrics.add_rate_limited_response()
                raise error
            try:
                payload = response.json()
            except json.JSONDecodeError as exc:
                raise RetryableAPIError(
                    f"invalid JSON response: {safe_body_preview(response.text)}",
                    circuit_failure=True,
                ) from exc
            gas = parse_gas(payload, self.config.chain_id)
        except ShutdownRequested:
            self.breaker.cancel_permission()
            raise
        except Exception as exc:
            if is_circuit_failure(exc):
                self.breaker.record_failure()
            else:
                self.breaker.record_neutral()
            raise
        else:
            self.breaker.record_success()
            self.logger.debug(
                "async HTTP attempt completed in %.2f ms",
                (time.perf_counter() - start) * 1000,
            )
            return gas


# =============================================================================
# Output
# =============================================================================


def decimal_to_string(value: Decimal) -> str:
    return format(value, "f")


def output_payload(data: GasPrices, runtime: Optional[dict[str, Any]]) -> dict[str, Any]:
    payload = model_dump_compat(data)
    for key in ("safe_gwei", "proposed_gwei", "fast_gwei", "base_fee_gwei"):
        payload[key] = decimal_to_string(getattr(data, key))
    payload["gas_used_ratio"] = [decimal_to_string(x) for x in data.gas_used_ratio]
    if runtime is not None:
        payload["runtime"] = runtime
    return payload


def render_output(
    data: GasPrices,
    *,
    json_output: bool,
    jsonl_output: bool,
    runtime: Optional[dict[str, Any]] = None,
) -> None:
    payload = output_payload(data, runtime)
    if json_output:
        print(json_dumps(payload, indent=True), flush=True)
        return
    if jsonl_output:
        print(json_dumps(payload, indent=False), flush=True)
        return

    print("\nEVM Gas Prices")
    print("-" * 52)
    print(f"Chain ID   : {data.chain_id}")
    print(f"Safe       : {data.safe_gwei:.9f} gwei")
    print(f"Proposed   : {data.proposed_gwei:.9f} gwei")
    print(f"Fast       : {data.fast_gwei:.9f} gwei")
    print(f"Base Fee   : {data.base_fee_gwei:.9f} gwei")
    print(f"Last Block : {data.last_block}")
    print(f"Fetched At : {data.fetched_at}")
    if data.gas_used_ratio:
        avg_ratio = sum(data.gas_used_ratio, Decimal(0)) / len(data.gas_used_ratio)
        print(f"Gas Usage  : {avg_ratio * 100:.2f}% avg ({len(data.gas_used_ratio)} blocks)")

    if runtime:
        metrics = runtime["metrics"]
        breaker = runtime["circuit_breaker"]
        print("\nRuntime")
        print("-" * 52)
        print(
            "Requests   : "
            f"{metrics['logical_requests']} logical / "
            f"{metrics['http_attempts']} HTTP / "
            f"{metrics['retry_attempts']} retries"
        )
        print(
            "Results    : "
            f"{metrics['successful_requests']} ok / "
            f"{metrics['failed_requests']} failed"
        )
        print(
            "Control    : "
            f"{metrics['circuit_blocked']} breaker-blocked / "
            f"{metrics['rate_limited_responses']} rate-limited"
        )
        print(
            "Latency    : "
            f"{metrics['last_latency_ms']:.2f} ms last / "
            f"{metrics['avg_latency_ms']:.2f} ms avg"
        )
        print(
            "Circuit    : "
            f"{breaker['state']} / failures "
            f"{breaker['failures']}/{breaker['failure_threshold']}"
        )


# =============================================================================
# Runtime loops
# =============================================================================


shutdown_event = threading.Event()


def signal_handler(_signum: int, _frame: Any) -> None:
    shutdown_event.set()


def should_poll(config: AppConfig) -> bool:
    return config.poll_interval > 0


def handle_poll_error(
    exc: Exception,
    config: AppConfig,
    logger: logging.Logger,
) -> bool:
    """Return True when polling should continue."""
    if not should_poll(config) or not config.continue_on_error:
        return False
    logger.error("poll iteration failed: %s", exc)
    return not shutdown_event.is_set()


def run_sync(config: AppConfig, logger: logging.Logger) -> int:
    client = SyncEtherscanClient(config, logger, shutdown_event)
    try:
        while not shutdown_event.is_set():
            try:
                data = client.fetch_gas_prices()
                runtime = client.metrics_snapshot() if config.show_metrics else None
                render_output(
                    data,
                    json_output=config.json_output,
                    jsonl_output=config.jsonl_output,
                    runtime=runtime,
                )
            except ShutdownRequested:
                break
            except Exception as exc:
                if not handle_poll_error(exc, config, logger):
                    raise

            if not should_poll(config):
                break
            try:
                sleep_interruptible(config.poll_interval, shutdown_event)
            except ShutdownRequested:
                break
    finally:
        client.close()
    return EXIT_INTERRUPT if shutdown_event.is_set() else EXIT_OK


async def run_async(config: AppConfig, logger: logging.Logger) -> int:
    client = AsyncEtherscanClient(config, logger, shutdown_event)
    try:
        while not shutdown_event.is_set():
            try:
                data = await client.fetch_gas_prices()
                runtime = client.metrics_snapshot() if config.show_metrics else None
                render_output(
                    data,
                    json_output=config.json_output,
                    jsonl_output=config.jsonl_output,
                    runtime=runtime,
                )
            except ShutdownRequested:
                break
            except Exception as exc:
                if not handle_poll_error(exc, config, logger):
                    raise

            if not should_poll(config):
                break
            try:
                await async_sleep_interruptible(config.poll_interval, shutdown_event)
            except ShutdownRequested:
                break
    finally:
        await client.close()
    return EXIT_INTERRUPT if shutdown_event.is_set() else EXIT_OK


# =============================================================================
# CLI
# =============================================================================


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Fetch gas prices from Etherscan API V2.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--api-key", help="API key; defaults to ETHERSCAN_API_KEY")
    parser.add_argument("--api-url", default=API_URL_V2, help="Etherscan-compatible API URL")
    parser.add_argument("--chain-id", default=DEFAULT_CHAIN_ID, help="EVM chain ID")

    output = parser.add_mutually_exclusive_group()
    output.add_argument("--json", action="store_true", help="Pretty JSON output")
    output.add_argument("--jsonl", action="store_true", help="One compact JSON object per poll")

    parser.add_argument("--verbose", action="store_true", help="Enable debug logs")
    parser.add_argument("--async-mode", action="store_true", help="Use httpx.AsyncClient")
    parser.add_argument("--poll", type=float, default=0.0, help="Polling interval; 0 means one request")
    parser.add_argument(
        "--continue-on-error",
        action="store_true",
        help="In polling mode continue after a failed iteration",
    )
    parser.add_argument(
        "--rate-limit-delay",
        type=float,
        default=DEFAULT_RATE_LIMIT_DELAY,
        help="Minimum spacing between HTTP attempts",
    )
    parser.add_argument("--retries", type=int, default=DEFAULT_RETRIES, help="Maximum HTTP attempts per logical request")
    parser.add_argument("--backoff-base", type=float, default=DEFAULT_BACKOFF_BASE)
    parser.add_argument("--backoff-max", type=float, default=DEFAULT_BACKOFF_MAX)
    parser.add_argument("--connect-timeout", type=float, default=DEFAULT_CONNECT_TIMEOUT)
    parser.add_argument("--read-timeout", type=float, default=DEFAULT_READ_TIMEOUT)
    parser.add_argument(
        "--circuit-failure-threshold",
        type=int,
        default=DEFAULT_CIRCUIT_FAILURE_THRESHOLD,
    )
    parser.add_argument(
        "--circuit-recovery-timeout",
        type=float,
        default=DEFAULT_CIRCUIT_RECOVERY_TIMEOUT,
    )
    parser.add_argument("--show-metrics", action="store_true")
    parser.add_argument("--proxy", help="Explicit proxy URL")
    parser.add_argument(
        "--no-env-proxy",
        action="store_true",
        help="Ignore HTTP_PROXY/HTTPS_PROXY environment variables",
    )
    return parser


def config_from_args(args: argparse.Namespace) -> AppConfig:
    config = AppConfig(
        api_key=resolve_api_key(args.api_key),
        api_url=args.api_url.strip(),
        chain_id=str(args.chain_id).strip(),
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
        jsonl_output=args.jsonl,
        async_mode=args.async_mode,
        show_metrics=args.show_metrics,
        continue_on_error=args.continue_on_error,
        proxy=args.proxy,
        trust_env_proxy=not args.no_env_proxy,
    )
    config.validate()
    return config


def cli(argv: Optional[list[str]] = None) -> int:
    shutdown_event.clear()
    parser = build_parser()
    args = parser.parse_args(argv)
    logger = configure_logger(args.verbose, args.json or args.jsonl)

    if threading.current_thread() is threading.main_thread():
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, signal_handler)

    try:
        config = config_from_args(args)
        redacted = asdict(config)
        redacted["api_key"] = "***"
        if redacted.get("proxy"):
            redacted["proxy"] = "***"
        logger.debug("config=%s", redacted)

        if config.async_mode:
            return asyncio.run(run_async(config, logger))
        return run_sync(config, logger)
    except (ValueError, argparse.ArgumentError) as exc:
        logger.error("configuration error: %s", exc)
        return EXIT_CONFIG
    except ShutdownRequested:
        return EXIT_INTERRUPT
    except ValidationError as exc:
        logger.error("response validation error: %s", exc)
    except NonRetryableAPIError as exc:
        logger.error("non-retryable API error: %s", exc)
    except CircuitBreakerOpen as exc:
        logger.error("circuit breaker open: %s", exc)
    except APIError as exc:
        logger.error("API error: %s", exc)
    except KeyboardInterrupt:
        shutdown_event.set()
        return EXIT_INTERRUPT
    except Exception as exc:
        logger.error("fatal error: %s", exc, exc_info=args.verbose)

    return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(cli())
