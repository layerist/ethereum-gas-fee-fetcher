#!/usr/bin/env python3
"""
Improved Etherscan Gas Tracker
------------------------------
Fetches and displays Ethereum gas prices using the Etherscan API.

Enhancements:
- Cleaner architecture and reduced duplication
- Robust logging without duplicate handlers
- More explicit error handling
- Polished retry logic
- Cleaner CLI and output formatting
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

BASE_PARAMS = {"module": MODULE, "action": ACTION}


# === Logger ===
def configure_logger(verbose: bool = False) -> logging.Logger:
    """Configure and return logger instance."""
    logger = logging.getLogger("etherscan_gas_tracker")

    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter(
            "%(asctime)s | %(levelname)-8s | %(message)s",
            "%H:%M:%S"
        ))
        logger.addHandler(handler)

    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    return logger


# === API Key ===
def get_api_key(provided_key: Optional[str]) -> str:
    """Return API key from CLI or environment."""
    key = provided_key or os.getenv("ETHERSCAN_API_KEY")
    if not key:
        raise ValueError("Missing API key. Use --api_key or set ETHERSCAN_API_KEY.")
    return key


# === Response Parsing ===
def parse_gas_data(response: Dict[str, Any]) -> Dict[str, str]:
    """Extract gas data or raise readable exception."""
    if response.get("status") != "1" or "result" not in response:
        raise ValueError(f"Etherscan API error: {response.get('message', 'Unknown error')}")

    r = response["result"]
    return {
        "SafeGasPrice": r.get("SafeGasPrice", "N/A"),
        "ProposeGasPrice": r.get("ProposeGasPrice", "N/A"),
        "FastGasPrice": r.get("FastGasPrice", "N/A"),
        "BaseFee": r.get("suggestBaseFee", "N/A"),
        "LastBlock": r.get("LastBlock", "N/A"),
    }


# === Retry Decorator Factory ===
def retry_decorator(retries: int, backoff_base: int, backoff_max: int, logger: logging.Logger):
    """Create a retry decorator for network operations."""
    return retry(
        stop=stop_after_attempt(retries),
        wait=wait_exponential(multiplier=backoff_base, max=backoff_max),
        before=before_log(logger, logging.WARNING),
        reraise=True,
    )


# === Fetcher Factory ===
def make_fetcher(
    session: requests.Session,
    retries: int,
    backoff_base: int,
    backoff_max: int,
    logger: logging.Logger,
) -> Callable[[str, int], Dict[str, str]]:

    @retry_decorator(retries, backoff_base, backoff_max, logger)
    def _fetch(api_key: str, timeout: int) -> Dict[str, str]:
        params = {**BASE_PARAMS, "apikey": api_key}
        logger.debug(f"Fetching gas prices with params: {params}")

        try:
            resp = session.get(ETHERSCAN_API_URL, params=params, timeout=timeout)
            resp.raise_for_status()
            data = resp.json()
        except Timeout:
            logger.warning("Request timed out.")
            raise
        except HTTPError as e:
            logger.error(f"HTTP {e.response.status_code}: {e}")
            raise
        except RequestException as e:
            logger.error(f"Network error: {e}")
            raise
        except ValueError:
            logger.error("Invalid JSON from Etherscan.")
            raise

        logger.debug(f"API raw response: {data}")
        return parse_gas_data(data)

    return _fetch


# === Output ===
def display(fees: Dict[str, str], json_output: bool, logger: logging.Logger):
    if json_output:
        print(json.dumps(fees, indent=2))
        return

    logger.info("\nEthereum Gas Prices (Gwei):")
    for k, v in fees.items():
        logger.info(f"  {k:<14}: {v}")


# === Main ===
def main(
    api_key: Optional[str],
    verbose: bool,
    timeout: int,
    retries: int,
    backoff_base: int,
    backoff_max: int,
    json_output: bool,
):
    logger = configure_logger(verbose)

    try:
        api_key = get_api_key(api_key)
        session = requests.Session()
        session.headers["User-Agent"] = "EtherscanGasTracker/1.1"

        fetch = make_fetcher(session, retries, backoff_base, backoff_max, logger)

        logger.info("Requesting Ethereum gas prices...")
        fees = fetch(api_key, timeout)
        display(fees, json_output, logger)

    except RetryError as e:
        logger.error(f"Failed after {retries} retries: {e.last_attempt.exception()}")
        sys.exit(1)
    except KeyboardInterrupt:
        logger.warning("Interrupted by user.")
        sys.exit(130)
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        sys.exit(1)


# === CLI ===
def cli():
    parser = argparse.ArgumentParser(description="Fetch Ethereum gas prices from Etherscan.")
    parser.add_argument("--api_key", help="Provide Etherscan API key or set env ETHERSCAN_API_KEY")
    parser.add_argument("--verbose", action="store_true", help="Enable debug logging")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="Request timeout (s)")
    parser.add_argument("--retries", type=int, default=DEFAULT_RETRIES)
    parser.add_argument("--backoff_base", type=int, default=DEFAULT_BACKOFF_BASE)
    parser.add_argument("--backoff_max", type=int, default=DEFAULT_BACKOFF_MAX)
    parser.add_argument("--json", action="store_true", help="Output JSON")

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
