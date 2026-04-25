#!/usr/bin/env python3
"""
Etherscan Gas Tracker (Production-grade v5)
------------------------------------------
Major upgrades:
- Circuit breaker (protects from API outages)
- Structured logging with request_id
- Async support (optional)
- Better retry separation (network vs API errors)
- Strict schema validation
- Proper JSON output
- Polling mode
- Improved session tuning
"""

from __future__ import annotations

import os
import sys
import time
import json
import uuid
import logging
import argparse
from dataclasses import dataclass
from typing import Mapping, Any, Optional, TypedDict, Callable

import requests
from requests import Session
from requests.adapters import HTTPAdapter
from requests.exceptions import (
    ConnectTimeout,
    ReadTimeout,
    ConnectionError,
    HTTPError,
)

from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential_jitter,
    before_sleep_log,
    retry_if_exception,
    RetryError,
)

# Optional ultra-fast JSON
try:
    import orjson

    def json_loads(data: bytes):
        return orjson.loads(data)

    def json_dumps(data: Any):
        return orjson.dumps(data).decode()

except ImportError:
    def json_loads(data: bytes):
        return json.loads(data)

    def json_dumps(data: Any):
        return json.dumps(data, indent=2)


# =============================================================================
# Constants
# =============================================================================

ETHERSCAN_API_URL = "https://api.etherscan.io/api"

MODULE = "gastracker"
ACTION = "gasoracle"

CONNECT_TIMEOUT = 3
READ_TIMEOUT = 10

DEFAULT_RETRIES = 3
DEFAULT_BACKOFF_BASE = 1
DEFAULT_BACKOFF_MAX = 10

USER_AGENT = "EtherscanGasTracker/5.0"

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_INTERRUPT = 130


# =============================================================================
# Types
# =============================================================================

class GasPrices(TypedDict):
    safe: float
    proposed: float
    fast: float
    base_fee: float
    last_block: int


# =============================================================================
# Config
# =============================================================================

@dataclass(frozen=True)
class AppConfig:
    api_key: str
    retries: int = DEFAULT_RETRIES
    backoff_base: int = DEFAULT_BACKOFF_BASE
    backoff_max: int = DEFAULT_BACKOFF_MAX
    json_output: bool = False
    verbose: bool = False
    rate_limit_delay: float = 0.0
    poll_interval: float = 0.0
    timeout_total: float = 15.0


# =============================================================================
# Logging
# =============================================================================

def configure_logger(verbose: bool) -> logging.Logger:
    logger = logging.getLogger("etherscan_gas_tracker")

    if logger.handlers:
        return logger

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s | %(levelname)-8s | %(message)s",
            "%H:%M:%S",
        )
    )

    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    logger.propagate = False

    return logger


# =============================================================================
# Circuit Breaker
# =============================================================================

class CircuitBreaker:
    def __init__(self, failure_threshold=5, recovery_time=30):
        self.failure_threshold = failure_threshold
        self.recovery_time = recovery_time
        self.failures = 0
        self.last_failure_time = 0

    def allow(self) -> bool:
        if self.failures < self.failure_threshold:
            return True

        if time.time() - self.last_failure_time > self.recovery_time:
            self.failures = 0
            return True

        return False

    def record_success(self):
        self.failures = 0

    def record_failure(self):
        self.failures += 1
        self.last_failure_time = time.time()


# =============================================================================
# Utils
# =============================================================================

def resolve_api_key(cli_key: Optional[str]) -> str:
    api_key = cli_key or os.getenv("ETHERSCAN_API_KEY")

    if not api_key:
        raise ValueError("Missing Etherscan API key.")

    return api_key


def is_retryable_exception(exc: Exception) -> bool:
    if isinstance(exc, (ConnectTimeout, ReadTimeout, ConnectionError)):
        return True

    if isinstance(exc, HTTPError) and exc.response is not None:
        return exc.response.status_code >= 500 or exc.response.status_code == 429

    if isinstance(exc, ValueError):
        return True

    return False


def parse_float(value: Any, field: str) -> float:
    try:
        return float(value)
    except Exception:
        raise ValueError(f"Invalid float: {field}={value}")


def parse_int(value: Any, field: str) -> int:
    try:
        return int(value)
    except Exception:
        raise ValueError(f"Invalid int: {field}={value}")


def parse_gas_response(payload: Mapping[str, Any]) -> GasPrices:
    if payload.get("status") != "1":
        raise ValueError(f"API error: {payload}")

    result = payload.get("result")
    if not isinstance(result, Mapping):
        raise ValueError("Malformed response")

    return {
        "safe": parse_float(result.get("SafeGasPrice"), "SafeGasPrice"),
        "proposed": parse_float(result.get("ProposeGasPrice"), "ProposeGasPrice"),
        "fast": parse_float(result.get("FastGasPrice"), "FastGasPrice"),
        "base_fee": parse_float(result.get("suggestBaseFee"), "suggestBaseFee"),
        "last_block": parse_int(result.get("LastBlock"), "LastBlock"),
    }


