import os
import time
import logging
import json
from typing import Dict, Any, Optional, List

import requests
from web3 import Web3
from web3.middleware import geth_poa_middleware
from web3.types import LogReceipt
from dotenv import load_dotenv

# --- Basic Configuration ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - [%(module)s.%(funcName)s] - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

# --- Load Environment Variables ---
# For security and flexibility, configuration is managed via a .env file.
load_dotenv()

# --- Constants ---
# Placeholder ABIs for the bridge contracts on source and destination chains.
# In a real-world scenario, these would be loaded from JSON files.
SOURCE_BRIDGE_ABI = json.loads('''
[
    {
        "anonymous": false,
        "inputs": [
            {"indexed": true, "name": "sender", "type": "address"},
            {"indexed": true, "name": "recipient", "type": "address"},
            {"indexed": false, "name": "amount", "type": "uint256"},
            {"indexed": false, "name": "destinationChainId", "type": "uint256"},
            {"indexed": true, "name": "transactionId", "type": "bytes32"}
        ],
        "name": "TokensDeposited",
        "type": "event"
    }
]
''')

DESTINATION_BRIDGE_ABI = json.loads('''
[
    {
        "inputs": [
            {"name": "recipient", "type": "address"},
            {"name": "amount", "type": "uint256"},
            {"name": "sourceTransactionId", "type": "bytes32"}
        ],
        "name": "releaseTokens",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function"
    }
]
''')


class ConfigError(Exception):
    """Custom exception for configuration errors."""
    pass


def load_configuration() -> Dict[str, Any]:
    """Loads and validates configuration from environment variables."""
    required_vars = [
        'SOURCE_CHAIN_RPC_URL', 'DESTINATION_CHAIN_RPC_URL',
        'SOURCE_BRIDGE_CONTRACT_ADDRESS', 'DESTINATION_BRIDGE_CONTRACT_ADDRESS',
        'RELAYER_PRIVATE_KEY', 'HEALTHCHECK_URL'
    ]
    config = {}
    for var in required_vars:
        value = os.getenv(var)
        if not value:
            raise ConfigError(f"Missing required environment variable: {var}")
        config[var] = value
    
    # Add optional variables with defaults
    config['POLL_INTERVAL_SECONDS'] = int(os.getenv('POLL_INTERVAL_SECONDS', '15'))
    config['BLOCK_CONFIRMATIONS'] = int(os.getenv('BLOCK_CONFIRMATIONS', '12'))

    logging.info("Configuration loaded successfully.")
    return config


class BlockchainConnector:
    """A robust wrapper for web3.py to handle blockchain connections."""

    def __init__(self, rpc_url: str, chain_name: str):
        """
        Initializes a connection to a blockchain node.

        Args:
            rpc_url (str): The HTTP RPC endpoint of the blockchain node.
            chain_name (str): A descriptive name for the chain (e.g., 'SourceChain').
        """
        self.rpc_url = rpc_url
        self.chain_name = chain_name
        self.web3: Optional[Web3] = None
        self._connect()

    def _connect(self):
        """Establishes the Web3 connection and handles potential PoA middleware."""
        try:
            self.web3 = Web3(Web3.HTTPProvider(self.rpc_url))
            # Inject middleware for PoA chains like Goerli, Sepolia, Polygon, etc.
            self.web3.middleware_onion.inject(geth_poa_middleware, layer=0)
            
            if not self.web3.is_connected():
                raise ConnectionError(f"Failed to connect to {self.chain_name} at {self.rpc_url}")
            
            self.chain_id = self.web3.eth.chain_id
            logging.info(f"Successfully connected to {self.chain_name} (Chain ID: {self.chain_id}).")
        except Exception as e:
            logging.error(f"Error connecting to {self.chain_name}: {e}")
            self.web3 = None

    def is_connected(self) -> bool:
        """Checks if the connection is alive."""
        return self.web3 is not None and self.web3.is_connected()

    def get_latest_block_number(self) -> int:
        """Retrieves the latest block number from the connected node."""
        if not self.is_connected():
            logging.warning(f"Attempted to get block number while disconnected from {self.chain_name}. Reconnecting...")
            self._connect()
            if not self.is_connected():
                return 0
        return self.web3.eth.block_number

    def get_contract(self, address: str, abi: List[Dict[str, Any]]):
        """Returns a Web3 contract instance."""
        if not self.is_connected():
            raise ConnectionError(f"Not connected to {self.chain_name}. Cannot get contract.")
        
        checksum_address = self.web3.to_checksum_address(address)
        return self.web3.eth.contract(address=checksum_address, abi=abi)


