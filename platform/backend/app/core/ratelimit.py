"""Redis-backed fixed-window rate limiting for auth endpoints.

Protects the public, unauthenticated surface (login brute force, register spam)
that would otherwise be wide open. State lives in Redis so it is shared across
backend replicas and survives a backend restart.

Fails OPEN: if Redis is unreachable, requests are allowed rather than blocked —
availability is chosen over strictness, matching app/core/cordon.py. A Redis
outage therefore disables throttling; that is an accepted trade-off (the
alternative locks every user out during a blip).

Client IP note: the app runs behind nginx/HAProxy, so we read the left-most
X-Forwarded-For entry as the caller. That header is client-settable unless a
trusted proxy overwrites it, so treat per-IP limits as best-effort — the
per-account (email) limit is the harder guarantee for brute-force defence.
"""
from __future__ import annotations

import logging

import redis.asyncio as aioredis
from fastapi import HTTPException, Request, status

from app.core.config import settings

logger = logging.getLogger(__name__)

_redis: aioredis.Redis | None = None


def _get_redis() -> aioredis.Redis:
    global _redis
    if _redis is None:
        _redis = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
    return _redis


def client_ip(request: Request) -> str:
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _too_many(retry_after: int) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail="Too many attempts. Please wait and try again.",
        headers={"Retry-After": str(max(retry_after, 1))},
    )


async def _incr(key: str, window: int) -> int:
    """Increment `key`, setting its TTL to `window` on first hit. Returns count."""
    r = _get_redis()
    n = await r.incr(key)
    if n == 1:
        await r.expire(key, window)
    return int(n)


async def _ttl(key: str) -> int:
    try:
        t = await _get_redis().ttl(key)
        return t if t and t > 0 else 1
    except Exception:
        return 1


async def enforce_attempt(key: str, limit: int, window: int) -> None:
    """Count this attempt and raise 429 if it exceeds `limit` within `window`."""
    try:
        n = await _incr(key, window)
    except Exception:
        logger.warning("rate limit: Redis unavailable, allowing (fail-open): %s", key, exc_info=True)
        return
    if n > limit:
        raise _too_many(await _ttl(key))


async def check_locked(key: str, limit: int) -> None:
    """Raise 429 if `key` has already reached `limit` — WITHOUT incrementing.

    Used to short-circuit a locked-out account before doing any password work.
    """
    try:
        cur = await _get_redis().get(key)
    except Exception:
        logger.warning("rate limit: Redis unavailable, allowing (fail-open): %s", key, exc_info=True)
        return
    if cur is not None and int(cur) >= limit:
        raise _too_many(await _ttl(key))


async def record_failure(key: str, window: int) -> None:
    """Bump a failure counter (used for per-account login lockout)."""
    try:
        await _incr(key, window)
    except Exception:
        logger.warning("rate limit: Redis unavailable, not counting failure: %s", key, exc_info=True)


async def reset(key: str) -> None:
    """Clear a counter (e.g. drop the failure count after a successful login)."""
    try:
        await _get_redis().delete(key)
    except Exception:
        pass
