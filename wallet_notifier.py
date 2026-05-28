import hashlib
import json
import os
import sqlite3
from typing import Dict, List

import requests

DB_PATH = os.getenv("DB_PATH", "bot.db")
OWNER_ID = int(os.getenv("OWNER_ID", "8796221224"))
POLL_SECONDS = int(os.getenv("NOTIFIER_POLL_SECONDS", "30"))

# Watch addresses
WATCH: Dict[str, Dict[str, str]] = {
    "USDT_TRC20": {
        "network": "TRON",
        "address": os.getenv("WATCH_USDT_TRON", "TUGany2B2ZyTEd3zpf6A6NTdiFaQpPL8qb"),
    },
    "USDT_BEP20": {
        "network": "BSC",
        "address": os.getenv("WATCH_USDT_BSC", "0xdbaa89c688b7a84145a5adcc5c2b7cc62bf10181"),
    },
    "USDT_ERC20": {
        "network": "ETHEREUM",
        "address": os.getenv("WATCH_USDT_ETH", "0xdbaa89c688b7a84145a5adcc5c2b7cc62bf10181"),
    },
    "USDC_SOL": {
        "network": "SOLANA",
        "address": os.getenv("WATCH_USDC_SOL", "EKmcFRMEsx5faXr1Pjz119sR7MJs2yo69v6uHRoMCmJs"),
    },
    "BTC": {
        "network": "BITCOIN",
        "address": os.getenv("WATCH_BTC", "bc1qlteerv5krqw3mrn5fzvpqwukavvn0mdnzn85j2"),
    },
}


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_tables() -> None:
    conn = db()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS wallet_seen_txs (
            tx_key TEXT PRIMARY KEY,
            tx_hash TEXT NOT NULL,
            symbol TEXT NOT NULL,
            network TEXT NOT NULL,
            amount_text TEXT,
            raw_json TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.commit()
    conn.close()


def seen(tx_key: str) -> bool:
    conn = db()
    row = conn.execute("SELECT 1 FROM wallet_seen_txs WHERE tx_key=?", (tx_key,)).fetchone()
    conn.close()
    return row is not None


def mark_seen(tx_key: str, tx_hash: str, symbol: str, network: str, amount_text: str, raw: dict) -> None:
    conn = db()
    conn.execute(
        "INSERT OR IGNORE INTO wallet_seen_txs(tx_key, tx_hash, symbol, network, amount_text, raw_json) VALUES (?,?,?,?,?,?)",
        (tx_key, tx_hash, symbol, network, amount_text, json.dumps(raw)[:4000]),
    )
    conn.commit()
    conn.close()


def _safe_key(symbol: str, network: str, tx_hash: str, amount_text: str) -> str:
    return hashlib.sha256(f"{symbol}|{network}|{tx_hash}|{amount_text}".encode()).hexdigest()


def fetch_tron_trc20(address: str) -> List[dict]:
    if not address:
        return []

    url = f"https://apilist.tronscanapi.com/api/token_trc20/transfers?limit=20&relatedAddress={address}&direction=to"
    data = requests.get(url, timeout=20).json()
    return data.get("token_transfers", [])


def fetch_btc(address: str) -> List[dict]:
    if not address:
        return []

    url = f"https://blockstream.info/api/address/{address}/txs"
    return requests.get(url, timeout=20).json()[:20]


def get_new_tron_payments() -> List[dict]:
    """
    Returns only new incoming TRON/TRC20 payments.
    Telegram message sending is handled by bot.py.
    """
    init_tables()
    new_payments = []

    tron_addr = WATCH["USDT_TRC20"]["address"]

    for tx in fetch_tron_trc20(tron_addr):
        tx_hash = tx.get("transaction_id", "")
        amount = str(tx.get("quant", ""))
        symbol = tx.get("tokenInfo", {}).get("tokenAbbr", "USDT")

        if not tx_hash:
            continue

        key = _safe_key(symbol, "TRON", tx_hash, amount)

        if seen(key):
            continue

        mark_seen(key, tx_hash, symbol, "TRON", amount, tx)

        new_payments.append(
            {
                "symbol": symbol,
                "network": "TRON",
                "amount": amount,
                "tx_hash": tx_hash,
                "raw": tx,
            }
        )

    return new_payments


def get_new_btc_payments() -> List[dict]:
    """
    Returns only new incoming BTC payments to the watched BTC address.
    Telegram message sending is handled by bot.py.
    """
    init_tables()
    new_payments = []

    btc_addr = WATCH["BTC"]["address"]

    for tx in fetch_btc(btc_addr):
        txid = tx.get("txid", "")

        if not txid:
            continue

        incoming_sats = sum(
            v.get("value", 0)
            for v in tx.get("vout", [])
            if v.get("scriptpubkey_address") == btc_addr
        )

        if incoming_sats <= 0:
            continue

        amount_btc = incoming_sats / 100_000_000
        key = _safe_key("BTC", "BITCOIN", txid, str(amount_btc))

        if seen(key):
            continue

        mark_seen(key, txid, "BTC", "BITCOIN", str(amount_btc), tx)

        new_payments.append(
            {
                "symbol": "BTC",
                "network": "BITCOIN",
                "amount": str(amount_btc),
                "tx_hash": txid,
                "raw": tx,
            }
        )

    return new_payments


def get_new_payments() -> List[dict]:
    """
    Main function used by bot.py.
    It checks all supported chains and returns all new incoming payments.
    """
    payments = []
    payments.extend(get_new_tron_payments())
    payments.extend(get_new_btc_payments())
    return payments


if __name__ == "__main__":
    print("wallet_notifier.py is now a helper module.")
    print("Run bot.py to start Telegram bot and wallet notifications together.")
