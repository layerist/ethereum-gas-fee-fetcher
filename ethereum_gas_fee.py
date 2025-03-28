import os
import requests
import logging
import argparse
from requests.exceptions import Timeout, RequestException, HTTPError
from retrying import retry

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)

# Constants
ETHERSCAN_API_URL = "https://api.etherscan.io/api"
RETRY_ATTEMPTS = 3
RETRY_BACKOFF = 1000  # 1 second base backoff
RETRY_BACKOFF_MAX = 4000  # 4 seconds max backoff


def validate_api_key(api_key: str) -> None:
    """Validates the API key."""
    if not api_key:
        raise ValueError("Etherscan API key is required. Provide it via --api_key argument or ETHERSCAN_API_KEY environment variable.")


def fetch_ethereum_gas_fees(api_key: str, timeout: int = 10) -> dict:
    """
    Fetches the current Ethereum gas fees from Etherscan.

    Args:
        api_key (str): Etherscan API key.
        timeout (int): API request timeout in seconds (default: 10).

    Returns:
        dict: Dictionary containing Safe, Proposed, and Fast gas prices in Gwei.

    Raises:
        ValueError: If the API key is invalid or response is unsuccessful.
        ConnectionError: If the request fails due to network issues.
        RuntimeError: For unexpected errors.
    """
    validate_api_key(api_key)

    params = {
        "module": "gastracker",
        "action": "gasoracle",
        "apikey": api_key,
    }

    try:
        logging.info("Fetching Ethereum gas fees from Etherscan...")
        response = requests.get(ETHERSCAN_API_URL, params=params, timeout=timeout)
        response.raise_for_status()  # Raise exception for HTTP errors
        data = response.json()

        if data.get("status") == "1" and "result" in data:
            return {
                "SafeGasPrice": data["result"]["SafeGasPrice"],
                "ProposeGasPrice": data["result"]["ProposeGasPrice"],
                "FastGasPrice": data["result"]["FastGasPrice"],
            }
        else:
            raise ValueError(f"Failed to retrieve gas fees: {data.get('message', 'Unknown error')}")

    except Timeout:
        raise ConnectionError("Request to Etherscan API timed out.")
    except HTTPError as e:
        raise ConnectionError(f"HTTP error occurred: {e}")
    except RequestException as e:
        raise ConnectionError(f"API request failed: {e}")
    except Exception as e:
        raise RuntimeError(f"Unexpected error: {e}")


@retry(stop_max_attempt_number=RETRY_ATTEMPTS, wait_exponential_multiplier=RETRY_BACKOFF, wait_exponential_max=RETRY_BACKOFF_MAX)
def fetch_gas_fees_with_retry(api_key: str, timeout: int = 10) -> dict:
    """
    Fetches Ethereum gas fees with automatic retries in case of failure.

    Retries up to RETRY_ATTEMPTS times with exponential backoff.

    Args:
        api_key (str): Etherscan API key.
        timeout (int): Timeout for each retry attempt.

    Returns:
        dict: Gas fees retrieved from the API.
    """
    attempt = fetch_gas_fees_with_retry.retry.statistics.get("attempt_number", 1)
    logging.info(f"Attempt {attempt}/{RETRY_ATTEMPTS} to fetch gas fees...")
    return fetch_ethereum_gas_fees(api_key, timeout)


def main(api_key: str) -> None:
    """
    Main function to fetch and log Ethereum gas fees.

    Args:
        api_key (str): Etherscan API key.
    """
    try:
        gas_fees = fetch_gas_fees_with_retry(api_key)
        logging.info("Ethereum Gas Fees:")
        logging.info(f"  Safe Gas Price: {gas_fees['SafeGasPrice']} Gwei")
        logging.info(f"  Proposed Gas Price: {gas_fees['ProposeGasPrice']} Gwei")
        logging.info(f"  Fast Gas Price: {gas_fees['FastGasPrice']} Gwei")
    except (ValueError, ConnectionError, RuntimeError) as e:
        logging.error(e)
    except Exception as e:
        logging.error(f"Unhandled exception: {e}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Fetch Ethereum gas fees from Etherscan API.")
    parser.add_argument(
        "--api_key",
        type=str,
        help="Etherscan API key (or set via ETHERSCAN_API_KEY environment variable)",
    )
    args = parser.parse_args()
    api_key = args.api_key or os.getenv("ETHERSCAN_API_KEY")

    if not api_key:
        logging.error("API key is required. Provide it via --api_key argument or ETHERSCAN_API_KEY environment variable.")
        exit(1)

    main(api_key)
