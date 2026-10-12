"""Collection profiles record who created them.

A service account (another platform, such as LLM AutoTune) may now create
collection profiles and manage the ones it created, the same rule as for
benchmarks. Existing profiles get no owner: only admins manage them, as before.

Revision ID: 002_profile_owner
Revises: 001_initial
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "002_profile_owner"
down_revision = "001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "replay_dataset_profiles",
        sa.Column("created_by_user_id", sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        "replay_dataset_profiles_created_by_user_id_fkey",
        "replay_dataset_profiles", "users",
        ["created_by_user_id"], ["id"], ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "replay_dataset_profiles_created_by_user_id_fkey",
        "replay_dataset_profiles", type_="foreignkey",
    )
    op.drop_column("replay_dataset_profiles", "created_by_user_id")
