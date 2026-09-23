"""Unit tests for the cascade-skip predicate (app.queue.skip_logic.blocks_chain).

Pure/offline: no DB connection, no broker — just the enum + the predicate.
"""
from __future__ import annotations

import pytest

from app.db.models import ModuleRunStatus
from app.queue.skip_logic import blocks_chain

S = ModuleRunStatus


@pytest.mark.parametrize(
    "status,passed,expected",
    [
        # A clean pass never blocks the chain.
        (S.DONE, True, False),
        # Ran but breached a redline -> blocks.
        (S.DONE, False, True),
        # Ran fine with no redlines configured (passed is None) -> does NOT block.
        (S.DONE, None, False),
        # Hard failure / error -> blocks regardless of passed.
        (S.FAILED, False, True),
        (S.FAILED, None, True),
        (S.FAILED, True, True),
        # Already skipped -> blocks, so the skip cascades down the chain.
        (S.SKIPPED, None, True),
        (S.SKIPPED, True, True),
    ],
)
def test_blocks_chain(status, passed, expected):
    assert blocks_chain(status, passed) is expected


def test_only_passed_false_blocks_for_nonterminal_status():
    # Defensive: a non-terminal status only blocks if passed was explicitly False.
    assert blocks_chain(S.RUNNING, None) is False
    assert blocks_chain(S.PENDING, None) is False
    assert blocks_chain(S.RUNNING, False) is True
