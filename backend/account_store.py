import json
import os
import uuid
from pathlib import Path
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes
import base64

DATA_DIR = Path.home() / ".freehive"
ACCOUNTS_FILE = DATA_DIR / "accounts.json"
KEY_FILE = DATA_DIR / ".key"

def _ensure_dir():
    DATA_DIR.mkdir(exist_ok=True)

def _get_or_create_key() -> bytes:
    _ensure_dir()
    if KEY_FILE.exists():
        return KEY_FILE.read_bytes()
    key = Fernet.generate_key()
    KEY_FILE.write_bytes(key)
    KEY_FILE.chmod(0o600)
    return key

def _get_fernet() -> Fernet:
    key = _get_or_create_key()
    return Fernet(key)

def _load_accounts() -> dict:
    _ensure_dir()
    if not ACCOUNTS_FILE.exists():
        return {}
    try:
        return json.loads(ACCOUNTS_FILE.read_text())
    except Exception:
        return {}

def _save_accounts(data: dict):
    _ensure_dir()
    ACCOUNTS_FILE.write_text(json.dumps(data, indent=2))
    ACCOUNTS_FILE.chmod(0o600)

def add_account(model: str, username: str, password: str) -> dict:
    f = _get_fernet()
    encrypted_password = f.encrypt(password.encode()).decode()
    account = {
        "id": str(uuid.uuid4()),
        "model": model,
        "username": username,
        "password": encrypted_password,
        "status": "active"
    }
    data = _load_accounts()
    if model not in data:
        data[model] = []
    data[model].append(account)
    _save_accounts(data)
    return {
        "id": account["id"],
        "model": model,
        "username": username,
        "status": "active"
    }

def get_accounts(model: str = None) -> list:
    data = _load_accounts()
    result = []
    if model:
        for acc in data.get(model, []):
            result.append({
                "id": acc["id"],
                "model": acc["model"],
                "username": acc["username"],
                "status": acc["status"]
            })
    else:
        for model_accounts in data.values():
            for acc in model_accounts:
                result.append({
                    "id": acc["id"],
                    "model": acc["model"],
                    "username": acc["username"],
                    "status": acc["status"]
                })
    return result

def get_credentials(account_id: str) -> dict | None:
    f = _get_fernet()
    data = _load_accounts()
    for model_accounts in data.values():
        for acc in model_accounts:
            if acc["id"] == account_id:
                return {
                    "username": acc["username"],
                    "password": f.decrypt(acc["password"].encode()).decode()
                }
    return None

def remove_account(account_id: str) -> bool:
    data = _load_accounts()
    for model in data:
        original = len(data[model])
        data[model] = [a for a in data[model] if a["id"] != account_id]
        if len(data[model]) < original:
            _save_accounts(data)
            return True
    return False

def update_status(account_id: str, status: str):
    data = _load_accounts()
    for model in data:
        for acc in data[model]:
            if acc["id"] == account_id:
                acc["status"] = status
                _save_accounts(data)
                return True
    return False