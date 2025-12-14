#!/usr/bin/env python3
"""
Etherscan Gas Tracker
--------------------
Fetches and displays Ethereum gas prices using the Etherscan API.

Features:
- Clean, testable architecture
- Explicit configuration via dataclass
- Robust retry logic for transient failures
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
from typing import Dict, Optional, Any

import requests
from requests.exceptions import Timeout, RequestException, HTTPError
from tenacity import retry, stop_after_attempt, wait_exponential, before_log, retry_if_exception_type, RetryError


# === Constants ===============================================================

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


# === Configuration ===========================================================

@dataclass(frozen=True)
class AppConfig:
    api_key: str
    timeout: int
    retries: int
    backoff_base: int
    backoff_max: int
    json_output: bool
    verbose: bool


# === Logging ================================================================

def configure_logger(verbose: bool) -> logging.Logger:
    logger = logging.getLogger("etherscan_gas_tracker")

    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter(
            "%(asctime)s | %(levelname)-8s | %(message)s",
            "%H:%M:%S",
        ))
        logger.addHandler(handler)

    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    logger.propagate = False
    return logger


# === Utilities ==============================================================

def resolve_api_key(cli_key: Optional[str]) -> str:
    key = cli_key or os.getenv("ETHERSCAN_API_KEY")
    if not key:
        raise ValueError("Etherscan API key is missing (CLI or ETHERSCAN_API_KEY).")
    return key


def parse_gas_response(payload: Dict[str, Any]) -> Dict[str, str]:
    if payload.get("status") != "1":
        message = payload.get("message", "Unknown error")
        result = payload.get("result", "")
        raise ValueError(f"Etherscan API error: {message} ({result})")

    result = payload["result"]
    return {
        "SafeGasPrice": result.get("SafeGasPrice", "N/A"),
        "ProposeGasPrice": result.get("ProposeGasPrice", "N/A"),
        "FastGasPrice": result.get("FastGasPrice", "N/A"),
        "BaseFee": result.get("suggestBaseFee", "N/A"),
        "LastBlock": result.get("LastBlock", "N/A"),
    }


# === Networking =============================================================

def retry_policy(logger: logging.Logger, retries: int, base: int, max_wait: int):
    return retry(
        stop=stop_after_attempt(retries),
        wait=wait_exponential(multiplier=base, max=max_wait),
        retry=retry_if_exception_type((Timeout, RequestException)),
        before=before_log(logger, logging.WARNING),
        reraise=True,
    )


def fetch_gas_prices(
    session: requests.Session,
    config: AppConfig,
    logger: logging.Logger,
) -> Dict[str, str]:

    @retry_policy(logger, config.retries, config.backoff_base, config.backoff_max)
    def _request() -> Dict[str, str]:
        params = {
            "module": MODULE,
            "action": ACTION,
            "apikey": config.api_key,
        }

        logger.debug("Request params: %s", params)

        response = session.get(
            ETHERSCAN_API_URL,
            params=params,
            timeout=config.timeout,
        )
        response.raise_for_status()

        try:
            data = response.json()
        except ValueError as e:
            raise ValueError("Invalid JSON received from Etherscan") from e

        logger.debug("Raw API response: %s", data)
        return parse_gas_response(data)

    return _request()


# === Output =================================================================

def render_output(data: Dict[str, str], json_output: bool, logger: logging.Logger) -> None:
    if json_output:
        print(json.dumps(data, indent=2))
        return

    logger.info("Ethereum Gas Prices (Gwei)")
    for key, value in data.items():
        logger.info("  %-14s : %s", key, value)


# === Main ===================================================================

def run(config: AppConfig) -> int:
    logger = configure_logger(config.verbose)

    try:
        logger.info("Fetching Ethereum gas prices...")

        with requests.Session() as session:
            session.headers["User-Agent"] = "EtherscanGasTracker/1.2"
            prices = fetch_gas_prices(session, config, logger)

        render_output(prices, config.json_output, logger)
        return EXIT_OK

    except RetryError as e:
        logger.error("Request failed after %d retries: %s", config.retries, e.last_attempt.exception())
        return EXIT_ERROR

    except KeyboardInterrupt:
        logger.warning("Interrupted by user.")
        return EXIT_INTERRUPT

    except Exception as e:
        logger.error("Fatal error: %s", e)
        return EXIT_ERROR


# === CLI ====================================================================

def cli() -> None:
    parser = argparse.ArgumentParser(
        description="Fetch Ethereum gas prices from Etherscan.",
    )

    parser.add_argument("--api_key", help="Etherscan API key (or env ETHERSCAN_API_KEY)")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="Request timeout (seconds)")
    parser.add_argument("--retries", type=int, default=DEFAULT_RETRIES, help="Retry attempts")
    parser.add_argument("--backoff_base", type=int, default=DEFAULT_BACKOFF_BASE, help="Backoff base multiplier")
    parser.add_argument("--backoff_max", type=int, default=DEFAULT_BACKOFF_MAX, help="Max backoff delay (seconds)")
    parser.add_argument("--json", action="store_true", help="Output raw JSON")
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
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(EXIT_ERROR)

    sys.exit(run(config))


if __name__ == "__main__":
    cli()
