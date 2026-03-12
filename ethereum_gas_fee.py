#!/usr/bin/env python3
"""
Etherscan Gas Tracker (Production-grade)
----------------------------------------
Fetch Ethereum gas prices using the Etherscan API.

Enhancements:
- Optimized connection pooling
- Strict retry policy (429 + 5xx + network)
- Exponential backoff with jitter
- Split connect/read timeouts
- Immutable request parameters
- Strict response validation
- Faster JSON parsing
"""

from __future__ import annotations

import os
import sys
import json
import logging
import argparse
from dataclasses import dataclass
from typing import Mapping, Any, Optional, TypedDict

import requests
from requests import Session
from requests.adapters import HTTPAdapter
from requests.exceptions import (
    Timeout,
    ConnectTimeout,
    ReadTimeout,
    ConnectionError,
    RequestException,
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

USER_AGENT = "EtherscanGasTracker/3.0"

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
# Utilities
# =============================================================================

def resolve_api_key(cli_key: Optional[str]) -> str:
    api_key = cli_key or os.getenv("ETHERSCAN_API_KEY")

    if not api_key:
        raise ValueError(
            "Missing Etherscan API key. "
            "Use --api-key or set ETHERSCAN_API_KEY."
        )

    return api_key


def is_retryable_exception(exc: Exception) -> bool:
    """
    Retry only network failures or server-side failures.
    """

    if isinstance(exc, (ConnectTimeout, ReadTimeout, ConnectionError)):
        return True

    if isinstance(exc, HTTPError) and exc.response is not None:
        status = exc.response.status_code
        return status == 429 or status >= 500

    return False


def parse_float(value: Any, field: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ValueError(f"Invalid numeric value for '{field}': {value}")


def parse_int(value: Any, field: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ValueError(f"Invalid integer value for '{field}': {value}")


def parse_gas_response(payload: Mapping[str, Any]) -> GasPrices:

    status = payload.get("status")

    if status != "1":
        raise ValueError(
            f"Etherscan API error: {payload.get('message')} "
            f"result={payload.get('result')}"
        )

    result = payload.get("result")

    if not isinstance(result, Mapping):
        raise ValueError("Malformed API response: result is not an object")

    return {
        "safe": parse_float(result.get("SafeGasPrice"), "SafeGasPrice"),
        "proposed": parse_float(result.get("ProposeGasPrice"), "ProposeGasPrice"),
        "fast": parse_float(result.get("FastGasPrice"), "FastGasPrice"),
        "base_fee": parse_float(result.get("suggestBaseFee"), "suggestBaseFee"),
        "last_block": parse_int(result.get("LastBlock"), "LastBlock"),
    }


# =============================================================================
# Etherscan Client
# =============================================================================

class EtherscanClient:

    def __init__(self, session: Session, config: AppConfig, logger: logging.Logger):

        self.session = session
        self.config = config
        self.logger = logger

        self.params: Mapping[str, str] = {
            "module": MODULE,
            "action": ACTION,
            "apikey": config.api_key,
        }

    def _retry_policy(self):

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

            self.logger.debug("Request params: %s", self.params)

            response = self.session.get(
                ETHERSCAN_API_URL,
                params=self.params,
                timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
            )

            response.raise_for_status()

            try:
                payload = json.loads(response.content)
            except Exception as exc:
                raise ValueError("Invalid JSON received from Etherscan") from exc

            self.logger.debug("Raw API response: %s", payload)

            return parse_gas_response(payload)

        return _request()


# =============================================================================
# Output
# =============================================================================

def render_output(data: GasPrices, json_output: bool) -> None:

    if json_output:
        print(json.dumps(data, separators=(",", ":")))
        return

    print("Ethereum Gas Prices (Gwei)")
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
        pool_connections=10,
        pool_maxsize=10,
        max_retries=0,
    )

    session.mount("https://", adapter)

    session.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "Connection": "keep-alive",
        }
    )

    return session


# =============================================================================
# Runner
# =============================================================================

def run(config: AppConfig) -> int:

    logger = configure_logger(config.verbose)

    try:

        logger.info("Fetching Ethereum gas prices...")

        with create_session() as session:

            client = EtherscanClient(session, config, logger)

            prices = client.fetch_gas_prices()

        render_output(prices, config.json_output)

        return EXIT_OK

    except RetryError as exc:

        logger.error(
            "Request failed after %d retries: %s",
            config.retries,
            exc.last_attempt.exception(),
        )

        return EXIT_ERROR

    except KeyboardInterrupt:

        logger.warning("Interrupted by user.")

        return EXIT_INTERRUPT

    except Exception as exc:

        logger.error("Fatal error: %s", exc)

        return EXIT_ERROR


# =============================================================================
# CLI
# =============================================================================

def cli():

    parser = argparse.ArgumentParser(
        description="Fetch Ethereum gas prices from Etherscan."
    )

    parser.add_argument("--api-key")
    parser.add_argument("--retries", type=int, default=DEFAULT_RETRIES)
    parser.add_argument("--backoff-base", type=int, default=DEFAULT_BACKOFF_BASE)
    parser.add_argument("--backoff-max", type=int, default=DEFAULT_BACKOFF_MAX)

    parser.add_argument("--json", action="store_true")
    parser.add_argument("--verbose", action="store_true")

    args = parser.parse_args()

    try:

        config = AppConfig(
            api_key=resolve_api_key(args.api_key),
            retries=max(1, args.retries),
            backoff_base=max(1, args.backoff_base),
            backoff_max=max(1, args.backoff_max),
            json_output=args.json,
            verbose=args.verbose,
        )

    except ValueError as exc:

        print(f"Error: {exc}", file=sys.stderr)

        sys.exit(EXIT_ERROR)

    sys.exit(run(config))


if __name__ == "__main__":
    cli()