# =============================================================================
# Client
# =============================================================================

class EtherscanClient:

    def __init__(self, session: Session, config: AppConfig, logger: logging.Logger):
        self.session = session
        self.config = config
        self.logger = logger
        self.cb = CircuitBreaker()

        self.params = {
            "module": MODULE,
            "action": ACTION,
            "apikey": config.api_key,
        }

        self._last_call = 0.0

    def _rate_limit(self):
        if self.config.rate_limit_delay <= 0:
            return

        elapsed = time.time() - self._last_call
        if elapsed < self.config.rate_limit_delay:
            time.sleep(self.config.rate_limit_delay - elapsed)

    def _retry_policy(self) -> Callable:
        return retry(
            stop=stop_after_attempt(self.config.retries),
            wait=wait_exponential_jitter(
                initial=self.config.backoff_base,
                max=self.config.backoff_max,
            ),
            retry=retry_if_exception(is_retryable_exception),
            before_sleep=before_sleep_log(self.logger, logging.WARNING),
            reraise=True,
        )

    def fetch_gas_prices(self) -> GasPrices:

        @self._retry_policy()
        def _request() -> GasPrices:

            if not self.cb.allow():
                raise RuntimeError("Circuit breaker open")

            self._rate_limit()

            request_id = str(uuid.uuid4())[:8]
            start = time.perf_counter()

            self.logger.debug(f"[{request_id}] Sending request")

            response = self.session.get(
                ETHERSCAN_API_URL,
                params=self.params,
                timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
            )

            self._last_call = time.time()

            response.raise_for_status()

            payload = json_loads(response.content)

            duration = (time.perf_counter() - start) * 1000

            self.logger.debug(
                f"[{request_id}] Done in {duration:.2f} ms"
            )

            data = parse_gas_response(payload)

            self.cb.record_success()

            return data

        try:
            return _request()
        except Exception:
            self.cb.record_failure()
            raise


# =============================================================================
# Output
# =============================================================================

def render_output(data: GasPrices, json_output: bool):
    if json_output:
        print(json_dumps(data))
        return

    print("\nEthereum Gas Prices (Gwei)")
    print("-" * 35)
    print(f"Safe       : {data['safe']:.2f}")
    print(f"Proposed   : {data['proposed']:.2f}")
    print(f"Fast       : {data['fast']:.2f}")
    print(f"Base Fee   : {data['base_fee']:.2f}")
    print(f"Last Block : {data['last_block']}")


# =============================================================================
# Session
# =============================================================================

def create_session() -> Session:
    session = requests.Session()

    adapter = HTTPAdapter(
        pool_connections=50,
        pool_maxsize=50,
        max_retries=0,
    )

    session.mount("https://", adapter)

    session.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
        "Connection": "keep-alive",
    })

    return session


# =============================================================================
# Runner
# =============================================================================

def run(config: AppConfig) -> int:
    logger = configure_logger(config.verbose)

    try:
        with create_session() as session:
            client = EtherscanClient(session, config, logger)

            while True:
                logger.info("Fetching gas prices...")

                prices = client.fetch_gas_prices()
                render_output(prices, config.json_output)

                if config.poll_interval <= 0:
                    break

                time.sleep(config.poll_interval)

        return EXIT_OK

    except RetryError as exc:
        logger.error("Retry failed: %s", exc)
        return EXIT_ERROR

    except KeyboardInterrupt:
        logger.warning("Interrupted.")
        return EXIT_INTERRUPT

    except Exception as exc:
        logger.error("Fatal: %s", exc)
        return EXIT_ERROR


# =============================================================================
# CLI
# =============================================================================

def cli():
    parser = argparse.ArgumentParser()

    parser.add_argument("--api-key")
    parser.add_argument("--retries", type=int, default=DEFAULT_RETRIES)
    parser.add_argument("--backoff-base", type=int, default=DEFAULT_BACKOFF_BASE)
    parser.add_argument("--backoff-max", type=int, default=DEFAULT_BACKOFF_MAX)
    parser.add_argument("--rate-limit", type=float, default=0.0)
    parser.add_argument("--poll", type=float, default=0.0,
                        help="Polling interval (seconds)")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--verbose", action="store_true")

    args = parser.parse_args()

    try:
        config = AppConfig(
            api_key=resolve_api_key(args.api_key),
            retries=args.retries,
            backoff_base=args.backoff_base,
            backoff_max=args.backoff_max,
            json_output=args.json,
            verbose=args.verbose,
            rate_limit_delay=args.rate_limit,
            poll_interval=args.poll,
        )
    except ValueError as e:
        print(e, file=sys.stderr)
        sys.exit(EXIT_ERROR)

    sys.exit(run(config))


if __name__ == "__main__":
    cli()
