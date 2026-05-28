import logging
import os
import sqlite3
import asyncio
import tempfile
import time
from pathlib import Path
from decimal import Decimal
from typing import Dict, Optional, Tuple
from urllib.parse import quote_plus

import requests
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update

try:
    from telegram import CopyTextButton
except Exception:
    CopyTextButton = None

from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN", "8915702735:AAF5XIqDqchFbjSmB9gAKCHmdAaNipqKECM")
OWNER_USERNAME = os.getenv("OWNER_USERNAME", "@Tfben10")
LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID", "-1003849178352"))
DB_PATH = os.getenv("DB_PATH", "bot.db")

ETHERSCAN_API_KEY = os.getenv("ETHERSCAN_API_KEY", "IECDPGB7N3KYGRISFCNZVHU6M4YMW8JTFY")
BSCSCAN_API_KEY = os.getenv("BSCSCAN_API_KEY", "IECDPGB7N3KYGRISFCNZVHU6M4YMW8JTFY")

BSC_RPC_URL = os.getenv("BSC_RPC_URL", "https://bsc-rpc.publicnode.com")
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

WALLET_NOTIFIER_ENABLED = os.getenv("WALLET_NOTIFIER_ENABLED", "1") == "1"
WALLET_NOTIFIER_INTERVAL = int(os.getenv("NOTIFIER_POLL_SECONDS", "30"))

VERIFY_TIME_LIMIT_SECONDS = 210

try:
    import wallet_notifier
except Exception as e:
    wallet_notifier = None
    logger.warning("wallet_notifier.py could not be imported: %s", e)

try:
    import screenshot_verifier
except Exception as e:
    screenshot_verifier = None
    logger.warning("screenshot_verifier.py could not be imported: %s", e)

GAME_CURRENCY = "USD_GAME"

ASSETS: Dict[str, Dict[str, str]] = {
    "USDT": {
        "ETHEREUM": os.getenv("ADDR_USDT_ETHEREUM", "0xdBaa89C688B7A84145A5aDcC5C2b7cC62bf10181"),
        "TRON": os.getenv("ADDR_USDT_TRON", "TUGany2B2ZyTEd3zpf6A6NTdiFaQpPL8qb"),
        "BSC": os.getenv("ADDR_USDT_BSC", "0xB2Cf047d110005D7FacdE31d92357072f9D1FfAC"),
        "SOLANA": os.getenv("ADDR_USDT_SOLANA", "EKmcFRMEsx5faXr1Pjz119sR7MJs2yo69v6uHRoMCmJs"),
        "POLYGON": os.getenv("ADDR_USDT_POLYGON", "0xdBaa89C688B7A84145A5aDcC5C2b7cC62bf10181"),
    },
    "USDC": {
        "ETHEREUM": os.getenv("ADDR_USDC_ETHEREUM", "0xdBaa89C688B7A84145A5aDcC5C2b7cC62bf10181"),
        "BSC": os.getenv("ADDR_USDC_BSC", "0xdBaa89C688B7A84145A5aDcC5C2b7cC62bf10181"),
        "SOLANA": os.getenv("ADDR_USDC_SOLANA", "EKmcFRMEsx5faXr1Pjz119sR7MJs2yo69v6uHRoMCmJs"),
        "BASE": os.getenv("ADDR_USDC_BASE", "0xdbaa89c688b7a84145a5adcc5c2b7cc62bf10181"),
    },
    "BITCOIN": {
        "BITCOIN": os.getenv("ADDR_BTC_BITCOIN", "bc1qlteerv5krqw3mrn5fzvpqwukavvn0mdnzn85j2"),
    },
}

TOKEN_CONTRACTS = {
    ("USDT", "ETHEREUM"): "0xdAC17F958D2ee523a2206206994597C13D831ec7",
    ("USDT", "BSC"): "0x55d398326f99059fF775485246999027B3197955",
    ("USDT", "POLYGON"): "0xc2132D05D31c914a87C6611C10748AEb04B58e8F",
    ("USDC", "ETHEREUM"): "0xA0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",
    ("USDC", "BSC"): "0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d",
    ("USDC", "BASE"): "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
}

