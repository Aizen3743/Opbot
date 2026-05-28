import logging
import os
import sqlite3
import asyncio
import tempfile
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
ETHERSCAN_API_KEY = os.getenv("ETHERSCAN_API_KEY", "")
BSCSCAN_API_KEY = os.getenv("BSCSCAN_API_KEY", "")

WALLET_NOTIFIER_ENABLED = os.getenv("WALLET_NOTIFIER_ENABLED", "1") == "1"
WALLET_NOTIFIER_INTERVAL = int(os.getenv("NOTIFIER_POLL_SECONDS", "30"))

try:
    import wallet_notifier as wallet_notifier_module
except Exception as e:
    wallet_notifier_module = None
    logger.warning("wallet_notifier.py could not be imported: %s", e)

try:
    import screenshot_verifier as screenshot_verifier_module
except Exception as e:
    screenshot_verifier_module = None
    logger.warning("screenshot_verifier.py could not be imported: %s", e)

GAME_CURRENCY = "USD_GAME"

ASSETS: Dict[str, Dict[str, str]] = {
    "USDT": {
        "ETHEREUM": os.getenv("ADDR_USDT_ETHEREUM", "SET_USDT_ETH_ADDRESS"),
        "TRON": os.getenv("ADDR_USDT_TRON", "SET_USDT_TRON_ADDRESS"),
        "BSC": os.getenv("ADDR_USDT_BSC", "SET_USDT_BSC_ADDRESS"),
    },
    "USDC": {
        "ETHEREUM": os.getenv("ADDR_USDC_ETHEREUM", "SET_USDC_ETH_ADDRESS"),
        "BSC": os.getenv("ADDR_USDC_BSC", "SET_USDC_BSC_ADDRESS"),
    },
    "BTC": {"BITCOIN": os.getenv("ADDR_BTC_BITCOIN", "SET_BTC_ADDRESS")},
}

TOKEN_CONTRACTS = {
    ("USDT", "ETHEREUM"): "0xdAC17F958D2ee523a2206206994597C13D831ec7",
    ("USDT", "BSC"): "0x55d398326f99059fF775485246999027B3197955",
    ("USDC", "ETHEREUM"): "0xA0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",
    ("USDC", "BSC"): "0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d",
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


async def log_event(context: ContextTypes.DEFAULT_TYPE, user_id: int, event_type: str, payload: str) -> None:
    conn = db()
    conn.execute("INSERT INTO events(user_id, event_type, payload) VALUES (?, ?, ?)", (user_id, event_type, payload))
    conn.commit()
    conn.close()
    if LOG_CHANNEL_ID:
        try:
            await context.bot.send_message(LOG_CHANNEL_ID, f"[{event_type}] user={user_id}\n{payload}")
        except Exception as e:
            logger.warning("Log channel send failed: %s", e)


def currency_buttons(action: str) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(c, callback_data=f"{action}:cur:{c}")] for c in ASSETS]
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data="menu:home")])
    return InlineKeyboardMarkup(rows)


def network_buttons(action: str, currency: str) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(n, callback_data=f"{action}:net:{currency}:{n}")] for n in ASSETS[currency].keys()]
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data=f"menu:{action}")])
    return InlineKeyboardMarkup(rows)


def make_copy_button(address: str) -> InlineKeyboardButton:
    """Create a Telegram copy button when supported, otherwise show copy instructions."""
    if CopyTextButton is not None:
        try:
            return InlineKeyboardButton("📋 Copy Address", copy_text=CopyTextButton(text=address))
        except TypeError:
            pass
    return InlineKeyboardButton("📋 Copy Address", callback_data=f"copyaddr:{address[:48]}")


def deposit_keyboard(currency: str, network: str, address: str, qr_url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [make_copy_button(address)],
            [InlineKeyboardButton("🔳 Open QR", url=qr_url)],
            [InlineKeyboardButton("✅ Verify", callback_data=f"deposit:verify:{currency}:{network}")],
            [InlineKeyboardButton("⬅️ Back", callback_data="menu:deposit")],
        ]
    )


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