class BridgeEventListener:
    """Listens for specific events on the source chain bridge contract."""

    def __init__(self, connector: BlockchainConnector, contract_address: str, block_confirmations: int):
        """
        Initializes the event listener.

        Args:
            connector (BlockchainConnector): The connector for the source chain.
            contract_address (str): The address of the source bridge contract.
            block_confirmations (int): Number of blocks to wait before considering an event confirmed.
        """
        self.connector = connector
        self.contract = self.connector.get_contract(contract_address, SOURCE_BRIDGE_ABI)
        self.block_confirmations = block_confirmations
        self.event_filter = self.contract.events.TokensDeposited.create_filter(fromBlock='latest')
        self.processed_events = set() # To avoid processing the same event twice.
        logging.info(f"Event listener initialized for contract at {contract_address} on {self.connector.chain_name}.")

    def get_confirmed_events(self) -> List[LogReceipt]:
        """
        Polls for new logs and returns only those that are sufficiently confirmed.
        This helps mitigate risks from blockchain re-organizations.
        """
        try:
            latest_block = self.connector.get_latest_block_number()
            if latest_block == 0: return []

            new_entries = self.event_filter.get_new_entries()
            if not new_entries:
                return []

            confirmed_events = []
            for event in new_entries:
                tx_hash = event['transactionHash'].hex()
                if tx_hash in self.processed_events:
                    continue

                # Check for confirmations
                if (latest_block - event['blockNumber']) >= self.block_confirmations:
                    logging.info(f"Confirmed event found: TxHash {tx_hash} in block {event['blockNumber']}")
                    self.processed_events.add(tx_hash)
                    confirmed_events.append(event)
                else:
                    logging.debug(f"Event {tx_hash} is pending confirmation. Current confirmations: {latest_block - event['blockNumber']}/{self.block_confirmations}")
            
            return confirmed_events
        except Exception as e:
            logging.error(f"Error while polling for events: {e}")
            return []


class TransactionProcessor:
    """Processes events by creating and sending transactions to the destination chain."""

    def __init__(self, connector: BlockchainConnector, contract_address: str, relayer_private_key: str):
        """
        Initializes the transaction processor.

        Args:
            connector (BlockchainConnector): The connector for the destination chain.
            contract_address (str): The address of the destination bridge contract.
            relayer_private_key (str): The private key of the account that will send transactions.
        """
        self.connector = connector
        self.contract = self.connector.get_contract(contract_address, DESTINATION_BRIDGE_ABI)
        self.web3 = self.connector.web3
        
        # Securely handle the relayer account
        if not relayer_private_key.startswith('0x'):
            relayer_private_key = '0x' + relayer_private_key
        self.relayer_account = self.web3.eth.account.from_key(relayer_private_key)
        self.relayer_address = self.relayer_account.address
        self.processed_source_tx_ids = set() # To ensure idempotency
        logging.info(f"Transaction Processor initialized. Relayer address: {self.relayer_address} on {self.connector.chain_name}.")

    def process_deposit_event(self, event: LogReceipt) -> bool:
        """
        Constructs and sends a `releaseTokens` transaction based on a `TokensDeposited` event.

        Args:
            event (LogReceipt): The event data from the source chain.

        Returns:
            bool: True if the transaction was successfully sent, False otherwise.
        """
        try:
            args = event['args']
            source_tx_id = args['transactionId']

            # Idempotency check: prevent re-processing the same cross-chain transaction
            if source_tx_id in self.processed_source_tx_ids:
                logging.warning(f"Source transaction ID {source_tx_id.hex()} has already been processed. Skipping.")
                return True

            logging.info(f"Processing deposit event for source tx ID: {source_tx_id.hex()}")
            
            # --- Build the transaction ---
            nonce = self.web3.eth.get_transaction_count(self.relayer_address)
            tx_data = {
                'from': self.relayer_address,
                'nonce': nonce,
                'gasPrice': self.web3.eth.gas_price,
                'chainId': self.connector.chain_id,
            }

            # Build the function call
            release_tx = self.contract.functions.releaseTokens(
                args['recipient'],
                args['amount'],
                source_tx_id
            ).build_transaction(tx_data)

            # Estimate gas to prevent 'out of gas' errors
            release_tx['gas'] = self.web3.eth.estimate_gas(release_tx)

            # --- Sign and send ---
            signed_tx = self.web3.eth.account.sign_transaction(release_tx, self.relayer_account.key)
            tx_hash = self.web3.eth.send_raw_transaction(signed_tx.rawTransaction)
            
            logging.info(f"Sent `releaseTokens` transaction to {self.connector.chain_name}. TxHash: {tx_hash.hex()}")
            
            # In a production system, you would wait for the receipt and handle failures.
            # For this simulation, we assume it succeeds if sending doesn't throw an error.
            # tx_receipt = self.web3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
            # if tx_receipt.status != 1:
            #     raise Exception("Transaction failed on-chain")

            self.processed_source_tx_ids.add(source_tx_id)
            return True
        except Exception as e:
            logging.error(f"Failed to process event and send transaction: {e}")
            return False


