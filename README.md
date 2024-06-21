# Ethereum Gas Fee Fetcher

A simple Python script to fetch the current gas fees for the Ethereum network using the Etherscan API.

## Features

- Fetches safe, proposed, and fast gas prices in gwei.
- Uses the Etherscan API for reliable gas price data.

## Requirements

- Python 3.x
- `requests` library (install using `pip install requests`)
- Etherscan API key

## Installation

1. Clone the repository or download the script file.
2. Install the required Python package:
    ```sh
    pip install requests
    ```

## Usage

1. Obtain your Etherscan API key from [Etherscan.io](https://etherscan.io/register).
2. Replace `YourEtherscanAPIKey` in the script with your actual API key.
3. Run the script:
    ```sh
    python ethereum_gas_fee.py
    ```

## Example Output

```
Safe Gas Price: 20 Gwei
Propose Gas Price: 25 Gwei
Fast Gas Price: 30 Gwei
```

## Function Details

### `get_ethereum_gas_fee(api_key)`

Fetches the current gas fee from Etherscan API.

**Arguments:**
- `api_key` (str): Your Etherscan API key.

**Returns:**
- `dict`: A dictionary containing gas fees in gwei.
  - `SafeGasPrice` (str): Safe gas price in gwei.
  - `ProposeGasPrice` (str): Proposed gas price in gwei.
  - `FastGasPrice` (str): Fast gas price in gwei.

**Raises:**
- `Exception`: If the API call fails or returns an error.

## Contributing

Feel free to submit issues or pull requests for improvements or additional features.

## License

This project is licensed under the MIT License.
