"""Dramatiq broker configured for Redis."""
from __future__ import annotations

import dramatiq
from dramatiq.brokers.redis import RedisBroker
from dramatiq.middleware import CurrentMessage

from app.core.config import settings
from app.queue.heartbeat import HeartbeatMiddleware

redis_broker = RedisBroker(url=settings.REDIS_URL)
redis_broker.add_middleware(HeartbeatMiddleware())
# Exposes the in-flight message to actors via CurrentMessage.get_current_message()
# — used for Dramatiq retry-count detection in jobs.py.
redis_broker.add_middleware(CurrentMessage())
dramatiq.set_broker(redis_broker)

broker = redis_broker