def verify_tron_usdt(tx_hash: str, expected_to: str, expected_amount: Decimal) -> Tuple[bool, str]:
    r = requests.get(f"https://apilist.tronscanapi.com/api/transaction-info?hash={tx_hash}", timeout=20)
    data = r.json()
    trc20 = data.get("trc20TransferInfo") or []
    for t in trc20:
        to_addr = (t.get("to_address") or "").strip()
        amount = Decimal(str(t.get("amount_str") or "0"))
        symbol = (t.get("symbol") or "").upper()
        if symbol == "USDT" and to_addr.lower() == expected_to.lower() and amount >= expected_amount:
            return True, f"USDT TRON payment found: {amount}"
    return False, "No matching USDT TRON transfer to wallet found"


def verify_erc20_scan(network: str, tx_hash: str, currency: str, expected_to: str, expected_amount: Decimal) -> Tuple[bool, str]:
    contract = TOKEN_CONTRACTS.get((currency, network))
    if not contract:
        return False, "No contract configured"

    if network == "ETHEREUM":
        host, key = "https://api.etherscan.io/api", ETHERSCAN_API_KEY
    else:
        host, key = "https://api.bscscan.com/api", BSCSCAN_API_KEY

    if not key:
        return False, f"Missing {network} scan API key"

    params = {
        "module": "account",
        "action": "tokentx",
        "contractaddress": contract,
        "page": 1,
        "offset": 100,
        "sort": "desc",
        "apikey": key,
    }

    r = requests.get(host, params=params, timeout=20)
    data = r.json()

    for t in data.get("result", []):
        if (t.get("hash") or "").lower() != tx_hash.lower():
            continue

        to_addr = (t.get("to") or "").lower()
        if to_addr != expected_to.lower():
            continue

        decimals = int(t.get("tokenDecimal") or 0)
        value = Decimal(t.get("value") or "0") / (Decimal(10) ** decimals)

        if value >= expected_amount:
            return True, f"{currency} {network} payment found: {value}"

    return False, "Transaction not found or amount/address mismatch"


def verify_payment(currency: str, network: str, tx_hash: str, amount: Decimal, expected_address: str) -> Tuple[bool, str]:
    if currency == "USDT" and network == "TRON":
        return verify_tron_usdt(tx_hash, expected_address, amount)

    if currency in {"USDT", "USDC"} and network in {"ETHEREUM", "BSC"}:
        return verify_erc20_scan(network, tx_hash, currency, expected_address, amount)

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
        [InlineKeyboardButton("💳 Deposit", callback_data="menu:deposit")],
        [InlineKeyboardButton("🏧 Withdraw", callback_data="menu:withdraw")],
    ]

    await update.message.reply_text(
        f"Welcome {user.first_name}!\nBalance: {bal:.2f} {GAME_CURRENCY}",
        reply_markup=InlineKeyboardMarkup(kb),
    )


async def menu_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    data = q.data

    if data.startswith("copyaddr:"):
        await q.answer("Address is shown in the message. Long-press it to copy.", show_alert=True)
        return

    if data == "menu:home":
        kb = [
            [InlineKeyboardButton("💳 Deposit", callback_data="menu:deposit")],
            [InlineKeyboardButton("🏧 Withdraw", callback_data="menu:withdraw")],
        ]
        await q.edit_message_text("Main menu", reply_markup=InlineKeyboardMarkup(kb))
        return

    if data == "menu:deposit":
        await q.edit_message_text("Choose currency to deposit", reply_markup=currency_buttons("deposit"))
        return

    if data == "menu:withdraw":
        await q.edit_message_text("Choose currency to withdraw", reply_markup=currency_buttons("withdraw"))
        return

    parts = data.split(":")
    action = parts[0]

    if len(parts) >= 3 and parts[1] == "cur":
        currency = parts[2]
        await q.edit_message_text(f"Choose network for {currency}", reply_markup=network_buttons(action, currency))
        return

    if len(parts) == 4 and parts[1] == "net":
        currency, network = parts[2], parts[3]

    elif len(parts) == 4 and parts[1] == "verify":
        currency, network = parts[2], parts[3]
        context.user_data["verify_target"] = {"currency": currency, "network": network}

        await q.edit_message_text(
            f"Send payment proof for {currency} on {network}:\n\n"
            f"1) Text: TX_HASH, AMOUNT\n"
            f"2) Screenshot/photo with amount in caption, for example: 10.50\n\n"
            f"Screenshot OCR is checked first, then the transaction is verified on-chain where supported.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("⬅️ Back", callback_data=f"deposit:net:{currency}:{network}")]]
            ),
        )
        return

    else:
        return

    if action == "deposit":
        addr = ASSETS[currency][network]
        qr = f"https://api.qrserver.com/v1/create-qr-code/?size=260x260&data={quote_plus(addr)}"

        await q.edit_message_text(
            f"Deposit {currency} on {network}\n\nAddress:\n`{addr}`\n\nQR:\n{qr}",
            parse_mode="Markdown",
            reply_markup=deposit_keyboard(currency, network, addr, qr),
        )

    elif action == "withdraw":
        await q.edit_message_text(
            f"Withdraw {currency} on {network}\n\nPlease contact owner: {OWNER_USERNAME}",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="menu:withdraw")]]),
        )