TOKEN_DECIMALS = {
    ("USDT", "ETHEREUM"): 6,
    ("USDT", "BSC"): 18,
    ("USDT", "POLYGON"): 6,
    ("USDC", "ETHEREUM"): 6,
    ("USDC", "BSC"): 18,
    ("USDC", "BASE"): 6,
}

ETHERSCAN_V2_CHAIN_IDS = {
    "ETHEREUM": "1",
    "POLYGON": "137",
    "BASE": "8453",
}


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    conn = db()
    cur = conn.cursor()

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            balance REAL DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS proofs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            tx_hash TEXT NOT NULL UNIQUE,
            currency TEXT NOT NULL,
            network TEXT NOT NULL,
            amount REAL NOT NULL,
            status TEXT NOT NULL DEFAULT 'approved',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            event_type TEXT NOT NULL,
            payload TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    conn.commit()
    conn.close()


async def log_event(
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    event_type: str,
    payload: str,
) -> None:
    conn = db()
    conn.execute(
        "INSERT INTO events(user_id, event_type, payload) VALUES (?, ?, ?)",
        (user_id, event_type, payload),
    )
    conn.commit()
    conn.close()

    if LOG_CHANNEL_ID:
        try:
            await context.bot.send_message(
                LOG_CHANNEL_ID,
                f"[{event_type}] user={user_id}\n{payload}",
            )
        except Exception as e:
            logger.warning("Log channel send failed: %s", e)


def currency_buttons(action: str) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(c, callback_data=f"{action}:cur:{c}")] for c in ASSETS]
    rows.append([InlineKeyboardButton("Back", callback_data="menu:home")])
    return InlineKeyboardMarkup(rows)


def network_buttons(action: str, currency: str) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(n, callback_data=f"{action}:net:{currency}:{n}")]
        for n in ASSETS[currency].keys()
    ]
    rows.append([InlineKeyboardButton("Back", callback_data=f"menu:{action}")])
    return InlineKeyboardMarkup(rows)


def make_copy_button(address: str) -> InlineKeyboardButton:
    if CopyTextButton is not None:
        try:
            return InlineKeyboardButton(
                "Copy Address",
                copy_text=CopyTextButton(text=address),
            )
        except TypeError:
            pass

    return InlineKeyboardButton(
        "Copy Address",
        callback_data=f"copyaddr:{address[:48]}",
    )


def deposit_keyboard(
    currency: str,
    network: str,
    address: str,
    qr_url: str,
) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [make_copy_button(address)],
            [InlineKeyboardButton("Open QR", url=qr_url)],
            [InlineKeyboardButton("Verify", callback_data=f"deposit:verify:{currency}:{network}")],
            [InlineKeyboardButton("Back", callback_data="menu:deposit")],
        ]
    )


def verify_timer_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Verify Hash", callback_data="verifyhash:start")],
        ]
    )


def retry_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Retry", callback_data="verifyhash:retry")],
        ]
    )


def format_seconds(seconds: int) -> str:
    if seconds < 0:
        seconds = 0

    minutes = seconds // 60
    secs = seconds % 60
    return f"{minutes:02d}:{secs:02d}"


def verify_timer_text(currency: str, network: str, remaining: int) -> str:
    return (
        f"Coin: {currency}\n"
        f"Network: {network}\n"
        f"Time left: {format_seconds(remaining)}\n\n"
        f"Verify within the above time.\n"
        f"If time is over, verification will not happen.\n\n"
        f"Do not go back from the QR/address page unless you verify.\n"
        f"Only verify the payment for the same coin and same network you selected."
    )


def cancel_verify_timer_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    job = context.user_data.get("verify_timer_job")

    if job:
        try:
            job.schedule_removal()
        except Exception:
            pass

    context.user_data.pop("verify_timer_job", None)


def get_verify_session(context: ContextTypes.DEFAULT_TYPE) -> Optional[dict]:
    return context.user_data.get("verify_session")


def session_expired(session: dict) -> bool:
    return time.time() > float(session.get("expires_at", 0))


