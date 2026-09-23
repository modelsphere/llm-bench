"""Deploy cordon: a Redis flag that makes the API refuse NEW submissions.

An operator sets this before a *disruptive* deploy — one where graceful worker
drain isn't enough — to create a quiet window:

  * a destructive (non-backward-compatible) DB migration, which a draining
    old-code worker could choke on, or
  * avoiding two worker generations co-resident for the whole drain budget when
    a very long (12-20h) submission is in flight.

The flow (docs/deploying.md, "Disruptive upgrades"): set the flag -> wait for
RUNNING submissions to drain to zero -> deploy -> clear the flag. While set, the
submit endpoints reject new work with HTTP 503 so nothing new starts.

It's a single Redis key (shared across backend replicas, survives a backend
restart). It does NOT survive a Redis restart — acceptable, since a Redis
restart is itself a disruptive event and the operator re-cordons.
"""
from __future__ import annotations

import logging
from typing import Optional

import redis

from app.core.config import settings

logger = logging.getLogger(__name__)

CORDON_KEY = "platform:cordon"
_DEFAULT_MESSAGE = "The platform is updating; new submissions are paused. Please retry in a few minutes."

_redis_client = None


def _get_redis():
    global _redis_client
    if _redis_client is None:
        _redis_client = redis.from_url(settings.REDIS_URL, decode_responses=True)
    return _redis_client


def cordon_reason() -> Optional[str]:
    """Return the operator-facing reason if submissions are cordoned, else None.

    Fails OPEN: if Redis is unreachable we return None (allow submissions) rather
    than locking every user out on an infra blip. The downside of a missed
    cordon is mild — a new submission just runs on the current code, which the
    graceful-drain rollout already tolerates.
    """
    try:
        value = _get_redis().get(CORDON_KEY)
    except Exception:
        logger.warning("cordon: Redis check failed — treating as un-cordoned", exc_info=True)
        return None
    if value is None:
        return None
    # An empty value still means "cordoned"; fall back to the default message.
    return value or _DEFAULT_MESSAGE
