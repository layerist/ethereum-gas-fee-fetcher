#!/usr/bin/env python3
"""
Etherscan Gas Tracker
--------------------
Fetches and displays Ethereum gas prices using the Etherscan API.

Features:
- Clean, testable architecture
- Explicit configuration via dataclass
- Robust retry logic with exponential backoff
- Structured logging without duplicate handlers
- JSON or human-readable output
"""

from __future__ import annotations

import os
import sys
import json
import logging
import argparse
from dataclasses import dataclass
from typing import Dict, Any, Optional

import requests
from requests import Session
from requests.exceptions import Timeout, RequestException, HTTPError
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    before_log,
    retry_if_exception,
    RetryError,
)


# =============================================================================
# Constants
# =============================================================================

ETHERSCAN_API_URL = "https://api.etherscan.io/api"
MODULE = "gastracker"
ACTION = "gasoracle"

DEFAULT_TIMEOUT = 10
DEFAULT_RETRIES = 3
DEFAULT_BACKOFF_BASE = 1
DEFAULT_BACKOFF_MAX = 4

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_INTERRUPT = 130


# =============================================================================
# Configuration
# =============================================================================

@dataclass(frozen=True)
class AppConfig:
    api_key: str
    timeout: int = DEFAULT_TIMEOUT
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

    if not logger.handlers:
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
        raise ValueError("Etherscan API key is missing (CLI or ETHERSCAN_API_KEY).")
    return api_key


def is_retryable_exception(exc: Exception) -> bool:
    if isinstance(exc, Timeout):
        return True
    if isinstance(exc, HTTPError):
        # Retry only on 5xx
        return exc.response is not None and exc.response.status_code >= 500
    return isinstance(exc, RequestException)


def parse_gas_response(payload: Dict[str, Any]) -> Dict[str, str]:
    if payload.get("status") != "1":
        raise ValueError(
            f"Etherscan API error: {payload.get('message')} "
            f"({payload.get('result')})"
        )

    result = payload.get("result", {})
    return {
        "SafeGasPrice": result.get("SafeGasPrice", "N/A"),
        "ProposeGasPrice": result.get("ProposeGasPrice", "N/A"),
        "FastGasPrice": result.get("FastGasPrice", "N/A"),
        "BaseFee": result.get("suggestBaseFee", "N/A"),
        "LastBlock": result.get("LastBlock", "N/A"),
    }


# =============================================================================
# Etherscan Client
# =============================================================================

class EtherscanClient:
    def __init__(self, session: Session, config: AppConfig, logger: logging.Logger) -> None:
        self.session = session
        self.config = config
        self.logger = logger

    def _params(self) -> Dict[str, str]:
        return {
            "module": MODULE,
            "action": ACTION,
            "apikey": self.config.api_key,
        }

    def _retry_policy(self):
        return retry(
            stop=stop_after_attempt(self.config.retries),
            wait=wait_exponential(
                multiplier=self.config.backoff_base,
                max=self.config.backoff_max,
            ),
            retry=retry_if_exception(is_retryable_exception),
            before=before_log(self.logger, logging.WARNING),
            reraise=True,
        )

    def fetch_gas_prices(self) -> Dict[str, str]:
        @self._retry_policy()
        def _request() -> Dict[str, str]:
            self.logger.debug("Request params: %s", self._params())

            response = self.session.get(
                ETHERSCAN_API_URL,
                params=self._params(),
                timeout=self.config.timeout,
            )
            response.raise_for_status()

            try:
                payload = response.json()
            except ValueError as exc:
                raise ValueError("Invalid JSON received from Etherscan") from exc

            self.logger.debug("Raw API response: %s", payload)
            return parse_gas_response(payload)

        return _request()


# =============================================================================
# Output
# =============================================================================

def render_output(data: Dict[str, str], json_output: bool, logger: logging.Logger) -> None:
    if json_output:
        print(json.dumps(data, indent=2))
        return

    logger.info("Ethereum Gas Prices (Gwei)")
    for key, value in data.items():
        logger.info("  %-16s : %s", key, value)


# =============================================================================
# Main Execution
# =============================================================================

def run(config: AppConfig) -> int:
    logger = configure_logger(config.verbose)

    try:
        logger.info("Fetching Ethereum gas prices...")

        with requests.Session() as session:
            session.headers.update({
                "User-Agent": "EtherscanGasTracker/1.3",
                "Accept": "application/json",
            })

            client = EtherscanClient(session, config, logger)
            prices = client.fetch_gas_prices()

        render_output(prices, config.json_output, logger)
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

def cli() -> None:
    parser = argparse.ArgumentParser(
        description="Fetch Ethereum gas prices from Etherscan.",
    )

    parser.add_argument("--api-key", help="Etherscan API key (or env ETHERSCAN_API_KEY)")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    parser.add_argument("--retries", type=int, default=DEFAULT_RETRIES)
    parser.add_argument("--backoff-base", type=int, default=DEFAULT_BACKOFF_BASE)
    parser.add_argument("--backoff-max", type=int, default=DEFAULT_BACKOFF_MAX)
    parser.add_argument("--json", action="store_true", help="Output JSON")
    parser.add_argument("--verbose", action="store_true", help="Enable debug logging")

    args = parser.parse_args()

    try:
        config = AppConfig(
            api_key=resolve_api_key(args.api_key),
            timeout=args.timeout,
            retries=args.retries,
            backoff_base=args.backoff_base,
            backoff_max=args.backoff_max,
            json_output=args.json,
            verbose=args.verbose,
        )
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(EXIT_ERROR)

    sys.exit(run(config))


if __name__ == "__main__":
    cli()
