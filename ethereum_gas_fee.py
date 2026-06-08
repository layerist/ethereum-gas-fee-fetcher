#!/usr/bin/env python3
"""
Etherscan Gas Tracker (Production-grade v7)
===========================================

Major upgrades:
- True async support (httpx.AsyncClient)
- Advanced circuit breaker (CLOSED / OPEN / HALF_OPEN)
- Thread-safe rate limiter
- Pydantic schema validation
- Strong retry separation
- Metrics collection
- Graceful shutdown
- Better logging
- Configurable request timeout
- Structured JSON output
- Polling mode
- Cleaner architecture
- Sync + Async clients
"""

from __future__ import annotations

import os
import sys
import time
import json
import uuid
import signal
import asyncio
import logging
import argparse
import threading

from enum import Enum
from typing import Optional, Any, Dict
from dataclasses import dataclass

import requests
import httpx
from pydantic import BaseModel, ValidationError, Field
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential_jitter,
    retry_if_exception,
    before_sleep_log,
)

# =============================================================================
# Optional orjson
# =============================================================================

try:
    import orjson

    def json_dumps(data: Any) -> str:
        return orjson.dumps(data).decode()

except ImportError:
    def json_dumps(data: Any) -> str:
        return json.dumps(data, indent=2)

# =============================================================================
# Constants
# =============================================================================

ETHERSCAN_API_URL = "https://api.etherscan.io/api"

MODULE = "gastracker"
ACTION = "gasoracle"

DEFAULT_CONNECT_TIMEOUT = 3.0
DEFAULT_READ_TIMEOUT = 10.0

DEFAULT_RETRIES = 5
DEFAULT_BACKOFF_BASE = 1
DEFAULT_BACKOFF_MAX = 10

USER_AGENT = "EtherscanGasTracker/6.0"

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_INTERRUPT = 130

# =============================================================================
# Config
# =============================================================================


@dataclass(frozen=True)
class AppConfig:
    api_key: str
    retries: int = DEFAULT_RETRIES
    backoff_base: int = DEFAULT_BACKOFF_BASE
    backoff_max: int = DEFAULT_BACKOFF_MAX

    connect_timeout: float = DEFAULT_CONNECT_TIMEOUT
    read_timeout: float = DEFAULT_READ_TIMEOUT

    poll_interval: float = 0.0
    rate_limit_delay: float = 0.0

    verbose: bool = False
    json_output: bool = False
    async_mode: bool = False


# =============================================================================
# Logger
# =============================================================================


def configure_logger(verbose: bool) -> logging.Logger:
    logger = logging.getLogger("etherscan")

    if logger.handlers:
        return logger

    logger.setLevel(logging.DEBUG if verbose else logging.INFO)

    handler = logging.StreamHandler(sys.stdout)

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(message)s",
        "%H:%M:%S",
    )

    handler.setFormatter(formatter)
    logger.addHandler(handler)

    logger.propagate = False
    return logger


# =============================================================================
# Models
# =============================================================================


class GasPrices(BaseModel):
    safe: float = Field(..., ge=0)
    proposed: float = Field(..., ge=0)
    fast: float = Field(..., ge=0)
    base_fee: float = Field(..., ge=0)
    last_block: int = Field(..., ge=0)


class EtherscanResponse(BaseModel):
    status: str
    message: str
    result: Dict[str, Any]


# =============================================================================
# Metrics
# =============================================================================


class Metrics:
    def __init__(self):
        self.total_requests = 0
        self.successful_requests = 0
        self.failed_requests = 0
        self.total_latency_ms = 0.0

    def add_success(self, latency_ms: float):
        self.total_requests += 1
        self.successful_requests += 1
        self.total_latency_ms += latency_ms

    def add_failure(self):
        self.total_requests += 1
        self.failed_requests += 1

    @property
    def avg_latency(self) -> float:
        if self.successful_requests == 0:
            return 0.0
        return self.total_latency_ms / self.successful_requests


# =============================================================================
# Circuit Breaker
# =============================================================================


