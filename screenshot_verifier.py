import os
import re
import sqlite3
from typing import Optional

import requests

DB_PATH = os.getenv("DB_PATH", "bot.db")
OCR_SPACE_API_KEY = os.getenv("OCR_SPACE_API_KEY", "K82601088888957")


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def extract_text_from_image(image_path: str) -> str:
    """OCR via OCR.Space API. Returns extracted text."""
    if not OCR_SPACE_API_KEY:
        raise RuntimeError("Set OCR_SPACE_API_KEY")
    with open(image_path, "rb") as f:
        r = requests.post(
            "https://api.ocr.space/parse/image",
            files={"filename": f},
            data={"apikey": OCR_SPACE_API_KEY, "language": "eng", "isOverlayRequired": False},
            timeout=60,
        )
    r.raise_for_status()
    data = r.json()
    parsed = data.get("ParsedResults", [])
    return "\n".join(x.get("ParsedText", "") for x in parsed)


def parse_tx_hash(ocr_text: str) -> Optional[str]:
    m = re.search(r"\b(0x[a-fA-F0-9]{64})\b", ocr_text)
    if m:
        return m.group(1)
    m = re.search(r"\b([a-fA-F0-9]{64})\b", ocr_text)
    return m.group(1) if m else None


def proof_exists(tx_hash: str) -> bool:
    conn = db()
    row = conn.execute("SELECT 1 FROM proofs WHERE tx_hash=?", (tx_hash,)).fetchone()
    conn.close()
    return row is not None


def verify_screenshot(image_path: str) -> dict:
    """
    Basic screenshot check:
    1) OCR text extraction.
    2) Try to find tx hash-like pattern.
    3) Check duplicate proof in DB.

    NOTE: This checks screenshot text only. It does NOT guarantee on-chain validity by itself.
    """
    text = extract_text_from_image(image_path)
    tx_hash = parse_tx_hash(text)
    if not tx_hash:
        return {"ok": False, "reason": "No TX hash found in screenshot", "tx_hash": None}

    if proof_exists(tx_hash):
        return {"ok": False, "reason": "Duplicate proof (tx hash already submitted)", "tx_hash": tx_hash}

    return {"ok": True, "reason": "Potentially valid screenshot proof", "tx_hash": tx_hash}
