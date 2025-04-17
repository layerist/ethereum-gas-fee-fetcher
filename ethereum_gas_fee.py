import os
import logging
import argparse
import requests
from typing import Dict, Optional
from requests.exceptions import Timeout, RequestException, HTTPError
from tenacity import retry, stop_after_attempt, wait_exponential, before_log

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# Constants
ETHERSCAN_API_URL = "https://api.etherscan.io/api"
RETRY_ATTEMPTS = 3
RETRY_BACKOFF_BASE = 1  # in seconds
RETRY_BACKOFF_MAX = 4  # in seconds


def validate_api_key(api_key: Optional[str]) -> None:
    """Ensure that an API key is provided."""
    if not api_key:
        raise ValueError("Missing Etherscan API key. Use --api_key or set ETHERSCAN_API_KEY environment variable.")


def parse_gas_data(data: dict) -> Dict[str, str]:
    """Parses and returns gas fee data."""
    result = data.get("result")
    if data.get("status") != "1" or not result:
        raise ValueError(f"Etherscan error: {data.get('message', 'Unknown error')}")
    
    return {
        "SafeGasPrice": result["SafeGasPrice"],
        "ProposeGasPrice": result["ProposeGasPrice"],
        "FastGasPrice": result["FastGasPrice"]
    }


def request_gas_fees(api_key: str, timeout: int = 10) -> Dict[str, str]:
    """Sends the request to the Etherscan API and returns the gas fee data."""
    params = {
        "module": "gastracker",
        "action": "gasoracle",
        "apikey": api_key
    }

    try:
        logger.debug("Sending request to Etherscan...")
        response = requests.get(ETHERSCAN_API_URL, params=params, timeout=timeout)
        response.raise_for_status()
        return parse_gas_data(response.json())

    except Timeout:
        raise ConnectionError("Request to Etherscan API timed out.")
    except HTTPError as e:
        raise ConnectionError(f"HTTP error: {e}")
    except RequestException as e:
        raise ConnectionError(f"Request failed: {e}")
    except Exception as e:
        raise RuntimeError(f"Unexpected error: {e}")


@retry(
    stop=stop_after_attempt(RETRY_ATTEMPTS),
    wait=wait_exponential(multiplier=RETRY_BACKOFF_BASE, max=RETRY_BACKOFF_MAX),
    before=before_log(logger, logging.INFO),
    reraise=True
)
def fetch_gas_fees(api_key: str, timeout: int = 10) -> Dict[str, str]:
    """Fetch Ethereum gas fees with retries on failure."""
    return request_gas_fees(api_key, timeout)


def main(api_key: str) -> None:
    """Main logic to fetch and display Ethereum gas prices."""
    try:
        gas_fees = fetch_gas_fees(api_key)
        logger.info("Ethereum Gas Prices:")
        logger.info(f"  Safe: {gas_fees['SafeGasPrice']} Gwei")
        logger.info(f"  Proposed: {gas_fees['ProposeGasPrice']} Gwei")
        logger.info(f"  Fast: {gas_fees['FastGasPrice']} Gwei")
    except Exception as e:
        logger.error(f"Failed to retrieve gas fees: {e}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Fetch Ethereum gas fees from Etherscan.")
    parser.add_argument(
        "--api_key",
        type=str,
        help="Etherscan API key (or set the ETHERSCAN_API_KEY environment variable)"
    )
    args = parser.parse_args()
    api_key = args.api_key or os.getenv("ETHERSCAN_API_KEY")

    if not api_key:
        logger.error("API key is required.")
        exit(1)

    main(api_key)
