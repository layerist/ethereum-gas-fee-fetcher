import os
import requests
import logging
import argparse

def get_ethereum_gas_fee(api_key):
    """
    Fetches the current gas fee from Etherscan API.

    Args:
        api_key (str): Etherscan API key.

    Returns:
        dict: A dictionary containing gas fees in Gwei.
    """
    if not api_key:
        logging.error("API key is missing.")
        raise ValueError("API key is required to fetch gas fees.")

    url = f"https://api.etherscan.io/api?module=gastracker&action=gasoracle&apikey={api_key}"
    
    try:
        logging.info("Sending request to Etherscan API...")
        response = requests.get(url)
        response.raise_for_status()
        data = response.json()

        if data.get('status') == '1' and 'result' in data:
            logging.info("Gas fee data fetched successfully.")
            return {
                "SafeGasPrice": data['result']['SafeGasPrice'],      # Safe Gas Price (Gwei)
                "ProposeGasPrice": data['result']['ProposeGasPrice'],  # Proposed Gas Price (Gwei)
                "FastGasPrice": data['result']['FastGasPrice']        # Fast Gas Price (Gwei)
            }
        else:
            error_message = data.get('message', 'Unknown error')
            logging.error(f"Failed to fetch gas fee data: {error_message}")
            raise ValueError(f"Failed to fetch gas fee data: {error_message}")
    except requests.exceptions.RequestException as e:
        logging.error(f"Request to Etherscan API failed: {e}")
        raise ConnectionError(f"Request to Etherscan API failed: {e}")
    except ValueError as e:
        logging.error(f"Data validation error: {e}")
        raise
    except Exception as e:
        logging.error(f"An unexpected error occurred: {e}")
        raise

def main(api_key):
    try:
        gas_fees = get_ethereum_gas_fee(api_key)
        logging.info("Gas fees retrieved successfully:")
        logging.info(f"Safe Gas Price: {gas_fees['SafeGasPrice']} Gwei")
        logging.info(f"Proposed Gas Price: {gas_fees['ProposeGasPrice']} Gwei")
        logging.info(f"Fast Gas Price: {gas_fees['FastGasPrice']} Gwei")
    except Exception as e:
        logging.error(f"Error during execution: {e}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Fetch Ethereum gas fees using the Etherscan API.')
    parser.add_argument('--api_key', type=str, help='Etherscan API key (or set via ETHERSCAN_API_KEY environment variable)')
    
    args = parser.parse_args()
    
    # Check if the API key is provided either via the command line or environment variable
    api_key = args.api_key or os.getenv("ETHERSCAN_API_KEY")
    
    if not api_key:
        logging.error("API key is required. Provide it via --api_key argument or ETHERSCAN_API_KEY environment variable.")
        raise ValueError("API key is required. Provide it via --api_key argument or ETHERSCAN_API_KEY environment variable.")
    
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
    main(api_key)