def extract_amount_from_caption(caption: str) -> Optional[Decimal]:
    if not caption:
        return None

    for raw in caption.replace(",", " ").split():
        cleaned = raw.strip().replace("$", "")

        try:
            amount = Decimal(cleaned)

            if amount > 0:
                return amount

        except Exception:
            continue

    return None


def proof_exists(tx_hash: str) -> bool:
    conn = db()
    row = conn.execute("SELECT 1 FROM proofs WHERE tx_hash=?", (tx_hash,)).fetchone()
    conn.close()
    return row is not None


def credit_user_payment(
    user_id: int,
    tx_hash: str,
    currency: str,
    network: str,
    amount: Decimal,
) -> Tuple[bool, str]:
    conn = db()

    try:
        existing = conn.execute("SELECT 1 FROM proofs WHERE tx_hash=?", (tx_hash,)).fetchone()

        if existing:
            return False, "This transaction hash is already used."

        conn.execute(
            "INSERT INTO proofs(user_id, tx_hash, currency, network, amount, status) VALUES (?,?,?,?,?, 'approved')",
            (user_id, tx_hash, currency, network, float(amount)),
        )

        conn.execute(
            "UPDATE users SET balance = balance + ? WHERE user_id=?",
            (float(amount), user_id),
        )

        conn.commit()
        return True, "credited"

    except sqlite3.IntegrityError:
        return False, "This transaction hash is already used."

    finally:
        conn.close()


def verify_tron_token_from_hash(
    tx_hash: str,
    expected_to: str,
    currency: str,
) -> Tuple[bool, str, Optional[Decimal]]:
    try:
        r = requests.get(
            f"https://apilist.tronscanapi.com/api/transaction-info?hash={tx_hash}",
            timeout=20,
        )
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        return False, f"TRON API error: {e}", None

    transfers = data.get("trc20TransferInfo") or []

    for t in transfers:
        to_addr = (t.get("to_address") or "").strip()
        symbol = (t.get("symbol") or "").upper()
        amount = Decimal(str(t.get("amount_str") or "0"))

        if symbol == currency.upper() and to_addr.lower() == expected_to.lower() and amount > 0:
            return True, f"{currency} TRON payment found", amount

    return False, f"No matching {currency} TRON payment to your wallet found", None


def verify_bsc_token_from_hash(
    tx_hash: str,
    currency: str,
    expected_to: str,
) -> Tuple[bool, str, Optional[Decimal]]:
    contract = TOKEN_CONTRACTS.get((currency, "BSC"))

    if not contract:
        return False, f"No BSC contract configured for {currency}", None

    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "eth_getTransactionReceipt",
        "params": [tx_hash],
    }

    try:
        r = requests.post(BSC_RPC_URL, json=payload, timeout=20)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        return False, f"BSC RPC error: {e}", None

    if data.get("error"):
        return False, f"BSC RPC error: {data.get('error')}", None

    receipt = data.get("result")

    if not receipt:
        return False, "Transaction not found on BSC yet. Try again after confirmation.", None

    if receipt.get("status") != "0x1":
        return False, "BSC transaction failed on-chain.", None

    expected_contract = contract.lower()
    expected_to_clean = expected_to.lower().replace("0x", "")
    decimals = TOKEN_DECIMALS.get((currency, "BSC"), 18)

    for log in receipt.get("logs", []):
        log_address = (log.get("address") or "").lower()

        if log_address != expected_contract:
            continue

        topics = log.get("topics", [])

        if len(topics) < 3:
            continue

        if topics[0].lower() != TRANSFER_TOPIC:
            continue

        to_topic = topics[2].lower().replace("0x", "")

        if not to_topic.endswith(expected_to_clean):
            continue

        raw_value_hex = log.get("data", "0x0")
        raw_value = int(raw_value_hex, 16)
        amount = Decimal(raw_value) / (Decimal(10) ** decimals)

        if amount > 0:
            return True, f"{currency} BSC payment found", amount

    return False, f"No matching {currency} BSC transfer to your wallet found in this hash.", None


