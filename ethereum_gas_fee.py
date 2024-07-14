import os
import requests
import logging

def get_ethereum_gas_fee(api_key):
    """
    Fetches the current gas fee from Etherscan API.

    Args:
        api_key (str): Etherscan API key.

    Returns:
        dict: A dictionary containing gas fees in gwei.
    """
    url = f"https://api.etherscan.io/api?module=gastracker&action=gasoracle&apikey={api_key}"
    
    try:
        response = requests.get(url)
        response.raise_for_status()  # Check for HTTP errors
        data = response.json()

        if data['status'] == '1':
            return {
                "SafeGasPrice": data['result']['SafeGasPrice'],   # Safe Gas Price (Gwei)
                "ProposeGasPrice": data['result']['ProposeGasPrice'],  # Propose Gas Price (Gwei)
                "FastGasPrice": data['result']['FastGasPrice']    # Fast Gas Price (Gwei)
            }
        else:
            logging.error(f"Failed to fetch gas fee data: {data['message']}")
            raise Exception(f"Failed to fetch gas fee data: {data['message']}")
    except requests.exceptions.RequestException as e:
        logging.error(f"Request failed: {e}")
        raise Exception(f"Request failed: {e}")
    except Exception as e:
        logging.error(f"An error occurred: {e}")
        raise

def main():
    api_key = os.getenv("ETHERSCAN_API_KEY") or "YourEtherscanAPIKey"
    
    try:
        gas_fees = get_ethereum_gas_fee(api_key)
        print(f"Safe Gas Price: {gas_fees['SafeGasPrice']} Gwei")
        print(f"Propose Gas Price: {gas_fees['ProposeGasPrice']} Gwei")
        print(f"Fast Gas Price: {gas_fees['FastGasPrice']} Gwei")
    except Exception as e:
        print(e)

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
