"""Fernet encryption for stored API keys."""
from __future__ import annotations

import base64

from cryptography.fernet import Fernet

from app.core.config import settings


def _pad_key(key: str) -> bytes:
    """Ensure the key is exactly 32 URL-safe base64 bytes."""
    raw = key.encode()
    # Use a deterministic stretch: hash the key then take 32 bytes
    import hashlib
    h = hashlib.sha256(raw).digest()
    return base64.urlsafe_b64encode(h)


_fernet: Fernet | None = None


def _get_fernet() -> Fernet:
    global _fernet
    if _fernet is None:
        _fernet = Fernet(_pad_key(settings.PLATFORM_SECRET_KEY))
    return _fernet


def encrypt_api_key(api_key: str) -> str:
    """Encrypt an API key. Returns a URL-safe base64 string."""
    return _get_fernet().encrypt(api_key.encode()).decode()


def decrypt_api_key(encrypted: str) -> str:
    """Decrypt an API key. Raises InvalidToken on bad input."""
    return _get_fernet().decrypt(encrypted.encode()).decode()
