"""The one error type every log source raises.

Its own module so that `victorialogs` and `sources` can both import it without
importing each other.
"""
from __future__ import annotations


class LogSourceError(RuntimeError):
    """A source could not be read for this window (after any retries), or was
    configured in a way it cannot serve. The message is shown to an admin."""