async def proof_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    target = context.user_data.get("verify_target")

    if not target:
        return

    txt = (update.message.text or "").strip()
    chunks = [p.strip() for p in txt.split(",")]

    if len(chunks) != 2:
        await update.message.reply_text("Invalid format. Use: TX_HASH, AMOUNT")
        return

    tx_hash, amount_text = chunks
    currency = target["currency"]
    network = target["network"]

    try:
        amount = Decimal(amount_text)
        if amount <= 0:
            raise ValueError
    except Exception:
        await update.message.reply_text("Amount must be positive number")
        return

    conn = db()
    existing = conn.execute("SELECT 1 FROM proofs WHERE tx_hash=?", (tx_hash,)).fetchone()

    if existing:
        conn.close()
        await update.message.reply_text("This TX hash already used.")
        return

    expected_addr = ASSETS[currency][network]
    ok, reason = verify_payment(currency, network, tx_hash, amount, expected_addr)

    if not ok:
        conn.close()
        await update.message.reply_text(f"Verification failed: {reason}")
        return

    user_id = update.effective_user.id

    conn.execute(
        "INSERT INTO proofs(user_id, tx_hash, currency, network, amount, status) VALUES (?,?,?,?,?, 'approved')",
        (user_id, tx_hash, currency, network, float(amount)),
    )

    conn.execute("UPDATE users SET balance = balance + ? WHERE user_id=?", (float(amount), user_id))
    conn.commit()
    conn.close()

    context.user_data.pop("verify_target", None)

    await update.message.reply_text(f"✅ Payment verified automatically. +{amount} {GAME_CURRENCY} credited.")
    await log_event(context, user_id, "AUTO_VERIFIED", f"{tx_hash} {currency}/{network} amount={amount}")


async def photo_proof_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    target = context.user_data.get("verify_target")

    if not target:
        await update.message.reply_text("Please choose Deposit → currency → network → Verify before sending a screenshot.")
        return

    if screenshot_verifier_module is None:
        await update.message.reply_text("Screenshot verifier is not available. Make sure screenshot_verifier.py is in the same folder as bot.py.")
        return

    amount = extract_amount_from_caption(update.message.caption or "")

    if amount is None:
        await update.message.reply_text("Please send the screenshot again with amount in the caption, for example: 10.50")
        return

    photo = update.message.photo[-1]
    tg_file = await context.bot.get_file(photo.file_id)

    with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg") as tmp:
        image_path = tmp.name

    try:
        await tg_file.download_to_drive(image_path)
        result = await asyncio.to_thread(screenshot_verifier_module.verify_screenshot, image_path)

    except Exception as e:
        await update.message.reply_text(f"Screenshot verification error: {e}")
        return

    finally:
        try:
            Path(image_path).unlink(missing_ok=True)
        except Exception:
            pass

    if not result.get("ok"):
        await update.message.reply_text(f"Screenshot rejected: {result.get('reason')}")
        return

    tx_hash = result.get("tx_hash")
    currency = target["currency"]
    network = target["network"]
    expected_addr = ASSETS[currency][network]

    ok, reason = await asyncio.to_thread(verify_payment, currency, network, tx_hash, amount, expected_addr)

    if not ok:
        await update.message.reply_text(f"On-chain verification failed: {reason}")
        return

    user_id = update.effective_user.id
    conn = db()

    try:
        conn.execute(
            "INSERT INTO proofs(user_id, tx_hash, currency, network, amount, status) VALUES (?,?,?,?,?, 'approved')",
            (user_id, tx_hash, currency, network, float(amount)),
        )

        conn.execute("UPDATE users SET balance = balance + ? WHERE user_id=?", (float(amount), user_id))
        conn.commit()

    except sqlite3.IntegrityError:
        await update.message.reply_text("This TX hash already used.")
        return

    finally:
        conn.close()

    context.user_data.pop("verify_target", None)

    await update.message.reply_text(f"✅ Screenshot and payment verified. +{amount} {GAME_CURRENCY} credited.")
    await log_event(context, user_id, "SCREENSHOT_AUTO_VERIFIED", f"{tx_hash} {currency}/{network} amount={amount}")


