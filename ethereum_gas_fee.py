import os
import sys
import json
import logging
import argparse
import requests
from typing import Dict, Optional, Any
from requests.exceptions import Timeout, RequestException, HTTPError
from tenacity import retry, stop_after_attempt, wait_exponential, before_log, RetryError

# Constants
ETHERSCAN_API_URL = "https://api.etherscan.io/api"
DEFAULT_TIMEOUT = 10
DEFAULT_RETRIES = 3
DEFAULT_BACKOFF_BASE = 1
DEFAULT_BACKOFF_MAX = 4

MODULE = "gastracker"
ACTION = "gasoracle"


def configure_logger(verbose: bool = False) -> logging.Logger:
    """Configure and return a logger instance."""
    logger = logging.getLogger("etherscan_gas_tracker")
    if not logger.handlers:
        handler = logging.StreamHandler()
        formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
        handler.setFormatter(formatter)
        logger.addHandler(handler)

    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    return logger


def get_api_key(provided_key: Optional[str]) -> str:
    """Get API key from argument or environment variable."""
    key = provided_key or os.getenv("ETHERSCAN_API_KEY")
    if not key:
        raise ValueError("Etherscan API key is required. Use --api_key or set ETHERSCAN_API_KEY.")
    return key


def parse_gas_data(response: Dict[str, Any]) -> Dict[str, str]:
    """Parse the gas price data from Etherscan response."""
    if response.get("status") != "1" or "result" not in response:
        raise ValueError(f"Etherscan error: {response.get('message', 'Unknown error')}")

    result = response["result"]
    return {
        "Safe": result.get("SafeGasPrice", "N/A"),
        "Proposed": result.get("ProposeGasPrice", "N/A"),
        "Fast": result.get("FastGasPrice", "N/A"),
    }


def build_retry(retries: int, backoff_base: int, backoff_max: int, logger: logging.Logger):
    """Build a retry decorator with exponential backoff."""
    return retry(
        stop=stop_after_attempt(retries),
        wait=wait_exponential(multiplier=backoff_base, max=backoff_max),
        before=before_log(logger, logging.WARNING),
        reraise=True,
    )


def make_fetch_function(session: requests.Session, retries: int, backoff_base: int, backoff_max: int, logger: logging.Logger):
    """Return a function that fetches gas prices with retry support."""

    @build_retry(retries, backoff_base, backoff_max, logger)
    def _fetch(api_key: str, timeout: int) -> Dict[str, str]:
        params = {
            "module": MODULE,
            "action": ACTION,
            "apikey": api_key,
        }
        try:
            logger.debug("Sending request to Etherscan Gas Oracle API...")
            response = session.get(ETHERSCAN_API_URL, params=params, timeout=timeout)
            response.raise_for_status()
            data = response.json()
            logger.debug(f"Raw API response: {data}")
            return parse_gas_data(data)

        except Timeout:
            logger.warning("Request timed out.")
            raise
        except HTTPError as e:
            logger.error(f"HTTP {e.response.status_code} error: {e}")
            raise
        except RequestException as e:
            logger.error(f"Network error: {e}")
            raise
        except Exception as e:
            logger.error(f"Unexpected error: {e}")
            raise

    return _fetch


def display_gas_prices(fees: Dict[str, str], json_output: bool, logger: logging.Logger) -> None:
    """Display gas prices in either JSON or human-readable format."""
    if json_output:
        print(json.dumps(fees, indent=2))
    else:
        logger.info("\nEthereum Gas Prices (Gwei):")
        for tier, price in fees.items():
            logger.info(f"  {tier:8}: {price}")


def main(
    api_key: Optional[str],
    verbose: bool,
    timeout: int,
    retries: int,
    backoff_base: int,
    backoff_max: int,
    json_output: bool,
) -> None:
    logger = configure_logger(verbose)

    try:
        api_key = get_api_key(api_key)
        with requests.Session() as session:
            fetch_gas_data = make_fetch_function(session, retries, backoff_base, backoff_max, logger)

            logger.info("Fetching gas prices from Etherscan...")
            fees = fetch_gas_data(api_key, timeout)
            display_gas_prices(fees, json_output, logger)

    except RetryError as e:
        logger.error(f"Failed after {retries} attempts: {e.last_attempt.exception()}")
        sys.exit(1)
    except Exception as e:
        logger.error(f"Error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Fetch Ethereum gas prices from Etherscan.")
    parser.add_argument("--api_key", type=str, help="Etherscan API key (or set ETHERSCAN_API_KEY env var)")
    parser.add_argument("--verbose", action="store_true", help="Enable debug logging")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="Request timeout in seconds")
    parser.add_argument("--retries", type=int, default=DEFAULT_RETRIES, help="Retry attempts on failure")
    parser.add_argument("--backoff_base", type=int, default=DEFAULT_BACKOFF_BASE, help="Backoff multiplier")
    parser.add_argument("--backoff_max", type=int, default=DEFAULT_BACKOFF_MAX, help="Maximum backoff seconds")
    parser.add_argument("--json", action="store_true", help="Output gas prices as JSON")
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
