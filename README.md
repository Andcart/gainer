# Gainer: Cross-Chain Bridge Relayer Simulation

This repository contains a Python-based simulation of a crucial component in a cross-chain bridge system: an off-chain relayer. This relayer is responsible for listening to events on a source blockchain and triggering corresponding actions on a destination blockchain.

This script is designed to be architecturally sound, robust, and demonstrative of the core logic required for such a system in a real-world decentralized application.

---

### Concept

A cross-chain bridge allows users to transfer assets or data from one blockchain (e.g., Ethereum) to another (e.g., Polygon). A common architecture for this involves:

1.  **Lock/Burn on Source Chain**: A user deposits assets into a smart contract on the source chain. This action emits an event, like `TokensDeposited`.
2.  **Off-Chain Relay**: An independent service, the "relayer" or "validator," listens for this event. To prevent issues with chain re-organizations, it waits for a certain number of block confirmations.
3.  **Mint/Release on Destination Chain**: Once the event is confirmed, the relayer submits a transaction to a smart contract on the destination chain. This transaction is signed by the relayer and includes proof of the original deposit. The destination contract verifies this and mints a corresponding wrapped asset or releases the equivalent native asset to the user.

This script simulates the **Off-Chain Relay** component (Step 2 & 3). It connects to two blockchain RPC endpoints, listens for deposit events, and submits release transactions.

---

### Code Architecture

The script is designed with a clear separation of concerns, implemented through several key classes:

-   **`BlockchainConnector`**: A reusable wrapper around the `web3.py` library. It manages the connection to a single blockchain node, handles PoA middleware injection (for chains like Polygon, Goerli), and provides utility methods like fetching the latest block number or instantiating a contract. An instance is created for both the source and destination chains.

-   **`BridgeEventListener`**: This is the core listening component. It attaches to the source chain's bridge contract and polls for new `TokensDeposited` events. Its key responsibility is to handle **block confirmations**, ensuring that it only acts on events that are unlikely to be reverted due to a chain re-organization.

-   **`TransactionProcessor`**: Once the `BridgeEventListener` confirms an event, it passes the event data to the `TransactionProcessor`. This class is responsible for all write operations. It constructs, signs, and sends the `releaseTokens` transaction to the destination chain. It manages the relayer's private key, nonce, and gas estimation.

-   **`HealthMonitor`**: A simple utility class demonstrating integration with external services. It periodically sends a status update (a "heartbeat") to a configured webhook URL using the `requests` library. This is crucial for monitoring the relayer's operational status in a production environment.

-   **Main Execution Block**: The `main()` function orchestrates the entire process. It loads configuration, initializes all the class instances, and runs the main `while` loop that drives the polling and processing cycle. It also includes graceful shutdown logic.

#### Flow Diagram

```
+-----------------------+      +-------------------------+      +---------------------------+
|  Source Chain Node    | <--- |   BridgeEventListener   | ---> |   TransactionProcessor    | ---> Destination Chain Node
| (e.g., Ethereum)      |      |                         |      |                           |      (e.g., Polygon)
+-----------------------+      | - Polls for new events  |      | - Builds `releaseTokens` tx |
                             | - Waits for confirmations|      | - Signs with relayer key  |
                             +-------------------------+      | - Sends to destination    |
                                                              +---------------------------+
                                                                            |
                                                                            v
                                                                    +-------------------+
                                                                    |   HealthMonitor   |
                                                                    | (via Requests)    |
                                                                    +-------------------+
```

---

### How it Works

1.  **Configuration**: The script starts by loading all necessary parameters (RPC URLs, contract addresses, private keys) from a `.env` file using `python-dotenv`. This avoids hardcoding sensitive information.

2.  **Initialization**: It creates two `BlockchainConnector` instances, one for the source chain and one for the destination. It then instantiates the `BridgeEventListener`, `TransactionProcessor`, and `HealthMonitor` with the appropriate connectors and configuration.

3.  **Event Polling Loop**: The script enters an infinite loop:
    a. The `BridgeEventListener` polls the source chain for new `TokensDeposited` event logs.
    b. For each new event, it checks if `current_block - event_block >= BLOCK_CONFIRMATIONS`.
    c. Events that meet the confirmation threshold are collected.

4.  **Transaction Processing**: If there are confirmed events:
    a. Each event is passed to the `TransactionProcessor`.
    b. The processor constructs a `releaseTokens` transaction, filling in the recipient, amount, and original transaction ID from the event data.
    c. It fetches the current nonce for the relayer's account, estimates gas, signs the transaction, and sends it to the destination chain.
    d. It keeps a record of processed transaction IDs to prevent double-spending in case of a script restart.

5.  **Health Reporting**: Periodically, the `HealthMonitor` sends a POST request to a specified URL with the relayer's current status, last seen block, and number of pending transactions.

6.  **Delay**: After each cycle, the script pauses for a configured interval (`POLL_INTERVAL_SECONDS`) before polling again.

---

### Usage Example

1.  **Prerequisites**:
    *   Python 3.8+
    *   Access to RPC endpoints for two EVM-compatible blockchains (e.g., from Alchemy, Infura, or a local node).
    *   A private key for an account on the destination chain with enough funds to pay for gas.

2.  **Installation**:
    Clone the repository and install the required dependencies.
    ```bash
    git clone https://github.com/your-username/gainer.git
    cd gainer
    pip install -r requirements.txt
    ```

3.  **Configuration**:
    Create a file named `.env` in the root directory of the project. Populate it with your specific details. **Do not commit this file to version control.**

    **Example `.env` file:**
    ```env
    # --- Chain Configuration ---
    # RPC URL for the chain where users deposit tokens
    SOURCE_CHAIN_RPC_URL="https://sepolia.infura.io/v3/YOUR_INFURA_PROJECT_ID"
    # RPC URL for the chain where tokens are released/minted
    DESTINATION_CHAIN_RPC_URL="https://rpc-mumbai.maticvigil.com/"

    # --- Contract Addresses ---
    SOURCE_BRIDGE_CONTRACT_ADDRESS="0x..."
    DESTINATION_BRIDGE_CONTRACT_ADDRESS="0x..."

    # --- Relayer Wallet ---
    # Private key of the account that will submit transactions on the destination chain
    # IMPORTANT: DO NOT USE A KEY WITH REAL FUNDS FOR TESTING
    RELAYER_PRIVATE_KEY="your_relayer_account_private_key_without_0x"

    # --- Monitoring ---
    # A webhook URL for health status reporting (e.g., from Healthchecks.io or a custom service)
    HEALTHCHECK_URL="https://hc-ping.com/YOUR_CHECK_UUID"

    # --- Optional Parameters ---
    # How often to poll for new events (in seconds)
    POLL_INTERVAL_SECONDS=15
    # Number of blocks to wait before processing an event
    BLOCK_CONFIRMATIONS=12
    ```

4.  **Running the Script**:
    Execute the script from your terminal.
    ```bash
    python script.py
    ```

    You will see logs in your console indicating the relayer's status, including connections, event polling, and transaction processing.
    ```
    2023-10-27 15:30:00 - INFO - [script.main] - Starting Gainer Bridge Relayer simulation...
    2023-10-27 15:30:01 - INFO - [script._connect] - Successfully connected to SourceChain (Chain ID: 11155111).
    2023-10-27 15:30:02 - INFO - [script._connect] - Successfully connected to DestinationChain (Chain ID: 80001).
    2023-10-27 15:30:02 - INFO - [script.main] - Entering main event loop. Press Ctrl+C to exit.
    ...
    ```