"""Pure cascade-skip predicate.

Kept dependency-light (no Dramatiq/Redis imports) so the chain semantics are
unit-testable without standing up the broker. See BenchmarkModule.skip_if_prev_failed.
"""
from __future__ import annotations

from app.db.models import ModuleRunStatus


def blocks_chain(status: ModuleRunStatus, passed: bool | None) -> bool:
    """Whether a module's outcome should cause a `skip_if_prev_failed` successor
    to be skipped.

    A module *blocks* the chain when it FAILED, was itself SKIPPED, or ran to
    completion but breached a redline (``passed is False``). A clean pass
    (``passed is True``) or a successful run with no redlines configured
    (``passed is None``) does NOT block — the chain continues.
    """
    if status in (ModuleRunStatus.FAILED, ModuleRunStatus.SKIPPED):
        return True
    return passed is False
