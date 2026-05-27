import logging
import os
import sqlite3
from typing import Dict
from urllib.parse import quote_plus

import requests
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
OWNER_USERNAME = os.getenv("OWNER_USERNAME", "@owner")
LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID", "0"))
DB_PATH = os.getenv("DB_PATH", "bot.db")
GAME_CURRENCY = "USD_GAME"

ASSETS: Dict[str, Dict[str, str]] = {
    "USDT": {
        "TRON": os.getenv("ADDR_USDT_TRON", "SET_USDT_TRON_ADDRESS"),
        "BSC": os.getenv("ADDR_USDT_BSC", "SET_USDT_BSC_ADDRESS"),
        "ETHEREUM": os.getenv("ADDR_USDT_ETHEREUM", "SET_USDT_ETH_ADDRESS"),
        "SOLANA": os.getenv("ADDR_USDT_SOLANA", "SET_USDT_SOLANA_ADDRESS"),
        "POLYGON": os.getenv("ADDR_USDT_POLYGON", "SET_USDT_POLYGON_ADDRESS"),
        "TON": os.getenv("ADDR_USDT_TON", "SET_USDT_TON_ADDRESS"),
    },
    "USDC": {
        "ETHEREUM": os.getenv("ADDR_USDC_ETHEREUM", "SET_USDC_ETH_ADDRESS"),
        "SOLANA": os.getenv("ADDR_USDC_SOLANA", "SET_USDC_SOLANA_ADDRESS"),
        "BSC": os.getenv("ADDR_USDC_BSC", "SET_USDC_BSC_ADDRESS"),
        "POLYGON": os.getenv("ADDR_USDC_POLYGON", "SET_USDC_POLYGON_ADDRESS"),
    },
    "BTC": {"BITCOIN": os.getenv("ADDR_BTC_BITCOIN", "SET_BTC_ADDRESS")},
    "BCH": {"BITCOIN_CASH": os.getenv("ADDR_BCH_BITCOIN_CASH", "SET_BCH_ADDRESS")},
    "ETH": {
        "ETHEREUM": os.getenv("ADDR_ETH_ETHEREUM", "SET_ETH_ADDRESS"),
        "BSC": os.getenv("ADDR_ETH_BSC", "SET_ETH_BSC_ADDRESS"),
        "BASE": os.getenv("ADDR_ETH_BASE", "SET_ETH_BASE_ADDRESS"),
    },
    "LTC": {"LITECOIN": os.getenv("ADDR_LTC_LITECOIN", "SET_LTC_ADDRESS")},
    "SOL": {"SOLANA": os.getenv("ADDR_SOL_SOLANA", "SET_SOL_ADDRESS")},
    "TRX": {"TRON": os.getenv("ADDR_TRX_TRON", "SET_TRX_ADDRESS")},
    "XMR": {"MONERO": os.getenv("ADDR_XMR_MONERO", "SET_XMR_ADDRESS")},
    "DAI": {
        "ETHEREUM": os.getenv("ADDR_DAI_ETHEREUM", "SET_DAI_ETH_ADDRESS"),
        "BSC": os.getenv("ADDR_DAI_BSC", "SET_DAI_BSC_ADDRESS"),
        "POLYGON": os.getenv("ADDR_DAI_POLYGON", "SET_DAI_POLYGON_ADDRESS"),
    },
    "DOGE": {"DOGECOIN": os.getenv("ADDR_DOGE_DOGECOIN", "SET_DOGE_ADDRESS")},
    "MATIC": {"POLYGON": os.getenv("ADDR_MATIC_POLYGON", "SET_MATIC_ADDRESS")},
    "BNB": {"BSC": os.getenv("ADDR_BNB_BSC", "SET_BNB_ADDRESS")},
    "TON": {"TONCOIN": os.getenv("ADDR_TON_TONCOIN", "SET_TON_ADDRESS")},
}


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    conn = db()
    conn.execute("CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT, balance REAL DEFAULT 0, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
    conn.execute("CREATE TABLE IF NOT EXISTS proofs (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, tx_hash TEXT NOT NULL UNIQUE, currency TEXT NOT NULL, network TEXT NOT NULL, amount REAL NOT NULL, status TEXT NOT NULL, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
    conn.execute("CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, event_type TEXT NOT NULL, payload TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
    conn.commit(); conn.close()


async def log_event(context: ContextTypes.DEFAULT_TYPE, user_id: int, event_type: str, payload: str) -> None:
    conn = db(); conn.execute("INSERT INTO events(user_id,event_type,payload) VALUES(?,?,?)", (user_id, event_type, payload)); conn.commit(); conn.close()
    if LOG_CHANNEL_ID:
        try:
            await context.bot.send_message(LOG_CHANNEL_ID, f"[{event_type}] user={user_id}\n{payload}")
        except Exception as e:
            logger.warning("log channel error: %s", e)


def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💳 Deposit", callback_data="menu:deposit")],
        [InlineKeyboardButton("🏧 Withdraw", callback_data="menu:withdraw")],
    ])


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    u = update.effective_user
    conn = db()
    conn.execute("INSERT INTO users(user_id,username,first_name) VALUES(?,?,?) ON CONFLICT(user_id) DO UPDATE SET username=excluded.username,first_name=excluded.first_name", (u.id, u.username, u.first_name))
    conn.commit(); bal = conn.execute("SELECT balance FROM users WHERE user_id=?", (u.id,)).fetchone()["balance"]; conn.close()
    await log_event(context, u.id, "START", f"username=@{u.username}")
    await update.message.reply_text(f"Welcome {u.first_name}!\nBalance: {bal:.2f} {GAME_CURRENCY}\n1 USD = 1 {GAME_CURRENCY}", reply_markup=main_menu())


def cur_buttons(action: str) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(c, callback_data=f"{action}:cur:{c}")] for c in ASSETS]
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data="menu:home")])
    return InlineKeyboardMarkup(rows)


