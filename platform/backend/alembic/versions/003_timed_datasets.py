"""Collection profiles can build timed datasets.

`layout = timed` keeps every request of one window in time order, with each
request's send time and session, for a replay on the recorded timing.
`session_field` names where the logs keep a session id; `max_idle_gap_s` is
how long an idle stretch may stay. Existing profiles stay `sampled`.

Revision ID: 003_timed_datasets
Revises: 002_profile_owner
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "003_timed_datasets"
down_revision = "002_profile_owner"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("replay_dataset_profiles", sa.Column(
        "layout", sa.String(length=16), nullable=False, server_default="sampled"))
    op.add_column("replay_dataset_profiles", sa.Column(
        "session_field", sa.String(length=200), nullable=False, server_default=""))
    op.add_column("replay_dataset_profiles", sa.Column(
        "max_idle_gap_s", sa.Float(), nullable=False, server_default=sa.text("5.0")))


def downgrade() -> None:
    op.drop_column("replay_dataset_profiles", "max_idle_gap_s")
    op.drop_column("replay_dataset_profiles", "session_field")
    op.drop_column("replay_dataset_profiles", "layout")