def verify_erc20_from_hash(
    network: str,
    tx_hash: str,
    currency: str,
    expected_to: str,
) -> Tuple[bool, str, Optional[Decimal]]:
    contract = TOKEN_CONTRACTS.get((currency, network))

    if not contract:
        return False, f"No token contract configured for {currency} on {network}", None

    host = "https://api.etherscan.io/v2/api"
    key = ETHERSCAN_API_KEY
    chain_id = ETHERSCAN_V2_CHAIN_IDS.get(network)

    if not chain_id:
        return False, f"No Etherscan V2 chain ID configured for {network}", None

    if not key:
        return False, "Missing Etherscan V2 API key", None

    params = {
        "chainid": chain_id,
        "module": "account",
        "action": "tokentx",
        "address": expected_to,
        "contractaddress": contract,
        "page": 1,
        "offset": 100,
        "sort": "desc",
        "apikey": key,
    }

    try:
        r = requests.get(host, params=params, timeout=20)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        return False, f"Etherscan V2 API error: {e}", None

    result = data.get("result", [])

    if isinstance(result, str):
        return False, f"Etherscan V2 error: {result}", None

    for t in result:
        if (t.get("hash") or "").lower() != tx_hash.lower():
            continue

        to_addr = (t.get("to") or "").lower()

        if to_addr != expected_to.lower():
            return False, "Transaction hash found but it was not sent to your deposit address", None

        token_symbol = (t.get("tokenSymbol") or "").upper()

        if token_symbol != currency.upper():
            return False, f"Transaction token is {token_symbol}, not {currency}", None

        decimals = int(t.get("tokenDecimal") or TOKEN_DECIMALS.get((currency, network), 18))
        value = Decimal(t.get("value") or "0") / (Decimal(10) ** decimals)

        if value > 0:
            return True, f"{currency} {network} payment found", value

    return False, "Transaction not found for selected coin/network/address", None


def verify_btc_from_hash(
    tx_hash: str,
    expected_to: str,
) -> Tuple[bool, str, Optional[Decimal]]:
    try:
        r = requests.get(f"https://blockstream.info/api/tx/{tx_hash}", timeout=20)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        return False, f"Bitcoin API error: {e}", None

    incoming_sats = sum(
        v.get("value", 0)
        for v in data.get("vout", [])
        if v.get("scriptpubkey_address") == expected_to
    )

    if incoming_sats <= 0:
        return False, "No BTC payment to your deposit address found in this hash", None

    amount_btc = Decimal(incoming_sats) / Decimal(100_000_000)
    return True, "BTC payment found", amount_btc


def verify_payment_from_hash(
    currency: str,
    network: str,
    tx_hash: str,
    expected_address: str,
) -> Tuple[bool, str, Optional[Decimal]]:
    if proof_exists(tx_hash):
        return False, "This transaction hash is already used.", None

    if currency in {"USDT", "USDC"} and network == "TRON":
        return verify_tron_token_from_hash(tx_hash, expected_address, currency)

    if currency in {"USDT", "USDC"} and network == "BSC":
        return verify_bsc_token_from_hash(tx_hash, currency, expected_address)

    if currency in {"USDT", "USDC"} and network in {"ETHEREUM", "POLYGON", "BASE"}:
        return verify_erc20_from_hash(network, tx_hash, currency, expected_address)

    if currency == "BITCOIN" and network == "BITCOIN":
        return verify_btc_from_hash(tx_hash, expected_address)

    return False, f"Auto hash verification is not supported yet for {currency} on {network}", None


def verify_tron_usdt(
    tx_hash: str,
    expected_to: str,
    expected_amount: Decimal,
) -> Tuple[bool, str]:
    ok, reason, amount = verify_tron_token_from_hash(tx_hash, expected_to, "USDT")

    if not ok:
        return False, reason

    if amount is not None and amount >= expected_amount:
        return True, f"USDT TRON payment found: {amount}"

    return False, "Amount is lower than expected"


