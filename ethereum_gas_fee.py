import os
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
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s - %(levelname)s - %(message)s"
    )
    return logging.getLogger("etherscan_gas_tracker")


def ensure_api_key(api_key: Optional[str]) -> str:
    if not api_key:
        raise ValueError("Missing Etherscan API key. Use --api_key or set ETHERSCAN_API_KEY env var.")
    return api_key


def parse_gas_data(data: dict) -> Dict[str, str]:
    if data.get("status") != "1" or "result" not in data:
        raise ValueError(f"Etherscan API error: {data.get('message', 'Unknown error')}")
    
    result = data["result"]
    return {
        "Safe": result.get("SafeGasPrice", "N/A"),
        "Proposed": result.get("ProposeGasPrice", "N/A"),
        "Fast": result.get("FastGasPrice", "N/A")
    }


def request_gas_fees(api_key: str, timeout: int) -> Dict[str, str]:
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
        raise ConnectionError("Request to Etherscan timed out.")
    except HTTPError as e:
        raise ConnectionError(f"HTTP error {e.response.status_code}: {e}")
    except RequestException as e:
        raise ConnectionError(f"Request failed: {e}")
    except Exception as e:
        raise RuntimeError(f"Unexpected error: {e}")


def build_retry(logger: logging.Logger, retries: int, backoff_base: int, backoff_max: int) -> Callable:
    return retry(
        stop=stop_after_attempt(retries),
        wait=wait_exponential(multiplier=backoff_base, max=backoff_max),
        before=before_log(logger, logging.WARNING),
        reraise=True
    )


def get_gas_fees_with_retry(api_key: str, logger: logging.Logger, timeout: int,
                            retries: int, backoff_base: int, backoff_max: int) -> Dict[str, str]:
    retry_call = build_retry(logger, retries, backoff_base, backoff_max)(
        lambda: request_gas_fees(api_key, timeout)
    )
    return retry_call()


def main(api_key: str, verbose: bool, timeout: int, retries: int,
         backoff_base: int, backoff_max: int, json_output: bool) -> None:
    logger = configure_logger(verbose)
    ensure_api_key(api_key)

    try:
        logger.info("Requesting gas prices from Etherscan...")
        fees = get_gas_fees_with_retry(api_key, logger, timeout, retries, backoff_base, backoff_max)
        
        if json_output:
            import json
            print(json.dumps(fees, indent=2))
        else:
            logger.info("Ethereum Gas Prices (Gwei):")
            for label, price in fees.items():
                logger.info(f"  {label}: {price}")
    except RetryError as e:
        logger.error(f"Failed after {retries} retries: {e.last_attempt.exception()}")
    except Exception as e:
        logger.error(f"Failed to fetch gas fees: {e}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Retrieve Ethereum gas prices from Etherscan with retries.")
    parser.add_argument("--api_key", type=str, help="Etherscan API key (or set ENV ETHERSCAN_API_KEY)")
    parser.add_argument("--verbose", action="store_true", help="Enable debug output")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="Timeout in seconds")
    parser.add_argument("--retries", type=int, default=DEFAULT_RETRIES, help="Retry attempts")
    parser.add_argument("--backoff_base", type=int, default=DEFAULT_BACKOFF_BASE, help="Backoff base (sec)")
    parser.add_argument("--backoff_max", type=int, default=DEFAULT_BACKOFF_MAX, help="Backoff max (sec)")
    parser.add_argument("--json", action="store_true", help="Print output as JSON")
    args = parser.parse_args()

    key = args.api_key or os.getenv("ETHERSCAN_API_KEY")
    main(key, args.verbose, args.timeout, args.retries, args.backoff_base, args.backoff_max, args.json)
