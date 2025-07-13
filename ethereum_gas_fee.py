import os
import json
import logging
import argparse
import requests
from typing import Dict, Optional, Callable
from requests.exceptions import Timeout, RequestException, HTTPError
from tenacity import retry, stop_after_attempt, wait_exponential, before_log, RetryError

# Constants
ETHERSCAN_API_URL = "https://api.etherscan.io/api"
DEFAULT_TIMEOUT = 10
DEFAULT_RETRIES = 3
DEFAULT_BACKOFF_BASE = 1
DEFAULT_BACKOFF_MAX = 4


def configure_logger(verbose: bool = False) -> logging.Logger:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s"
    )
    return logging.getLogger("etherscan_gas_tracker")


def get_api_key(provided_key: Optional[str]) -> str:
    api_key = provided_key or os.getenv("ETHERSCAN_API_KEY")
    if not api_key:
        raise ValueError("Missing Etherscan API key. Use --api_key or set ETHERSCAN_API_KEY env var.")
    return api_key


def parse_gas_data(response: dict) -> Dict[str, str]:
    if response.get("status") != "1" or "result" not in response:
        raise ValueError(f"Etherscan API error: {response.get('message', 'Unknown error')}")
    
    result = response["result"]
    return {
        "Safe": result.get("SafeGasPrice", "N/A"),
        "Proposed": result.get("ProposeGasPrice", "N/A"),
        "Fast": result.get("FastGasPrice", "N/A")
    }


def fetch_gas_data(api_key: str, timeout: int) -> Dict[str, str]:
    params = {
        "module": "gastracker",
        "action": "gasoracle",
        "apikey": api_key
    }

    try:
        response = requests.get(ETHERSCAN_API_URL, params=params, timeout=timeout)
        response.raise_for_status()
        return parse_gas_data(response.json())
    except Timeout:
        raise ConnectionError("Request timed out.")
    except HTTPError as e:
        raise ConnectionError(f"HTTP {e.response.status_code}: {e}")
    except RequestException as e:
        raise ConnectionError(f"Request error: {e}")
    except Exception as e:
        raise RuntimeError(f"Unexpected error: {e}")


def build_retry_handler(logger: logging.Logger, retries: int, backoff_base: int, backoff_max: int) -> Callable:
    return retry(
        stop=stop_after_attempt(retries),
        wait=wait_exponential(multiplier=backoff_base, max=backoff_max),
        before=before_log(logger, logging.WARNING),
        reraise=True
    )


def get_gas_fees_with_retry(
    api_key: str, logger: logging.Logger, timeout: int,
    retries: int, backoff_base: int, backoff_max: int
) -> Dict[str, str]:
    retry_fn = build_retry_handler(logger, retries, backoff_base, backoff_max)
    return retry_fn(lambda: fetch_gas_data(api_key, timeout))()


def display_gas_prices(fees: Dict[str, str], json_output: bool) -> None:
    if json_output:
        print(json.dumps(fees, indent=2))
    else:
        print("Ethereum Gas Prices (Gwei):")
        for label, price in fees.items():
            print(f"  {label}: {price}")


def main(
    api_key: Optional[str], verbose: bool, timeout: int, retries: int,
    backoff_base: int, backoff_max: int, json_output: bool
) -> None:
    logger = configure_logger(verbose)

    try:
        api_key = get_api_key(api_key)
        logger.info("Fetching gas prices from Etherscan...")
        fees = get_gas_fees_with_retry(api_key, logger, timeout, retries, backoff_base, backoff_max)
        display_gas_prices(fees, json_output)
    except RetryError as e:
        logger.error(f"Failed after {retries} retries: {e.last_attempt.exception()}")
    except Exception as e:
        logger.error(f"Error: {e}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Fetch Ethereum gas prices from Etherscan with retries.")
    parser.add_argument("--api_key", type=str, help="Etherscan API key (or set ETHERSCAN_API_KEY env var)")
    parser.add_argument("--verbose", action="store_true", help="Enable debug logging")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="HTTP request timeout (seconds)")
    parser.add_argument("--retries", type=int, default=DEFAULT_RETRIES, help="Number of retry attempts")
    parser.add_argument("--backoff_base", type=int, default=DEFAULT_BACKOFF_BASE, help="Exponential backoff base")
    parser.add_argument("--backoff_max", type=int, default=DEFAULT_BACKOFF_MAX, help="Max backoff time (seconds)")
    parser.add_argument("--json", action="store_true", help="Output in JSON format")
    args = parser.parse_args()

    main(
        api_key=args.api_key,
        verbose=args.verbose,
        timeout=args.timeout,
        retries=args.retries,
        backoff_base=args.backoff_base,
        backoff_max=args.backoff_max,
        json_output=args.json
    )
