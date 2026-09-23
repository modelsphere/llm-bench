"""Dramatiq middleware: per-submission Redis heartbeats + worker boot/shutdown logs.

A worker process writes a Redis key `worker:submission:<id>` with a TTL of
HEARTBEAT_TTL_SECONDS when it picks up a run_submission task, and re-writes it
every HEARTBEAT_INTERVAL_SECONDS from a daemon thread. If the worker process
dies abruptly (segfault, OOM-kill, signal), the key expires and the backend
reaper marks the submission FAILED with a "Worker process crashed" error.

The middleware also logs worker-process boot/shutdown so ops can see
auto-restarts in pod stdout.
"""
from __future__ import annotations

import json
import logging
import os
import socket
import threading
import time
from typing import Optional

import dramatiq
from dramatiq.middleware import Middleware

logger = logging.getLogger(__name__)

HEARTBEAT_TTL_SECONDS = 60
HEARTBEAT_INTERVAL_SECONDS = 20
HEARTBEAT_KEY_PREFIX = "worker:submission:"

_TARGET_ACTOR = "run_submission"


def heartbeat_key(submission_id: int) -> str:
    return f"{HEARTBEAT_KEY_PREFIX}{submission_id}"


class HeartbeatMiddleware(Middleware):
    """Maintains a per-task Redis heartbeat and logs worker lifecycle events."""

    def __init__(self) -> None:
        self._redis = None
        # message_id -> (submission_id, stop_event, thread)
        self._tasks: dict[str, tuple[int, threading.Event, threading.Thread]] = {}
        self._lock = threading.Lock()
        self._hostname = socket.gethostname()

    # ---- redis lazy init ---------------------------------------------------
    def _get_redis(self):
        if self._redis is None:
            import redis
            from app.core.config import settings
            self._redis = redis.from_url(settings.REDIS_URL, decode_responses=False)
        return self._redis

    # ---- worker lifecycle --------------------------------------------------
    def after_worker_boot(self, broker, worker):
        logger.warning(
            "[worker pid=%d host=%s] booted — dramatiq worker process online",
            os.getpid(), self._hostname,
        )

    def before_worker_shutdown(self, broker, worker):
        logger.warning(
            "[worker pid=%d host=%s] shutting down",
            os.getpid(), self._hostname,
        )

    # ---- per-message heartbeat --------------------------------------------
    def before_process_message(self, broker, message):
        if message.actor_name != _TARGET_ACTOR:
            return
        submission_id = _extract_submission_id(message)
        if submission_id is None:
            return

        payload = json.dumps({
            "pid": os.getpid(),
            "host": self._hostname,
            "actor": message.actor_name,
            "message_id": message.message_id,
            "started_at": time.time(),
        }).encode()

        try:
            self._get_redis().set(heartbeat_key(submission_id), payload, ex=HEARTBEAT_TTL_SECONDS)
        except Exception:
            logger.warning("heartbeat: failed to set initial key for submission %d", submission_id)

        stop_event = threading.Event()
        thread = threading.Thread(
            target=self._refresh_loop,
            args=(submission_id, stop_event, payload),
            daemon=True,
            name=f"heartbeat-{submission_id}",
        )
        thread.start()
        with self._lock:
            self._tasks[message.message_id] = (submission_id, stop_event, thread)

    def after_process_message(self, broker, message, *, result=None, exception=None):
        if message.actor_name != _TARGET_ACTOR:
            return
        with self._lock:
            entry = self._tasks.pop(message.message_id, None)
        if entry is None:
            return
        submission_id, stop_event, thread = entry
        stop_event.set()
        thread.join(timeout=3)
        try:
            self._get_redis().delete(heartbeat_key(submission_id))
        except Exception:
            logger.warning("heartbeat: failed to delete key for submission %d", submission_id)

    # ---- internal refresh loop --------------------------------------------
    def _refresh_loop(
        self, submission_id: int, stop_event: threading.Event, payload: bytes
    ) -> None:
        # SET, not EXPIRE: EXPIRE is a no-op on a key that no longer exists, and
        # Redis here is a single replica with no persistence, so any Redis
        # restart drops every heartbeat permanently. An EXPIRE-only refresh
        # could never recreate them, so the reaper would mark every in-flight
        # submission FAILED within GRACE_SECONDS while the workers kept running
        # to completion — and the cancel-watcher only reacts to CANCELED, so
        # nothing would stop them. Re-SETting restores the key on the next tick.
        key = heartbeat_key(submission_id)
        while not stop_event.wait(timeout=HEARTBEAT_INTERVAL_SECONDS):
            try:
                self._get_redis().set(key, payload, ex=HEARTBEAT_TTL_SECONDS)
            except Exception:
                logger.warning("heartbeat: refresh failed for submission %d", submission_id)


def _extract_submission_id(message: "dramatiq.Message") -> Optional[int]:
    if message.args:
        try:
            return int(message.args[0])
        except (TypeError, ValueError):
            pass
    sid = message.kwargs.get("submission_id") if message.kwargs else None
    if sid is None:
        return None
    try:
        return int(sid)
    except (TypeError, ValueError):
        return None
