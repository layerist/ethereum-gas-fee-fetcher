import os
import logging
import argparse
import requests
from typing import Dict, Optional
from requests.exceptions import Timeout, RequestException, HTTPError
from tenacity import retry, stop_after_attempt, wait_exponential, before_log

# Constants
ETHERSCAN_API_URL = "https://api.etherscan.io/api"
RETRY_ATTEMPTS = 3
RETRY_BACKOFF_BASE = 1  # Base delay (seconds)
RETRY_BACKOFF_MAX = 4   # Max delay (seconds)


def configure_logging(verbose: bool = False) -> logging.Logger:
    """Configure and return the logger."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s - %(levelname)s - %(message)s"
    )
    return logging.getLogger(__name__)


def validate_api_key(api_key: Optional[str]) -> None:
    """Raise an error if API key is missing."""
    if not api_key:
        raise ValueError("Missing Etherscan API key. Use --api_key or set ETHERSCAN_API_KEY.")


def parse_gas_response(response_json: dict) -> Dict[str, str]:
    """Extract and return gas price data from Etherscan response."""
    if response_json.get("status") != "1" or "result" not in response_json:
        raise ValueError(f"Etherscan API error: {response_json.get('message', 'Unknown error')}")

    result = response_json["result"]
    return {
        "SafeGasPrice": result["SafeGasPrice"],
        "ProposeGasPrice": result["ProposeGasPrice"],
        "FastGasPrice": result["FastGasPrice"]
    }


def make_gas_fee_request(api_key: str, timeout: int = 10) -> Dict[str, str]:
    """Perform the actual API request to fetch gas fees."""
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
        raise ConnectionError("Request timed out.")
    except HTTPError as e:
        raise ConnectionError(f"HTTP error: {e}")
    except RequestException as e:
        raise ConnectionError(f"Request failed: {e}")
    except Exception as e:
        raise RuntimeError(f"Unexpected error: {e}")


def with_retry(logger: logging.Logger):
    """Return a retry decorator configured with logging."""
    return retry(
        stop=stop_after_attempt(RETRY_ATTEMPTS),
        wait=wait_exponential(multiplier=RETRY_BACKOFF_BASE, max=RETRY_BACKOFF_MAX),
        before=before_log(logger, logging.INFO),
        reraise=True
    )


def fetch_gas_fees(api_key: str, logger: logging.Logger, timeout: int = 10) -> Dict[str, str]:
    """Fetch gas fees using a retry mechanism."""
    retry_decorator = with_retry(logger)
    return retry_decorator(lambda: make_gas_fee_request(api_key, timeout))()


def main(api_key: str, verbose: bool = False) -> None:
    """Main execution function."""
    logger = configure_logging(verbose)

    try:
        validate_api_key(api_key)
        gas_fees = fetch_gas_fees(api_key, logger)
        logger.info("Ethereum Gas Prices:")
        logger.info(f"  Safe: {gas_fees['SafeGasPrice']} Gwei")
        logger.info(f"  Proposed: {gas_fees['ProposeGasPrice']} Gwei")
        logger.info(f"  Fast: {gas_fees['FastGasPrice']} Gwei")
    except Exception as e:
        logger.error(f"Failed to fetch gas fees: {e}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Fetch Ethereum gas fees from Etherscan.")
    parser.add_argument("--api_key", type=str, help="Etherscan API key or set ETHERSCAN_API_KEY env variable")
    parser.add_argument("--verbose", action="store_true", help="Enable debug logging")
    args = parser.parse_args()

    api_key = args.api_key or os.getenv("ETHERSCAN_API_KEY")
    main(api_key, verbose=args.verbose)