def net_buttons(action: str, cur: str) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(n, callback_data=f"{action}:net:{cur}:{n}")] for n in ASSETS[cur]]
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data=f"menu:{action}")])
    return InlineKeyboardMarkup(rows)


def verify_tron_usdt(tx_hash: str, to_addr: str, amount: float) -> bool:
    data = requests.get(f"https://apilist.tronscanapi.com/api/transaction-info?hash={tx_hash}", timeout=20).json()
    transfers = data.get("trc20TransferInfo", [])
    for t in transfers:
        if str(t.get("to_address", "")).lower() == to_addr.lower():
            q = float(t.get("quant", 0) or 0)
            d = int(t.get("decimals", 6) or 6)
            if (q / (10 ** d)) >= amount:
                return True
    return False


def verify_btc(tx_hash: str, to_addr: str, amount: float) -> bool:
    tx = requests.get(f"https://blockstream.info/api/tx/{tx_hash}", timeout=20).json()
    sats_needed = int(amount * 100_000_000)
    for out in tx.get("vout", []):
        if out.get("scriptpubkey_address") == to_addr and int(out.get("value", 0)) >= sats_needed:
            return True
    return False


def auto_verify(currency: str, network: str, tx_hash: str, amount: float) -> tuple[bool, str]:
    to_addr = ASSETS[currency][network]
    try:
        if currency == "USDT" and network == "TRON":
            return verify_tron_usdt(tx_hash, to_addr, amount), "TRON check"
        if currency == "BTC" and network == "BITCOIN":
            return verify_btc(tx_hash, to_addr, amount), "BTC check"
        return False, "Auto-verify not configured yet for this currency/network"
    except Exception as e:
        return False, f"Verifier error: {e}"