def verify_erc20_scan(
    network: str,
    tx_hash: str,
    currency: str,
    expected_to: str,
    expected_amount: Decimal,
) -> Tuple[bool, str]:
    if network == "BSC":
        ok, reason, amount = verify_bsc_token_from_hash(tx_hash, currency, expected_to)
    else:
        ok, reason, amount = verify_erc20_from_hash(network, tx_hash, currency, expected_to)

    if not ok:
        return False, reason

    if amount is not None and amount >= expected_amount:
        return True, f"{currency} {network} payment found: {amount}"

    return False, "Amount is lower than expected"


def verify_payment(
    currency: str,
    network: str,
    tx_hash: str,
    amount: Decimal,
    expected_address: str,
) -> Tuple[bool, str]:
    if currency == "USDT" and network == "TRON":
        return verify_tron_usdt(tx_hash, expected_address, amount)

    if currency in {"USDT", "USDC"} and network in {"ETHEREUM", "BSC", "POLYGON", "BASE"}:
        return verify_erc20_scan(network, tx_hash, currency, expected_address, amount)

    if currency == "BITCOIN" and network == "BITCOIN":
        ok, reason, actual_amount = verify_btc_from_hash(tx_hash, expected_address)

        if ok and actual_amount is not None and actual_amount >= amount:
            return True, f"BTC payment found: {actual_amount}"

        return False, reason

    return False, f"Auto verify not supported yet for {currency} on {network}"


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user

    conn = db()
    conn.execute(
        "INSERT INTO users(user_id, username, first_name) VALUES(?,?,?) ON CONFLICT(user_id) DO UPDATE SET username=excluded.username, first_name=excluded.first_name",
        (user.id, user.username, user.first_name),
    )
    conn.commit()

    bal = conn.execute("SELECT balance FROM users WHERE user_id=?", (user.id,)).fetchone()["balance"]
    conn.close()

    kb = [
        [InlineKeyboardButton("Deposit", callback_data="menu:deposit")],
        [InlineKeyboardButton("Withdraw", callback_data="menu:withdraw")],
    ]

    await update.message.reply_text(
        f"Welcome {user.first_name}!\nBalance: {bal:.2f} {GAME_CURRENCY}",
        reply_markup=InlineKeyboardMarkup(kb),
    )


async def verify_timer_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    data = context.job.data
    chat_id = data["chat_id"]
    message_id = data["message_id"]
    user_id = data["user_id"]
    currency = data["currency"]
    network = data["network"]
    expires_at = data["expires_at"]

    remaining = int(expires_at - time.time())

    user_data = context.application.user_data.get(user_id, {})
    session = user_data.get("verify_session")

    if not session:
        context.job.schedule_removal()
        return

    if session.get("completed"):
        context.job.schedule_removal()
        return

    if remaining <= 0:
        session["expired"] = True
        session["awaiting_hash"] = False

        try:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=(
                    f"Coin: {currency}\n"
                    f"Network: {network}\n"
                    f"Time left: 00:00\n\n"
                    f"Time up. Verification failed.\n"
                    f"Contact owner: {OWNER_USERNAME}"
                ),
            )
        except Exception:
            pass

        context.job.schedule_removal()
        return

    try:
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=verify_timer_text(currency, network, remaining),
            reply_markup=verify_timer_keyboard(),
        )
    except Exception:
        pass


