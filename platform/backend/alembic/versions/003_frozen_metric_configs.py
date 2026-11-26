"""Keep frozen BFCL rules separate from attempt-specific display configs.

Revision ID: 003_frozen_metric_configs
Revises: 003_timed_datasets
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "003_frozen_metric_configs"
down_revision = "003_timed_datasets"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "submission_runs",
        sa.Column("frozen_metric_configs_json", postgresql.JSONB(), nullable=True),
    )
    # Existing snapshots are the best available historical rules. Never read
    # current benchmark settings to reconstruct a submitted run's contract.
    # Legacy display entries have no provenance, so preserve them rather than
    # guessing which were configured by the admin and which came from a run.
    op.execute("""
        UPDATE submission_runs
        SET frozen_metric_configs_json = metric_configs_json
        WHERE module_name = 'bfcl_v4'
    """)


def downgrade() -> None:
    op.drop_column("submission_runs", "frozen_metric_configs_json")