class CircuitState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:

    def __init__(
        self,
        failure_threshold: int = 5,
        recovery_timeout: int = 30,
    ):
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
                elapsed = time.time() - self.last_failure_time

                if elapsed >= self.recovery_timeout:
                    self.state = CircuitState.HALF_OPEN
                    return True

                return False

            return True

    def record_success(self):
        with self._lock:
            self.failures = 0
            self.state = CircuitState.CLOSED

    def record_failure(self):
        with self._lock:
            self.failures += 1
            self.last_failure_time = time.time()

            if self.failures >= self.failure_threshold:
                self.state = CircuitState.OPEN


# =============================================================================
# Rate Limiter
# =============================================================================


class RateLimiter:

    def __init__(self, delay: float):
        self.delay = delay
        self.lock = threading.Lock()
        self.last_call = 0.0

    def wait(self):
        if self.delay <= 0:
            return

        with self.lock:
            elapsed = time.time() - self.last_call

            if elapsed < self.delay:
                time.sleep(self.delay - elapsed)

            self.last_call = time.time()


# =============================================================================
# Exceptions
# =============================================================================


class APIError(Exception):
    pass


class CircuitBreakerOpen(Exception):
    pass


# =============================================================================
# Utils
# =============================================================================


def resolve_api_key(cli_key: Optional[str]) -> str:
    key = cli_key or os.getenv("ETHERSCAN_API_KEY")

    if not key:
        raise ValueError(
            "Missing API key. "
            "Use --api-key or ETHERSCAN_API_KEY."
        )

    return key


def parse_gas(payload: dict) -> GasPrices:
    response = EtherscanResponse.model_validate(payload)

    if response.status != "1":
        raise APIError(response.message)

    result = response.result

    return GasPrices(
        safe=float(result["SafeGasPrice"]),
        proposed=float(result["ProposeGasPrice"]),
        fast=float(result["FastGasPrice"]),
        base_fee=float(result["suggestBaseFee"]),
        last_block=int(result["LastBlock"]),
    )


def is_retryable_exception(exc: Exception) -> bool:
    retryable = (
        requests.Timeout,
        requests.ConnectionError,
        httpx.TimeoutException,
        httpx.NetworkError,
        APIError,
    )

    return isinstance(exc, retryable)


# =============================================================================
# Client
# =============================================================================


class EtherscanClient:

    def __init__(
        self,
        config: AppConfig,
        logger: logging.Logger,
    ):
        self.config = config
        self.logger = logger

        self.metrics = Metrics()
        self.breaker = CircuitBreaker()
        self.rate_limiter = RateLimiter(
            config.rate_limit_delay
        )

        self.params = {
            "module": MODULE,
            "action": ACTION,
            "apikey": config.api_key,
        }

        self.session = requests.Session()

        self.session.headers.update({
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "Connection": "keep-alive",
        })

    def _retry_policy(self):
        return retry(
            stop=stop_after_attempt(
                self.config.retries
            ),
            wait=wait_exponential_jitter(
                initial=self.config.backoff_base,
                max=self.config.backoff_max,
            ),
            retry=retry_if_exception(
                is_retryable_exception
            ),
            before_sleep=before_sleep_log(
                self.logger,
                logging.WARNING,
            ),
            reraise=True,
        )

    @_retry_policy
    def fetch_gas_prices(self) -> GasPrices:
        raise RuntimeError("Decorator placeholder")


# =============================================================================
# Sync implementation
# =============================================================================


