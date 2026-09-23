"""JWT auth utilities: encode, decode, and password hashing."""
from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

from jose import JWTError, jwt
from passlib.context import CryptContext
from pydantic import BaseModel

from app.core.config import settings

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# Personal API keys are presented in the same `Authorization: Bearer <token>`
# header as JWTs. This prefix lets get_current_user tell them apart: JWTs are
# base64url segments joined by dots and never start with this string.
API_KEY_PREFIX = "llmb_"

# Password-reset tokens use their own prefix so they can never be mistaken for a
# personal API key (they are submitted to /auth/reset-password, not the
# Authorization header, but a distinct prefix keeps them unambiguous anyway).
RESET_TOKEN_PREFIX = "llmr_"


def generate_api_key() -> tuple[str, str, str]:
    """Mint a new API key.

    Returns (full_key, display_prefix, key_hash). Only the hash is persisted;
    the full key is returned to the caller exactly once.
    """
    full_key = API_KEY_PREFIX + secrets.token_urlsafe(32)
    return full_key, full_key[:12], hash_api_key(full_key)


def generate_reset_token() -> tuple[str, str, str]:
    """Mint a single-use password-reset token.

    Returns (full_token, display_prefix, token_hash). Only the hash is
    persisted; the full token is shown to the approving super_admin exactly
    once. Same high-entropy + sha256-at-rest scheme as generate_api_key.
    """
    full_token = RESET_TOKEN_PREFIX + secrets.token_urlsafe(32)
    return full_token, full_token[:12], hash_api_key(full_token)


def hash_api_key(key: str) -> str:
    """SHA-256 hex digest. Fine for high-entropy random keys (unlike passwords,
    no slow KDF is needed — and we can't afford bcrypt on every API request)."""
    return hashlib.sha256(key.encode()).hexdigest()


class TokenData(BaseModel):
    sub: str  # user id
    role: str
    exp: datetime
    iat: datetime | None = None  # issued-at; compared against User.password_changed_at


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


def get_password_hash(password: str) -> str:
    return pwd_context.hash(password)


# Precomputed once at import. When a login names an account that doesn't exist,
# we still run one verify against this hash so the response takes ~the same time
# as a real check — otherwise the fast "no such user" path leaks which emails are
# registered (user enumeration by timing).
_DUMMY_PASSWORD_HASH = pwd_context.hash("account-enumeration-mitigation")


def dummy_verify(plain: str) -> None:
    """Burn ~one bcrypt verify without revealing that no user matched."""
    verify_password(plain, _DUMMY_PASSWORD_HASH)


def create_access_token(data: dict[str, Any], expires_delta: timedelta | None = None) -> str:
    to_encode = dict(data)
    now = datetime.now(UTC)
    expire = now + (expires_delta or timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES))
    to_encode["exp"] = expire
    # iat lets a password reset invalidate tokens minted before it; see
    # resolve_user_from_token's password_changed_at check.
    to_encode.setdefault("iat", now)
    return jwt.encode(to_encode, settings.SECRET_KEY, algorithm=settings.ALGORITHM)


def decode_access_token(token: str) -> TokenData | None:
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        iat = payload.get("iat")
        return TokenData(
            sub=str(payload["sub"]),
            role=str(payload["role"]),
            exp=datetime.fromtimestamp(payload["exp"], tz=UTC),
            iat=datetime.fromtimestamp(iat, tz=UTC) if iat is not None else None,
        )
    except JWTError:
        return None