async def menu_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    data = q.data

    if data.startswith("copyaddr:"):
        await q.answer(
            "Address is shown in the message. Long-press it to copy.",
            show_alert=True,
        )
        return

    if data == "verifyhash:start" or data == "verifyhash:retry":
        session = get_verify_session(context)

        if not session:
            await q.message.reply_text("No active verification session. Please open Deposit again.")
            return

        if session_expired(session) or session.get("expired"):
            session["expired"] = True
            session["awaiting_hash"] = False

            await q.message.reply_text(
                f"Time up. Verification failed.\nContact owner: {OWNER_USERNAME}"
            )
            return

        session["awaiting_hash"] = True

        await q.message.reply_text(
            "Send your transaction hash and not anything else or transaction will fail."
        )
        return

    if data == "menu:home":
        kb = [
            [InlineKeyboardButton("Deposit", callback_data="menu:deposit")],
            [InlineKeyboardButton("Withdraw", callback_data="menu:withdraw")],
        ]

        await q.edit_message_text(
            "Main menu",
            reply_markup=InlineKeyboardMarkup(kb),
        )
        return

    if data == "menu:deposit":
        await q.edit_message_text(
            "Choose currency to deposit",
            reply_markup=currency_buttons("deposit"),
        )
        return

    if data == "menu:withdraw":
        await q.edit_message_text(
            "Choose currency to withdraw",
            reply_markup=currency_buttons("withdraw"),
        )
        return

    parts = data.split(":")
    action = parts[0]

    if len(parts) >= 3 and parts[1] == "cur":
        currency = parts[2]

        await q.edit_message_text(
            f"Choose network for {currency}",
            reply_markup=network_buttons(action, currency),
        )
        return

    if len(parts) == 4 and parts[1] == "net":
        currency, network = parts[2], parts[3]

    elif len(parts) == 4 and parts[1] == "verify":
        currency, network = parts[2], parts[3]
        expires_at = time.time() + VERIFY_TIME_LIMIT_SECONDS

        cancel_verify_timer_job(context)

        context.user_data["verify_session"] = {
            "currency": currency,
            "network": network,
            "expires_at": expires_at,
            "awaiting_hash": False,
            "expired": False,
            "completed": False,
            "timer_chat_id": q.message.chat_id,
            "timer_message_id": q.message.message_id,
        }

        await q.edit_message_text(
            verify_timer_text(currency, network, VERIFY_TIME_LIMIT_SECONDS),
            reply_markup=verify_timer_keyboard(),
        )

        if context.job_queue is not None:
            job = context.job_queue.run_repeating(
                verify_timer_job,
                interval=1,
                first=1,
                name=f"verify_timer_{q.from_user.id}",
                data={
                    "chat_id": q.message.chat_id,
                    "message_id": q.message.message_id,
                    "user_id": q.from_user.id,
                    "currency": currency,
                    "network": network,
                    "expires_at": expires_at,
                },
            )
            context.user_data["verify_timer_job"] = job

        return

    else:
        return

    if action == "deposit":
        addr = ASSETS[currency][network]
        qr = f"https://api.qrserver.com/v1/create-qr-code/?size=260x260&data={quote_plus(addr)}"

        await q.edit_message_text(
            f"Deposit {currency} on {network}\n\n"
            f"Address:\n{addr}\n\n"
            f"QR:\n{qr}\n\n"
            f"Do not go back from this QR/address page unless you verify your payment.",
            reply_markup=deposit_keyboard(currency, network, addr, qr),
        )

    elif action == "withdraw":
        await q.edit_message_text(
            f"Withdraw {currency} on {network}\n\nPlease contact owner: {OWNER_USERNAME}",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("Back", callback_data="menu:withdraw")]]
            ),
        )