class HealthMonitor:
    """A simple monitor to report the relayer's status to an external service."""

    def __init__(self, healthcheck_url: str, relayer_id: str):
        """
        Initializes the health monitor.

        Args:
            healthcheck_url (str): The URL of the monitoring service.
            relayer_id (str): A unique identifier for this relayer instance.
        """
        self.url = healthcheck_url
        self.relayer_id = relayer_id
        self.last_checkin = 0
        self.checkin_interval = 60 # Check in every 60 seconds

    def report_status(self, status: str, last_processed_block: int, pending_tx: int):
        """Sends a status update if the interval has passed."""
        current_time = time.time()
        if current_time - self.last_checkin < self.checkin_interval:
            return
        
        try:
            payload = {
                'relayerId': self.relayer_id,
                'status': status,
                'timestamp': int(current_time),
                'lastSourceBlock': last_processed_block,
                'pendingTransactions': pending_tx,
            }
            response = requests.post(self.url, json=payload, timeout=10)
            if response.status_code == 200:
                logging.info(f"Successfully reported status to health monitor: {status}")
            else:
                logging.warning(f"Failed to report status. Service returned {response.status_code}")
            self.last_checkin = current_time
        except requests.RequestException as e:
            logging.error(f"Could not reach health monitoring service: {e}")


def main():
    """The main execution function to run the bridge relayer."""
    logging.info("Starting Gainer Bridge Relayer simulation...")
    
    try:
        config = load_configuration()

        # --- Initialize components ---
        source_connector = BlockchainConnector(config['SOURCE_CHAIN_RPC_URL'], 'SourceChain')
        dest_connector = BlockchainConnector(config['DESTINATION_CHAIN_RPC_URL'], 'DestinationChain')

        if not source_connector.is_connected() or not dest_connector.is_connected():
            logging.critical("Failed to connect to one or both blockchains. Exiting.")
            return

        listener = BridgeEventListener(
            source_connector, 
            config['SOURCE_BRIDGE_CONTRACT_ADDRESS'], 
            config['BLOCK_CONFIRMATIONS']
        )
        processor = TransactionProcessor(
            dest_connector, 
            config['DESTINATION_BRIDGE_CONTRACT_ADDRESS'],
            config['RELAYER_PRIVATE_KEY']
        )
        monitor = HealthMonitor(config['HEALTHCHECK_URL'], processor.relayer_address)

        # --- Main loop ---
        logging.info("Entering main event loop. Press Ctrl+C to exit.")
        while True:
            confirmed_events = listener.get_confirmed_events()
            
            if confirmed_events:
                logging.info(f"Found {len(confirmed_events)} new confirmed events to process.")
                for event in confirmed_events:
                    processor.process_deposit_event(event)
            else:
                logging.debug("No new confirmed events found. Polling again soon...")

            # Report health status
            monitor.report_status(
                'OPERATIONAL',
                source_connector.get_latest_block_number(),
                len(confirmed_events)
            )

            time.sleep(config['POLL_INTERVAL_SECONDS'])

    except ConfigError as e:
        logging.critical(f"Configuration error: {e}")
    except KeyboardInterrupt:
        logging.info("Shutdown signal received. Exiting gracefully.")
    except Exception as e:
        logging.critical(f"An unhandled error occurred in the main loop: {e}", exc_info=True)
        monitor.report_status('ERROR', 0, 0) # Report critical failure

if __name__ == "__main__":
    main()