class SyncEtherscanClient(EtherscanClient):

    def __init__(
        self,
        config: AppConfig,
        logger: logging.Logger,
    ):
        super().__init__(config, logger)

    def fetch_gas_prices(self) -> GasPrices:

        @self._retry_policy()
        def _request() -> GasPrices:

            if not self.breaker.allow_request():
                raise CircuitBreakerOpen(
                    "Circuit breaker open"
                )

            self.rate_limiter.wait()

            request_id = str(uuid.uuid4())[:8]

            start = time.perf_counter()

            self.logger.debug(
                f"[{request_id}] Request started"
            )

            response = self.session.get(
                ETHERSCAN_API_URL,
                params=self.params,
                timeout=(
                    self.config.connect_timeout,
                    self.config.read_timeout,
                ),
            )

            response.raise_for_status()

            latency_ms = (
                time.perf_counter() - start
            ) * 1000

            payload = response.json()

            gas = parse_gas(payload)

            self.metrics.add_success(
                latency_ms
            )

            self.breaker.record_success()

            self.logger.debug(
                f"[{request_id}] "
                f"{latency_ms:.2f} ms"
            )

            return gas

        try:
            return _request()

        except Exception:
            self.metrics.add_failure()
            self.breaker.record_failure()
            raise


# =============================================================================
# Async implementation
# =============================================================================


class AsyncEtherscanClient:

    def __init__(
        self,
        config: AppConfig,
        logger: logging.Logger,
    ):
        self.config = config
        self.logger = logger

        self.client = httpx.AsyncClient(
            headers={
                "User-Agent": USER_AGENT
            },
            timeout=httpx.Timeout(
                connect=config.connect_timeout,
                read=config.read_timeout,
            ),
        )

    async def fetch_gas_prices(
        self,
    ) -> GasPrices:

        response = await self.client.get(
            ETHERSCAN_API_URL,
            params={
                "module": MODULE,
                "action": ACTION,
                "apikey": self.config.api_key,
            },
        )

        response.raise_for_status()

        payload = response.json()

        return parse_gas(payload)

    async def close(self):
        await self.client.aclose()


# =============================================================================
# Output
# =============================================================================


def render_output(
    data: GasPrices,
    json_output: bool,
):
    payload = data.model_dump()

    if json_output:
        print(json_dumps(payload))
        return

    print("\nEthereum Gas Prices")
    print("-" * 40)

    print(f"Safe       : {data.safe:.2f}")
    print(f"Proposed   : {data.proposed:.2f}")
    print(f"Fast       : {data.fast:.2f}")
    print(f"Base Fee   : {data.base_fee:.2f}")
    print(f"Last Block : {data.last_block}")


# =============================================================================
# Main
# =============================================================================


shutdown_event = threading.Event()


def signal_handler(*_):
    shutdown_event.set()


def cli():
    parser = argparse.ArgumentParser()

    parser.add_argument("--api-key")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--async-mode", action="store_true")

    parser.add_argument(
        "--poll",
        type=float,
        default=0.0,
    )

    args = parser.parse_args()

    signal.signal(
        signal.SIGINT,
        signal_handler,
    )

    logger = configure_logger(
        args.verbose
    )

    config = AppConfig(
        api_key=resolve_api_key(
            args.api_key
        ),
        json_output=args.json,
        verbose=args.verbose,
        poll_interval=args.poll,
        async_mode=args.async_mode,
    )

    try:

        if config.async_mode:

            async def async_runner():

                client = AsyncEtherscanClient(
                    config,
                    logger,
                )

                while (
                    not shutdown_event.is_set()
                ):
                    data = await (
                        client.fetch_gas_prices()
                    )

                    render_output(
                        data,
                        config.json_output,
                    )

                    if (
                        config.poll_interval
                        <= 0
                    ):
                        break

                    await asyncio.sleep(
                        config.poll_interval
                    )

                await client.close()

            asyncio.run(async_runner())

        else:

            client = SyncEtherscanClient(
                config,
                logger,
            )

            while (
                not shutdown_event.is_set()
            ):
                data = (
                    client.fetch_gas_prices()
                )

                render_output(
                    data,
                    config.json_output,
                )

                if (
                    config.poll_interval
                    <= 0
                ):
                    break

                time.sleep(
                    config.poll_interval
                )

        return EXIT_OK

    except ValidationError as exc:
        logger.error(
            "Validation error: %s",
            exc,
        )

    except Exception as exc:
        logger.error("Fatal: %s", exc)

    return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(cli())

# TODO improved
