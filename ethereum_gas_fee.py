import os
import requests
import logging
import argparse
from requests.exceptions import Timeout, RequestException
from retrying import retry

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)

def get_ethereum_gas_fee(api_key: str, timeout: int = 10) -> dict:
    """
    Fetches the current Ethereum gas fee from the Etherscan API.

    Args:
        api_key (str): Etherscan API key.
        timeout (int): Timeout for the API request in seconds (default is 10).

    Returns:
        dict: A dictionary containing gas fees in Gwei.

    Raises:
        ValueError: If the API key is invalid or data is not retrieved successfully.
        ConnectionError: If there is a request failure.
    """
    if not api_key:
        raise ValueError("API key is required to fetch gas fees.")

    url = "https://api.etherscan.io/api"
    params = {
        "module": "gastracker",
        "action": "gasoracle",
        "apikey": api_key,
    }

    try:
        logging.info("Sending request to Etherscan API...")
        response = requests.get(url, params=params, timeout=timeout)
        response.raise_for_status()

        data = response.json()
        if data.get("status") == "1" and "result" in data:
            logging.info("Gas fee data fetched successfully.")
            return {
                "SafeGasPrice": data["result"]["SafeGasPrice"], 
                "ProposeGasPrice": data["result"]["ProposeGasPrice"], 
                "FastGasPrice": data["result"]["FastGasPrice"], 
            }
        else:
            error_message = data.get("message", "Unknown error")
            raise ValueError(f"Failed to fetch gas fee data: {error_message}")

    except Timeout:
        raise ConnectionError("Request to Etherscan API timed out.")
    except RequestException as e:
        raise ConnectionError(f"Request to Etherscan API failed: {e}")
    except Exception as e:
        raise RuntimeError(f"An unexpected error occurred: {e}")

@retry(stop_max_attempt_number=3, wait_exponential_multiplier=1000, wait_exponential_max=4000)
def fetch_gas_fees_with_retry(api_key: str, timeout: int = 10) -> dict:
    """
    Wrapper around get_ethereum_gas_fee that implements a retry mechanism.

    Retries up to 3 times with exponential backoff.

    Args:
        api_key (str): Etherscan API key.
        timeout (int): Timeout for each retry attempt.

    Returns:
        dict: Gas fees retrieved from the API.
    """
    return get_ethereum_gas_fee(api_key, timeout)

def main(api_key: str) -> None:
    """
    Main execution function to fetch and log Ethereum gas fees.

    Args:
        api_key (str): Etherscan API key.
    """
    try:
        gas_fees = fetch_gas_fees_with_retry(api_key)
        logging.info("Gas fees retrieved successfully:")
        logging.info(f"Safe Gas Price: {gas_fees['SafeGasPrice']} Gwei")
        logging.info(f"Proposed Gas Price: {gas_fees['ProposeGasPrice']} Gwei")
        logging.info(f"Fast Gas Price: {gas_fees['FastGasPrice']} Gwei")
    except ValueError as ve:
        logging.error(f"Validation error: {ve}")
    except ConnectionError as ce:
        logging.error(f"Connection error: {ce}")
    except RuntimeError as re:
        logging.error(f"Runtime error: {re}")
    except Exception as e:
        logging.error(f"Unhandled exception: {e}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Fetch Ethereum gas fees using the Etherscan API.")
    parser.add_argument(
        "--api_key",
        type=str,
        help="Etherscan API key (or set via ETHERSCAN_API_KEY environment variable)",
    )

    args = parser.parse_args()

    api_key = args.api_key or os.getenv("ETHERSCAN_API_KEY")

    if not api_key:
        logging.error(
            "API key is required. Provide it via --api_key argument or ETHERSCAN_API_KEY environment variable."
        )
        exit(1)

    main(api_key)