async def proof_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    session = get_verify_session(context)

    if not session:
        return

    if session_expired(session) or session.get("expired"):
        session["expired"] = True
        session["awaiting_hash"] = False

        await update.message.reply_text(
            f"Time up. Verification failed.\nContact owner: {OWNER_USERNAME}"
        )
        return

    if not session.get("awaiting_hash"):
        await update.message.reply_text(
            "Please click Verify Hash first, then send only your transaction hash."
        )
        return

    tx_hash = (update.message.text or "").strip()

    if not tx_hash or " " in tx_hash or "," in tx_hash or "\n" in tx_hash:
        await update.message.reply_text(
            "Transaction failed. Send only the transaction hash and nothing else.",
            reply_markup=retry_keyboard(),
        )
        return

    currency = session["currency"]
    network = session["network"]
    expected_addr = ASSETS[currency][network]

    verifying_message = await update.message.reply_text("Verifying hash...")

    ok, reason, detected_amount = await asyncio.to_thread(
        verify_payment_from_hash,
        currency,
        network,
        tx_hash,
        expected_addr,
    )

    if not ok or detected_amount is None:
        await verifying_message.edit_text(
            f"Verification failed.\n\n"
            f"Reason: {reason}\n\n"
            f"Make sure you verify the same coin and network you selected.\n"
            f"Coin: {currency}\n"
            f"Network: {network}",
            reply_markup=retry_keyboard(),
        )
        return

    user_id = update.effective_user.id

    credited, credit_reason = credit_user_payment(
        user_id,
        tx_hash,
        currency,
        network,
        detected_amount,
    )

    if not credited:
        await verifying_message.edit_text(
            f"Verification failed.\n\n"
            f"Reason: {credit_reason}",
            reply_markup=retry_keyboard(),
        )
        return

    session["completed"] = True
    session["awaiting_hash"] = False
    cancel_verify_timer_job(context)

    await verifying_message.edit_text(
        f"Verification done.\n\n"
        f"Coin: {currency}\n"
        f"Network: {network}\n"
        f"Amount added: {detected_amount} {GAME_CURRENCY}"
    )

    timer_chat_id = session.get("timer_chat_id")
    timer_message_id = session.get("timer_message_id")

    if timer_chat_id and timer_message_id:
        try:
            await context.bot.edit_message_text(
                chat_id=timer_chat_id,
                message_id=timer_message_id,
                text=(
                    f"Verification completed.\n\n"
                    f"Coin: {currency}\n"
                    f"Network: {network}\n"
                    f"Amount added: {detected_amount} {GAME_CURRENCY}"
                ),
            )
        except Exception:
            pass

    context.user_data.pop("verify_session", None)

    await log_event(
        context,
        user_id,
        "HASH_AUTO_VERIFIED",
        f"{tx_hash} {currency}/{network} amount={detected_amount}",
    )


async def photo_proof_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    session = get_verify_session(context)

    if not session:
        await update.message.reply_text(
            "Please choose Deposit -> currency -> network -> Verify before sending a screenshot."
        )
        return

    if session_expired(session) or session.get("expired"):
        session["expired"] = True
        await update.message.reply_text(
            f"Time up. Verification failed.\nContact owner: {OWNER_USERNAME}"
        )
        return

    if screenshot_verifier is None:
        await update.message.reply_text(
            "Screenshot verifier is not available. Make sure screenshot_verifier.py is in the same folder as bot.py."
        )
        return

    amount = extract_amount_from_caption(update.message.caption or "")

    if amount is None:
        await update.message.reply_text(
            "Please send the screenshot again with amount in the caption, for example: 10.50"
        )
        return

    photo = update.message.photo[-1]
    tg_file = await context.bot.get_file(photo.file_id)

    with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg") as tmp:
        image_path = tmp.name

    try:
        await tg_file.download_to_drive(image_path)
        result = await asyncio.to_thread(
            screenshot_verifier.verify_screenshot,
            image_path,
        )

    except Exception as e:
        await update.message.reply_text(f"Screenshot verification error: {e}")
        return

    finally:
        try:
            Path(image_path).unlink(missing_ok=True)
        except Exception:
            pass

    if not result.get("ok"):
        await update.message.reply_text(
            f"Screenshot rejected: {result.get('reason')}"
        )
        return

    tx_hash = result.get("tx_hash")
    currency = session["currency"]
    network = session["network"]
    expected_addr = ASSETS[currency][network]

    ok, reason, detected_amount = await asyncio.to_thread(
        verify_payment_from_hash,
        currency,
        network,
        tx_hash,
        expected_addr,
    )

    if not ok or detected_amount is None:
        await update.message.reply_text(
            f"On-chain verification failed: {reason}",
            reply_markup=retry_keyboard(),
        )
        return

    user_id = update.effective_user.id

    credited, credit_reason = credit_user_payment(
        user_id,
        tx_hash,
        currency,
        network,
        detected_amount,
    )

    if not credited:
        await update.message.reply_text(
            f"Verification failed: {credit_reason}",
            reply_markup=retry_keyboard(),
        )
        return

    session["completed"] = True
    cancel_verify_timer_job(context)
    context.user_data.pop("verify_session", None)

    await update.message.reply_text(
        f"Screenshot and payment verified.\n"
        f"Coin: {currency}\n"
        f"Network: {network}\n"
        f"Amount added: {detected_amount} {GAME_CURRENCY}"
    )

    await log_event(
        context,
        user_id,
        "SCREENSHOT_AUTO_VERIFIED",
        f"{tx_hash} {currency}/{network} amount={detected_amount}",
    )


