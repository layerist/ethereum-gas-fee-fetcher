#!/usr/bin/env python3
"""
Etherscan Gas Tracker
---------------------
Fetches and displays real-time Ethereum gas prices from the Etherscan API.

Features:
- Configurable retries with exponential backoff
- JSON or human-readable output
- Structured logging and graceful error handling
"""

import os
import sys
import json
import logging
import argparse
from typing import Dict, Optional, Any, Callable

import requests
from requests.exceptions import Timeout, RequestException, HTTPError
from tenacity import retry, stop_after_attempt, wait_exponential, before_log, RetryError


# === Constants ===
ETHERSCAN_API_URL = "https://api.etherscan.io/api"
DEFAULT_TIMEOUT = 10
DEFAULT_RETRIES = 3
DEFAULT_BACKOFF_BASE = 1
DEFAULT_BACKOFF_MAX = 4
MODULE = "gastracker"
ACTION = "gasoracle"


# === Logging ===
def configure_logger(verbose: bool = False) -> logging.Logger:
    """Create and configure a console logger."""
    logger = logging.getLogger("etherscan_gas_tracker")
    logger.propagate = False  # Avoid duplicate logs in some environments

    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        formatter = logging.Formatter(
            "%(asctime)s | %(levelname)-8s | %(message)s", "%H:%M:%S"
        )
        handler.setFormatter(formatter)
        logger.addHandler(handler)

    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    return logger


# === API Key ===
def get_api_key(provided_key: Optional[str]) -> str:
    """Return the Etherscan API key from CLI argument or environment variable."""
    key = provided_key or os.getenv("ETHERSCAN_API_KEY")
    if not key:
        raise ValueError("Etherscan API key is missing. Use --api_key or set ETHERSCAN_API_KEY.")
    return key


# === Response Parsing ===
def parse_gas_data(response: Dict[str, Any]) -> Dict[str, str]:
    """Parse and return gas price tiers from Etherscan API response."""
    if response.get("status") != "1" or "result" not in response:
        message = response.get("message", "Unknown error")
        raise ValueError(f"Etherscan API error: {message}")

    result = response["result"]
    return {
        "Safe": result.get("SafeGasPrice", "N/A"),
        "Proposed": result.get("ProposeGasPrice", "N/A"),
        "Fast": result.get("FastGasPrice", "N/A"),
        "BaseFee": result.get("suggestBaseFee", "N/A"),
        "LastBlock": result.get("LastBlock", "N/A"),
    }


# === Retry Decorator Builder ===
def build_retry_decorator(
    retries: int,
    backoff_base: int,
    backoff_max: int,
    logger: logging.Logger,
) -> Callable:
    """Return a retry decorator configured with exponential backoff."""
    return retry(
        stop=stop_after_attempt(retries),
        wait=wait_exponential(multiplier=backoff_base, max=backoff_max),
        before=before_log(logger, logging.WARNING),
        reraise=True,
    )


# === Fetch Function Factory ===
def make_fetch_function(
    session: requests.Session,
    retries: int,
    backoff_base: int,
    backoff_max: int,
    logger: logging.Logger,
) -> Callable[[str, int], Dict[str, str]]:
    """Return a function that fetches gas prices from Etherscan with retry support."""

    @build_retry_decorator(retries, backoff_base, backoff_max, logger)
    def _fetch(api_key: str, timeout: int) -> Dict[str, str]:
        params = {"module": MODULE, "action": ACTION, "apikey": api_key}
        logger.debug(f"Requesting gas data from Etherscan with params: {params}")

        try:
            response = session.get(ETHERSCAN_API_URL, params=params, timeout=timeout)
            response.raise_for_status()

            data = response.json()
            logger.debug(f"Raw API response: {data}")
            return parse_gas_data(data)

        except Timeout:
            logger.warning(f"Request timed out after {timeout}s.")
            raise
        except HTTPError as e:
            logger.error(f"HTTP error {e.response.status_code}: {e}")
            raise
        except RequestException as e:
            logger.error(f"Network error: {e}")
            raise
        except json.JSONDecodeError as e:
            logger.error(f"Failed to decode JSON: {e}")
            raise
        except Exception as e:
            logger.exception(f"Unexpected error while fetching gas data: {e}")
            raise

    return _fetch


# === Display ===
def display_gas_prices(fees: Dict[str, str], json_output: bool, logger: logging.Logger) -> None:
    """Display gas prices in JSON or text format."""
    if json_output:
        print(json.dumps(fees, indent=2))
    else:
        logger.info("\nEthereum Gas Prices (Gwei):")
        for key, value in fees.items():
            logger.info(f"  {key:<10}: {value}")


# === Main ===
def main(
    api_key: Optional[str],
    verbose: bool,
    timeout: int,
    retries: int,
    backoff_base: int,
    backoff_max: int,
    json_output: bool,
) -> None:
    """Main entry point for the CLI tool."""
    logger = configure_logger(verbose)

    try:
        api_key = get_api_key(api_key)
        with requests.Session() as session:
            session.headers.update({"User-Agent": "EtherscanGasTracker/1.0"})
            fetch_gas_data = make_fetch_function(session, retries, backoff_base, backoff_max, logger)

            logger.info("Fetching Ethereum gas prices from Etherscan...")
            fees = fetch_gas_data(api_key, timeout)
            display_gas_prices(fees, json_output, logger)

    except RetryError as e:
        last_exc = e.last_attempt.exception()
        logger.error(f"Failed after {retries} retries: {last_exc}")
        sys.exit(1)
    except KeyboardInterrupt:
        logger.warning("Operation cancelled by user.")
        sys.exit(130)
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        sys.exit(1)


# === CLI Entrypoint ===
def cli() -> None:
    """CLI argument parser and runner."""
    parser = argparse.ArgumentParser(
        description="Fetch and display current Ethereum gas prices using the Etherscan API."
    )
    parser.add_argument("--api_key", type=str, help="Etherscan API key (or set ETHERSCAN_API_KEY)")
    parser.add_argument("--verbose", action="store_true", help="Enable verbose (debug) logging")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="Request timeout in seconds")
    parser.add_argument("--retries", type=int, default=DEFAULT_RETRIES, help="Retry attempts on failure")
    parser.add_argument("--backoff_base", type=int, default=DEFAULT_BACKOFF_BASE, help="Exponential backoff base")
    parser.add_argument("--backoff_max", type=int, default=DEFAULT_BACKOFF_MAX, help="Maximum backoff duration (seconds)")
    parser.add_argument("--json", action="store_true", help="Output as JSON instead of plain text")

    args = parser.parse_args()

    main(
        api_key=args.api_key,
        verbose=args.verbose,
        timeout=args.timeout,
        retries=args.retries,
        backoff_base=args.backoff_base,
        backoff_max=args.backoff_max,
        json_output=args.json,
    )


if __name__ == "__main__":
    cli()