async def wallet_notifier_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    if wallet_notifier_module is None:
        return

    try:
        wallet_notifier_module.init_tables()

        tron_addr = wallet_notifier_module.WATCH.get("USDT_TRC20", {}).get("address")

        for tx in await asyncio.to_thread(wallet_notifier_module.fetch_tron_trc20, tron_addr):
            tx_hash = tx.get("transaction_id", "")
            amount = str(tx.get("quant", ""))
            symbol = tx.get("tokenInfo", {}).get("tokenAbbr", "USDT")

            if not tx_hash:
                continue

            key = wallet_notifier_module._safe_key(symbol, "TRON", tx_hash, amount)

            if wallet_notifier_module.seen(key):
                continue

            wallet_notifier_module.mark_seen(key, tx_hash, symbol, "TRON", amount, tx)

            await context.bot.send_message(
                chat_id=wallet_notifier_module.OWNER_ID,
                text=f"🔔 New incoming payment detected\n{symbol} on TRON\nAmount: {amount}\nTX: {tx_hash}",
            )

        btc_addr = wallet_notifier_module.WATCH.get("BTC", {}).get("address")

        for tx in await asyncio.to_thread(wallet_notifier_module.fetch_btc, btc_addr):
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
            key = wallet_notifier_module._safe_key("BTC", "BITCOIN", txid, str(amount_btc))

            if wallet_notifier_module.seen(key):
                continue

            wallet_notifier_module.mark_seen(key, txid, "BTC", "BITCOIN", str(amount_btc), tx)

            await context.bot.send_message(
                chat_id=wallet_notifier_module.OWNER_ID,
                text=f"🔔 New BTC incoming payment detected\nAmount: {amount_btc} BTC\nTX: {txid}",
            )

    except Exception as e:
        logger.warning("Wallet notifier job failed: %s", e)


async def start_wallet_notifier(app: Application) -> None:
    if not WALLET_NOTIFIER_ENABLED:
        return

    if wallet_notifier_module is None:
        logger.warning("Wallet notifier disabled because wallet_notifier.py was not imported.")
        return

    if app.job_queue is None:
        logger.warning("Wallet notifier needs python-telegram-bot[job-queue]. Install it or run wallet_notifier.py separately.")
        return

    app.job_queue.run_repeating(
        wallet_notifier_job,
        interval=WALLET_NOTIFIER_INTERVAL,
        first=5,
        name="wallet_notifier",
    )

    logger.info("Wallet notifier background job enabled every %s seconds", WALLET_NOTIFIER_INTERVAL)


def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("Set BOT_TOKEN env var")

    init_db()

    if wallet_notifier_module is not None:
        wallet_notifier_module.init_tables()

    app = Application.builder().token(BOT_TOKEN).post_init(start_wallet_notifier).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(menu_router, pattern=r"^(menu:|deposit:|withdraw:|copyaddr:).+"))
    app.add_handler(MessageHandler(filters.PHOTO, photo_proof_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, proof_handler))

    app.run_polling()


if __name__ == "__main__":
    main()