def fallback_get_new_payments_from_old_wallet_notifier() -> list:
    payments = []

    if wallet_notifier is None:
        return payments

    wallet_notifier.init_tables()

    tron_addr = wallet_notifier.WATCH.get("USDT_TRC20", {}).get("address")

    for tx in wallet_notifier.fetch_tron_trc20(tron_addr):
        tx_hash = tx.get("transaction_id", "")
        amount = str(tx.get("quant", ""))
        symbol = tx.get("tokenInfo", {}).get("tokenAbbr", "USDT")

        if not tx_hash:
            continue

        key = wallet_notifier._safe_key(symbol, "TRON", tx_hash, amount)

        if wallet_notifier.seen(key):
            continue

        wallet_notifier.mark_seen(key, tx_hash, symbol, "TRON", amount, tx)

        payments.append(
            {
                "symbol": symbol,
                "network": "TRON",
                "amount": amount,
                "tx_hash": tx_hash,
                "raw": tx,
            }
        )

    btc_addr = wallet_notifier.WATCH.get("BTC", {}).get("address")

    for tx in wallet_notifier.fetch_btc(btc_addr):
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
        key = wallet_notifier._safe_key("BTC", "BITCOIN", txid, str(amount_btc))

        if wallet_notifier.seen(key):
            continue

        wallet_notifier.mark_seen(key, txid, "BTC", "BITCOIN", str(amount_btc), tx)

        payments.append(
            {
                "symbol": "BTC",
                "network": "BITCOIN",
                "amount": str(amount_btc),
                "tx_hash": txid,
                "raw": tx,
            }
        )

    return payments


async def wallet_notifier_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    if wallet_notifier is None:
        return

    try:
        if hasattr(wallet_notifier, "get_new_payments"):
            payments = await asyncio.to_thread(wallet_notifier.get_new_payments)
        else:
            payments = await asyncio.to_thread(fallback_get_new_payments_from_old_wallet_notifier)

        for payment in payments:
            symbol = payment["symbol"]
            network = payment["network"]
            amount = payment["amount"]
            tx_hash = payment["tx_hash"]

            await context.bot.send_message(
                chat_id=wallet_notifier.OWNER_ID,
                text=(
                    f"New incoming payment detected\n"
                    f"{symbol} on {network}\n"
                    f"Amount: {amount}\n"
                    f"TX: {tx_hash}"
                ),
            )

    except Exception as e:
        logger.warning("Wallet notifier job failed: %s", e)


async def start_wallet_notifier(app: Application) -> None:
    if not WALLET_NOTIFIER_ENABLED:
        return

    if wallet_notifier is None:
        logger.warning("Wallet notifier disabled because wallet_notifier.py was not imported.")
        return

    if app.job_queue is None:
        logger.warning("Wallet notifier needs python-telegram-bot[job-queue].")
        logger.warning('Install with: pip install "python-telegram-bot[job-queue]" requests')
        return

    wallet_notifier.init_tables()

    app.job_queue.run_repeating(
        wallet_notifier_job,
        interval=WALLET_NOTIFIER_INTERVAL,
        first=5,
        name="wallet_notifier",
    )

    logger.info(
        "Wallet notifier background job enabled every %s seconds",
        WALLET_NOTIFIER_INTERVAL,
    )


def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("Set BOT_TOKEN env var")

    init_db()

    if wallet_notifier is not None:
        wallet_notifier.init_tables()

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(start_wallet_notifier)
        .build()
    )

    app.add_handler(CommandHandler("start", start))

    app.add_handler(
        CallbackQueryHandler(
            menu_router,
            pattern=r"^(menu:|deposit:|withdraw:|copyaddr:|verifyhash:).+",
        )
    )

    app.add_handler(MessageHandler(filters.PHOTO, photo_proof_handler))

    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            proof_handler,
        )
    )

    app.run_polling()


if __name__ == "__main__":
    main()
