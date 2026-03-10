#!/usr/bin/env python3
"""
POLYMARKET ON-CHAIN ALLOWANCES SETUP
=====================================

Sets USDC and CTF token approvals for the Polymarket exchange contracts.
Required before placing orders via the CLOB API.

Approves:
  1. CTF Exchange (standard markets)
  2. Neg Risk CTF Exchange (neg_risk markets)
  3. Neg Risk Adapter

Prerequisites:
    pip install web3
    Your wallet needs POL/MATIC for gas on Polygon.

Usage:
    python setup_allowances.py
"""

import os
import sys

def main():
    try:
        from web3 import Web3
        from web3.constants import MAX_INT
        from web3.middleware import ExtraDataToPOAMiddleware
    except ImportError:
        print("[ERROR] web3 not installed. Run: pip install web3")
        sys.exit(1)

    private_key = os.getenv("POLYGON_PRIVATE_KEY", "")
    if not private_key:
        print("[ERROR] Set POLYGON_PRIVATE_KEY environment variable")
        sys.exit(1)

    if not private_key.startswith("0x"):
        private_key = "0x" + private_key

    rpc_url = os.getenv("POLYGON_RPC_URL", "https://polygon-rpc.com")
    chain_id = 137

    web3 = Web3(Web3.HTTPProvider(rpc_url))
    web3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)

    account = web3.eth.account.from_key(private_key)
    pub_key = account.address
    print(f"Wallet: {pub_key}")
    print(f"Balance: {web3.eth.get_balance(pub_key) / 1e18:.4f} POL")

    erc20_abi = '[{"constant":false,"inputs":[{"name":"_spender","type":"address"},{"name":"_value","type":"uint256"}],"name":"approve","outputs":[{"name":"","type":"bool"}],"payable":false,"stateMutability":"nonpayable","type":"function"}]'
    erc1155_abi = '[{"inputs":[{"internalType":"address","name":"operator","type":"address"},{"internalType":"bool","name":"approved","type":"bool"}],"name":"setApprovalForAll","outputs":[],"stateMutability":"nonpayable","type":"function"}]'

    # Native USDC on Polygon (what Polymarket actually uses)
    usdc_native_address = "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"
    # Bridged USDC.e (legacy, approve just in case)
    usdc_bridged_address = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
    ctf_address = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"

    usdc_native = web3.eth.contract(address=web3.to_checksum_address(usdc_native_address), abi=erc20_abi)
    usdc_bridged = web3.eth.contract(address=web3.to_checksum_address(usdc_bridged_address), abi=erc20_abi)
    ctf = web3.eth.contract(address=web3.to_checksum_address(ctf_address), abi=erc1155_abi)

    targets = {
        "CTF Exchange": "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E",
        "Neg Risk CTF Exchange": "0xC5d563A36AE78145C45a50134d48A1215220f80a",
        "Neg Risk Adapter": "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296",
    }

    nonce = web3.eth.get_transaction_count(pub_key)

    for name, target in targets.items():
        print(f"\n--- {name} ({target}) ---")

        # Approve native USDC spending (what Polymarket uses)
        print(f"  Approving native USDC (0x3c49...)...")
        tx = usdc_native.functions.approve(target, int(MAX_INT, 0)).build_transaction({
            "chainId": chain_id, "from": pub_key, "nonce": nonce,
        })
        signed = web3.eth.account.sign_transaction(tx, private_key=private_key)
        tx_hash = web3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = web3.eth.wait_for_transaction_receipt(tx_hash, 600)
        print(f"  Native USDC approved: {receipt.transactionHash.hex()}")
        nonce += 1

        # Approve bridged USDC.e spending (legacy fallback)
        print(f"  Approving bridged USDC.e (0x2791...)...")
        tx = usdc_bridged.functions.approve(target, int(MAX_INT, 0)).build_transaction({
            "chainId": chain_id, "from": pub_key, "nonce": nonce,
        })
        signed = web3.eth.account.sign_transaction(tx, private_key=private_key)
        tx_hash = web3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = web3.eth.wait_for_transaction_receipt(tx_hash, 600)
        print(f"  Bridged USDC.e approved: {receipt.transactionHash.hex()}")
        nonce += 1

        # Approve CTF token transfers
        print(f"  Approving CTF tokens...")
        tx = ctf.functions.setApprovalForAll(target, True).build_transaction({
            "chainId": chain_id, "from": pub_key, "nonce": nonce,
        })
        signed = web3.eth.account.sign_transaction(tx, private_key=private_key)
        tx_hash = web3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = web3.eth.wait_for_transaction_receipt(tx_hash, 600)
        print(f"  CTF approved: {receipt.transactionHash.hex()}")
        nonce += 1

    print("\n[SUCCESS] All allowances set for native USDC + USDC.e + CTF! You can now place orders via the CLOB API.")


if __name__ == "__main__":
    main()