async def menu_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query; await q.answer(); data = q.data
    if data == "menu:home":
        await q.edit_message_text("Main menu", reply_markup=main_menu()); return
    if data == "menu:deposit":
        await q.edit_message_text("Choose currency to deposit", reply_markup=cur_buttons("deposit")); return
    if data == "menu:withdraw":
        await q.edit_message_text("Choose currency to withdraw", reply_markup=cur_buttons("withdraw")); return

    parts = data.split(":")
    if len(parts) == 3 and parts[1] == "cur":
        await q.edit_message_text(f"Choose network for {parts[2]}", reply_markup=net_buttons(parts[0], parts[2])); return

    if len(parts) == 4 and parts[1] == "net":
        action, currency, network = parts[0], parts[2], parts[3]
        if action == "deposit":
            addr = ASSETS[currency][network]
            qr = f"https://api.qrserver.com/v1/create-qr-code/?size=260x260&data={quote_plus(addr)}"
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Verify Payment", callback_data=f"verify:start:{currency}:{network}")],
                [InlineKeyboardButton("📋 Copy Address", callback_data=f"copy:addr:{currency}:{network}")],
                [InlineKeyboardButton("⬅️ Back", callback_data="menu:deposit")],
            ])
            await q.edit_message_text(f"Deposit {currency} on {network}\n\nAddress:\n`{addr}`\n\nQR:\n{qr}", parse_mode="Markdown", reply_markup=kb)
            return
        await q.edit_message_text(f"Withdraw {currency} on {network}\n\nContact owner: {OWNER_USERNAME}", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="menu:withdraw")]]))
        return

    if len(parts) == 4 and parts[0] == "copy" and parts[1] == "addr":
        currency, network = parts[2], parts[3]
        addr = ASSETS[currency][network]
        await q.message.reply_text(f"Copy this address:\n`{addr}`", parse_mode="Markdown")
        return

    if len(parts) == 4 and parts[0] == "verify" and parts[1] == "start":
        currency, network = parts[2], parts[3]
        context.user_data["awaiting_verify"] = {"currency": currency, "network": network}
        await q.edit_message_text(
            f"Send TX hash and amount for {currency} on {network}.\nFormat:\nTX_HASH, AMOUNT\nExample:\n0xabc..., 1.5",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="menu:deposit")]]),
        )


async def proof_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if "awaiting_verify" not in context.user_data:
        return
    st = context.user_data["awaiting_verify"]
    try:
        tx_hash, amount_raw = [x.strip() for x in (update.message.text or "").split(",", 1)]
        amount = float(amount_raw)
        if amount <= 0:
            raise ValueError
    except Exception:
        await update.message.reply_text("Invalid format. Use: TX_HASH, AMOUNT")
        return

    currency, network = st["currency"], st["network"]
    user_id = update.effective_user.id
    ok, reason = auto_verify(currency, network, tx_hash, amount)

    conn = db()
    try:
        status = "approved" if ok else "rejected"
        conn.execute("INSERT INTO proofs(user_id,tx_hash,currency,network,amount,status) VALUES (?,?,?,?,?,?)", (user_id, tx_hash, currency, network, amount, status))
    except sqlite3.IntegrityError:
        conn.close()
        await update.message.reply_text("This TX hash was already used before.")
        return

    if ok:
        conn.execute("UPDATE users SET balance = balance + ? WHERE user_id=?", (amount, user_id))
        conn.commit(); conn.close()
        await update.message.reply_text(f"✅ Payment verified automatically. +{amount} {GAME_CURRENCY} added.")
        await log_event(context, user_id, "AUTO_VERIFY_OK", f"{currency}/{network} {tx_hash} amount={amount}")
    else:
        conn.commit(); conn.close()
        await update.message.reply_text(f"❌ Could not verify payment automatically. Reason: {reason}")
        await log_event(context, user_id, "AUTO_VERIFY_FAIL", f"{currency}/{network} {tx_hash} amount={amount} reason={reason}")

    context.user_data.pop("awaiting_verify", None)


def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("Set BOT_TOKEN env var")
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(menu_router))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, proof_handler))
    app.run_polling()


if __name__ == "__main__":
    main()
