import os
import logging
import argparse
import requests
from typing import Dict, Optional, Callable
from requests.exceptions import Timeout, RequestException, HTTPError
from tenacity import retry, stop_after_attempt, wait_exponential, before_log, RetryError

# Constants
ETHERSCAN_API_URL = "https://api.etherscan.io/api"
DEFAULT_RETRY_ATTEMPTS = 3
DEFAULT_RETRY_BACKOFF_BASE = 1  # seconds
DEFAULT_RETRY_BACKOFF_MAX = 4   # seconds
DEFAULT_TIMEOUT = 10  # seconds


def configure_logging(verbose: bool = False) -> logging.Logger:
    """Configure and return the logger."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s - %(levelname)s - %(message)s"
    )
    return logging.getLogger("EtherscanGasTracker")


def validate_api_key(api_key: Optional[str]) -> None:
    """Raise an error if API key is missing."""
    if not api_key:
        raise ValueError("Missing Etherscan API key. Provide --api_key or set ETHERSCAN_API_KEY.")


def parse_gas_response(response_json: dict) -> Dict[str, str]:
    """
    Extract gas price data from Etherscan API response.
    Raises ValueError if the response format is invalid.
    """
    if response_json.get("status") != "1" or "result" not in response_json:
        error_msg = response_json.get("message", "Unknown error")
        raise ValueError(f"Etherscan API returned an error: {error_msg}")

    result = response_json["result"]
    return {
        "Safe": result.get("SafeGasPrice"),
        "Proposed": result.get("ProposeGasPrice"),
        "Fast": result.get("FastGasPrice")
    }


def make_gas_fee_request(api_key: str, timeout: int) -> Dict[str, str]:
    """
    Perform the actual HTTP request to Etherscan API for gas fees.
    Raises ConnectionError or RuntimeError on failure.
    """
    params = {
        "module": "gastracker",
        "action": "gasoracle",
        "apikey": api_key
    }

    try:
        response = requests.get(ETHERSCAN_API_URL, params=params, timeout=timeout)
        response.raise_for_status()
        return parse_gas_response(response.json())
    except Timeout:
        raise ConnectionError("Etherscan request timed out.")
    except HTTPError as e:
        raise ConnectionError(f"HTTP error {e.response.status_code}: {e}")
    except RequestException as e:
        raise ConnectionError(f"Request exception: {e}")
    except Exception as e:
        raise RuntimeError(f"Unexpected error: {e}")


def retry_decorator(logger: logging.Logger, attempts: int, backoff_base: int, backoff_max: int) -> Callable:
    """
    Create a tenacity retry decorator with logging.
    """
    return retry(
        stop=stop_after_attempt(attempts),
        wait=wait_exponential(multiplier=backoff_base, max=backoff_max),
        before=before_log(logger, logging.WARNING),
        reraise=True
    )


def fetch_gas_fees(api_key: str, logger: logging.Logger, timeout: int, attempts: int, backoff_base: int, backoff_max: int) -> Dict[str, str]:
    """
    Fetch gas fees with retry logic.
    """
    decorated_call = retry_decorator(logger, attempts, backoff_base, backoff_max)(lambda: make_gas_fee_request(api_key, timeout))
    return decorated_call()


def main(api_key: str, verbose: bool = False, timeout: int = DEFAULT_TIMEOUT, attempts: int = DEFAULT_RETRY_ATTEMPTS,
         backoff_base: int = DEFAULT_RETRY_BACKOFF_BASE, backoff_max: int = DEFAULT_RETRY_BACKOFF_MAX) -> None:
    """
    Main execution function.
    """
    logger = configure_logging(verbose)
    validate_api_key(api_key)

    try:
        logger.info("Fetching Ethereum gas prices from Etherscan...")
        gas_fees = fetch_gas_fees(api_key, logger, timeout, attempts, backoff_base, backoff_max)
        logger.info("Ethereum Gas Prices (Gwei):")
        for key, value in gas_fees.items():
            logger.info(f"  {key}: {value}")
    except RetryError as e:
        logger.error(f"Failed after {attempts} retries: {e.last_attempt.exception()}")
    except Exception as e:
        logger.error(f"Error fetching gas fees: {e}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Fetch Ethereum gas fees from Etherscan with retries.")
    parser.add_argument("--api_key", type=str, help="Etherscan API key or set ETHERSCAN_API_KEY env variable")
    parser.add_argument("--verbose", action="store_true", help="Enable debug logging")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="Request timeout in seconds")
    parser.add_argument("--retries", type=int, default=DEFAULT_RETRY_ATTEMPTS, help="Number of retry attempts")
    parser.add_argument("--backoff_base", type=int, default=DEFAULT_RETRY_BACKOFF_BASE, help="Base backoff time in seconds")
    parser.add_argument("--backoff_max", type=int, default=DEFAULT_RETRY_BACKOFF_MAX, help="Max backoff time in seconds")
    args = parser.parse_args()

    api_key = args.api_key or os.getenv("ETHERSCAN_API_KEY")
    main(api_key, verbose=args.verbose, timeout=args.timeout, attempts=args.retries,
         backoff_base=args.backoff_base, backoff_max=args.backoff_max)
