"""Initial schema + seed data.

Single squashed migration: creates the final-form schema for every table and
seeds reference data only — the card-type list and test_modules (from
bench.modules.MODULE_REGISTRY). Identities and the starter benchmark are
seed.py's job: a migration cannot ask for a password, and one that inserts a
default admin ships a known login to every install.

This is the first migration of the public repository. It is deliberately NOT
continuous with the internal chain it was squashed from, so there is no upgrade
path between the two — see docs/upgrading.md.

Revision ID: 001_initial
Revises:
"""
from __future__ import annotations

import json
import sys
from dataclasses import asdict, is_dataclass
from pathlib import Path

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "001_initial"
down_revision = None
branch_labels = None
depends_on = None


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


def _create_schema() -> None:
    op.create_table('card_types',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=100), nullable=False),
    sa.Column('display_order', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_card_types_name'), 'card_types', ['name'], unique=True)
    op.create_table('replay_dataset_profiles',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=64), nullable=False),
    sa.Column('display_name', sa.String(length=200), nullable=False),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('enabled', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('source_type', sa.String(length=32), server_default=sa.text("'bodylog_files'"), nullable=False),
    sa.Column('source_url', sa.String(length=500), nullable=False),
    sa.Column('models', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('statuses', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('forwarded_to', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('uris', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('exclude_truncated', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('extra_logsql', sa.Text(), nullable=True),
    sa.Column('window_hours', sa.Integer(), server_default=sa.text('24'), nullable=False),
    sa.Column('window_timezone', sa.String(length=64), server_default='UTC', nullable=False),
    sa.Column('subwindow_minutes', sa.Integer(), server_default=sa.text('60'), nullable=False),
    sa.Column('sample_size', sa.Integer(), server_default=sa.text('2000'), nullable=False),
    sa.Column('oversample_factor', sa.Float(), server_default=sa.text('3.0'), nullable=False),
    sa.Column('max_carry_multiple', sa.Integer(), server_default=sa.text('4'), nullable=False),
    sa.Column('max_bytes', sa.BigInteger(), server_default=sa.text('8589934592'), nullable=False),
    sa.Column('clean', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('max_model_len', sa.Integer(), server_default=sa.text('262144'), nullable=False),
    sa.Column('keep_response_body', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('compress', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('header_denylist', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('min_records', sa.Integer(), server_default=sa.text('100'), nullable=False),
    sa.Column('min_buckets', sa.Integer(), server_default=sa.text('1'), nullable=False),
    sa.Column('schedule_interval_hours', sa.Float(), server_default=sa.text('24.0'), nullable=False),
    sa.Column('schedule_anchor_hour', sa.Integer(), nullable=True),
    sa.Column('max_age_hours', sa.Float(), server_default=sa.text('48.0'), nullable=False),
    sa.Column('keep_builds', sa.Integer(), server_default=sa.text('7'), nullable=False),
    sa.Column('min_retain_hours', sa.Float(), server_default=sa.text('8.0'), nullable=False),
    sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_replay_dataset_profiles_name'), 'replay_dataset_profiles', ['name'], unique=True)
    op.create_table('test_modules',
    sa.Column('name', sa.String(length=100), nullable=False),
    sa.Column('display_name', sa.String(length=255), nullable=False),
    sa.Column('description', sa.Text(), nullable=False),
    sa.Column('params_schema_json', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('default_params_json', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('metrics_schema_json', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('version', sa.String(length=50), nullable=False),
    sa.PrimaryKeyConstraint('name')
    )
    op.create_table('users',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('email', sa.String(length=255), nullable=False),
    sa.Column('username', sa.String(length=100), nullable=False),
    sa.Column('password_hash', sa.String(length=255), nullable=False),
    sa.Column('role', sa.Enum('super_admin', 'admin', 'service', 'user', name='userrole'), nullable=False),
    sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
    sa.Column('password_changed_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_users_email'), 'users', ['email'], unique=True)
    op.create_index(op.f('ix_users_username'), 'users', ['username'], unique=True)
    op.create_table('api_keys',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=100), nullable=False),
    sa.Column('key_prefix', sa.String(length=16), nullable=False),
    sa.Column('key_hash', sa.String(length=64), nullable=False),
    sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
    sa.Column('last_used_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_api_keys_key_hash'), 'api_keys', ['key_hash'], unique=True)
    op.create_index(op.f('ix_api_keys_user_id'), 'api_keys', ['user_id'], unique=False)
    op.create_table('benchmarks',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('slug', sa.String(length=100), nullable=False),
    sa.Column('name', sa.String(length=255), nullable=False),
    sa.Column('description', sa.Text(), nullable=False),
    sa.Column('version', sa.String(length=50), nullable=False),
    sa.Column('status', sa.Enum('draft', 'active', 'archived', name='benchmarkstatus'), nullable=False),
    sa.Column('config_hash', sa.String(length=16), nullable=True),
    sa.Column('is_locked', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('group_tags', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
    sa.Column('created_by_user_id', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['created_by_user_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_benchmarks_slug'), 'benchmarks', ['slug'], unique=True)
    op.create_table('password_reset_requests',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('approved_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('approved_by_user_id', sa.Integer(), nullable=True),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('used_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('canceled_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('token_hash', sa.String(length=64), nullable=True),
    sa.Column('token_prefix', sa.String(length=16), nullable=True),
    sa.ForeignKeyConstraint(['approved_by_user_id'], ['users.id'], ),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_password_reset_requests_token_hash'), 'password_reset_requests', ['token_hash'], unique=True)
    op.create_index(op.f('ix_password_reset_requests_user_id'), 'password_reset_requests', ['user_id'], unique=False)
    op.create_table('replay_dataset_builds',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('profile_id', sa.Integer(), nullable=False),
    sa.Column('build_id', sa.String(length=64), nullable=True),
    sa.Column('status', sa.Enum('pending', 'running', 'ready', 'failed', name='datasetbuildstatus'), nullable=False),
    sa.Column('trigger', sa.String(length=20), nullable=False),
    sa.Column('triggered_by_user_id', sa.Integer(), nullable=True),
    sa.Column('claimed_by', sa.String(length=120), nullable=True),
    sa.Column('heartbeat_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('window_start', sa.DateTime(timezone=True), nullable=True),
    sa.Column('window_end', sa.DateTime(timezone=True), nullable=True),
    sa.Column('seed', sa.BigInteger(), nullable=True),
    sa.Column('records', sa.Integer(), nullable=True),
    sa.Column('size_bytes', sa.BigInteger(), nullable=True),
    sa.Column('sha256', sa.String(length=64), nullable=True),
    sa.Column('path', sa.String(length=500), nullable=True),
    sa.Column('stats_json', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('progress', sa.Text(), nullable=True),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['profile_id'], ['replay_dataset_profiles.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['triggered_by_user_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_replay_builds_profile_finished', 'replay_dataset_builds', ['profile_id', 'finished_at'], unique=False)
    op.create_index(op.f('ix_replay_dataset_builds_build_id'), 'replay_dataset_builds', ['build_id'], unique=False)
    op.create_index(op.f('ix_replay_dataset_builds_profile_id'), 'replay_dataset_builds', ['profile_id'], unique=False)
    op.create_index(op.f('ix_replay_dataset_builds_status'), 'replay_dataset_builds', ['status'], unique=False)
    op.create_table('benchmark_modules',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('benchmark_id', sa.Integer(), nullable=False),
    sa.Column('module_name', sa.String(length=100), nullable=False),
    sa.Column('params_json', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('metric_configs_json', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('weight', sa.Numeric(precision=5, scale=4), nullable=False),
    sa.Column('order_index', sa.Integer(), nullable=False),
    sa.Column('skip_if_prev_failed', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.ForeignKeyConstraint(['benchmark_id'], ['benchmarks.id'], ),
    sa.ForeignKeyConstraint(['module_name'], ['test_modules.name'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_benchmark_module_benchmark_order', 'benchmark_modules', ['benchmark_id', 'order_index'], unique=False)
    op.create_index(op.f('ix_benchmark_modules_benchmark_id'), 'benchmark_modules', ['benchmark_id'], unique=False)
    op.create_index(op.f('ix_benchmark_modules_module_name'), 'benchmark_modules', ['module_name'], unique=False)
    op.create_table('submissions',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('benchmark_id', sa.Integer(), nullable=True),
    sa.Column('module_name', sa.String(length=100), nullable=True),
    sa.Column('endpoint_url', sa.String(length=500), nullable=False),
    sa.Column('endpoint_model', sa.String(length=255), nullable=False),
    sa.Column('endpoint_api_key_enc', sa.Text(), nullable=False),
    sa.Column('contributor', sa.String(length=255), nullable=True),
    sa.Column('status', sa.Enum('queued', 'running', 'done', 'failed', 'canceled', name='submissionstatus'), nullable=False),
    sa.Column('score_total', sa.Float(), nullable=True),
    sa.Column('passed', sa.Boolean(), nullable=True),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('run_attempts', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('benchmark_config_hash', sa.String(length=16), nullable=True),
    sa.Column('extra_params', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('cards_per_machine', sa.Integer(), nullable=True),
    sa.Column('machine_count', sa.Integer(), nullable=True),
    sa.Column('card_type', sa.String(length=50), nullable=True),
    sa.Column('description_summary', sa.String(length=100), nullable=True),
    sa.Column('description_detail', sa.Text(), nullable=True),
    sa.Column('source_url', sa.String(length=500), nullable=True),
    sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
    sa.Column('started_at', sa.DateTime(), nullable=True),
    sa.Column('finished_at', sa.DateTime(), nullable=True),
    sa.Column('worker_log_gz', sa.LargeBinary(), nullable=True),
    sa.Column('worker_log_truncated', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.CheckConstraint('(benchmark_id IS NOT NULL AND module_name IS NULL) OR (benchmark_id IS NULL AND module_name IS NOT NULL)', name='ck_submission_one_of'),
    sa.ForeignKeyConstraint(['benchmark_id'], ['benchmarks.id'], ),
    sa.ForeignKeyConstraint(['module_name'], ['test_modules.name'], ),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_submissions_benchmark_id'), 'submissions', ['benchmark_id'], unique=False)
    op.create_index('ix_submissions_created_at', 'submissions', ['created_at'], unique=False)
    op.create_index('ix_submissions_leaderboard', 'submissions', ['benchmark_id', 'status', 'score_total'], unique=False)
    op.create_index(op.f('ix_submissions_user_id'), 'submissions', ['user_id'], unique=False)
    op.create_table('submission_runs',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('submission_id', sa.Integer(), nullable=False),
    sa.Column('benchmark_module_id', sa.Integer(), nullable=True),
    sa.Column('module_name', sa.String(length=100), nullable=False),
    sa.Column('params_json', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('status', sa.Enum('pending', 'running', 'done', 'failed', 'skipped', name='modulerunstatus'), nullable=False),
    sa.Column('score', sa.Float(), nullable=True),
    sa.Column('passed', sa.Boolean(), nullable=True),
    sa.Column('metrics_json', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('metric_configs_json', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('started_at', sa.DateTime(), nullable=True),
    sa.Column('finished_at', sa.DateTime(), nullable=True),
    sa.Column('artifact_path', sa.String(length=500), nullable=True),
    sa.Column('applied_concurrency', sa.Integer(), nullable=True),
    sa.ForeignKeyConstraint(['benchmark_module_id'], ['benchmark_modules.id'], ),
    sa.ForeignKeyConstraint(['submission_id'], ['submissions.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_submission_runs_benchmark_module_id'), 'submission_runs', ['benchmark_module_id'], unique=False)
    op.create_index(op.f('ix_submission_runs_submission_id'), 'submission_runs', ['submission_id'], unique=False)


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


def _repo_root() -> Path:
    p = Path(__file__).resolve().parent
    for _ in range(10):
        if (p / "benchmarks").is_dir() and (p / "bench").is_dir():
            return p
        p = p.parent
    raise FileNotFoundError("Could not find repo root (no benchmarks/ and bench/ dirs)")


def _module_descriptors() -> list[dict]:
    """Load module descriptors from bench.modules.MODULE_REGISTRY.

    The migration runs inside the backend image where /app is on sys.path;
    falls back to inserting the repo root for local `alembic upgrade` runs.
    """
    root = _repo_root()
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))

    from bench.modules import MODULE_REGISTRY  # type: ignore[import-not-found]

    out: list[dict] = []
    for name, cls in MODULE_REGISTRY.items():
        d = cls.descriptor()
        metric_descs = [
            asdict(m) if is_dataclass(m) else m for m in d.get("metrics_descriptors", [])
        ]
        default_metric_configs = [
            asdict(m) if is_dataclass(m) else m for m in d.get("default_metric_configs", [])
        ]
        out.append({
            "name": name,
            "display_name": d["display_name"],
            "description": d["description"],
            "params_schema": d["params_schema"],
            "default_params": d["default_params"],
            "metrics_schema": {
                "metrics_descriptors": metric_descs,
                "default_metric_configs": default_metric_configs,
            },
        })
    return out


# ---------------------------------------------------------------------------
# Seed data
# ---------------------------------------------------------------------------


# GPU / accelerator names offered in the card-type dropdown out of the box. The
# list is a lookup table, not a constraint: a submission stores the string it
# was given, and an admin edits this set through the Card Types page — so
# nothing here limits what hardware can be benchmarked.
_DEFAULT_CARD_TYPES = [
    "A100", "H100", "H200", "H20", "B200", "B300",
    "Ascend_910B", "Ascend_910C", "DCU_K100", "MLU370",
    "BI100", "BI150", "P800", "BR166", "S4000", "C500", "VA16",
]


def _seed() -> None:
    conn = op.get_bind()

    # 0. Card types.
    for order, name in enumerate(_DEFAULT_CARD_TYPES):
        conn.execute(
            sa.text(
                "INSERT INTO card_types (name, display_order) "
                "VALUES (:name, :display_order) ON CONFLICT (name) DO NOTHING"
            ),
            {"name": name, "display_order": order},
        )

    # 2. test_modules from the bench module registry — single source of truth.
    for mod in _module_descriptors():
        conn.execute(
            sa.text(
                "INSERT INTO test_modules "
                "(name, display_name, description, params_schema_json, "
                " default_params_json, metrics_schema_json, version) "
                "VALUES (:name, :display_name, :description, "
                "        :params_schema_json, :default_params_json, "
                "        :metrics_schema_json, '1.0.0')"
            ),
            {
                "name": mod["name"],
                "display_name": mod["display_name"],
                "description": mod["description"],
                "params_schema_json": json.dumps(mod["params_schema"]),
                "default_params_json": json.dumps(mod["default_params"]),
                "metrics_schema_json": json.dumps(mod["metrics_schema"]),
            },
        )


# ---------------------------------------------------------------------------
# Entrypoints
# ---------------------------------------------------------------------------


def upgrade() -> None:
    _create_schema()
    _seed()


def downgrade() -> None:
    op.drop_index(op.f('ix_submission_runs_submission_id'), table_name='submission_runs')
    op.drop_index(op.f('ix_submission_runs_benchmark_module_id'), table_name='submission_runs')
    op.drop_table('submission_runs')
    op.drop_index(op.f('ix_submissions_user_id'), table_name='submissions')
    op.drop_index('ix_submissions_leaderboard', table_name='submissions')
    op.drop_index('ix_submissions_created_at', table_name='submissions')
    op.drop_index(op.f('ix_submissions_benchmark_id'), table_name='submissions')
    op.drop_table('submissions')
    op.drop_index(op.f('ix_benchmark_modules_module_name'), table_name='benchmark_modules')
    op.drop_index(op.f('ix_benchmark_modules_benchmark_id'), table_name='benchmark_modules')
    op.drop_index('ix_benchmark_module_benchmark_order', table_name='benchmark_modules')
    op.drop_table('benchmark_modules')
    op.drop_index(op.f('ix_replay_dataset_builds_status'), table_name='replay_dataset_builds')
    op.drop_index(op.f('ix_replay_dataset_builds_profile_id'), table_name='replay_dataset_builds')
    op.drop_index(op.f('ix_replay_dataset_builds_build_id'), table_name='replay_dataset_builds')
    op.drop_index('ix_replay_builds_profile_finished', table_name='replay_dataset_builds')
    op.drop_table('replay_dataset_builds')
    op.drop_index(op.f('ix_password_reset_requests_user_id'), table_name='password_reset_requests')
    op.drop_index(op.f('ix_password_reset_requests_token_hash'), table_name='password_reset_requests')
    op.drop_table('password_reset_requests')
    op.drop_index(op.f('ix_benchmarks_slug'), table_name='benchmarks')
    op.drop_table('benchmarks')
    op.drop_index(op.f('ix_api_keys_user_id'), table_name='api_keys')
    op.drop_index(op.f('ix_api_keys_key_hash'), table_name='api_keys')
    op.drop_table('api_keys')
    op.drop_index(op.f('ix_users_username'), table_name='users')
    op.drop_index(op.f('ix_users_email'), table_name='users')
    op.drop_table('users')
    op.drop_table('test_modules')
    op.drop_index(op.f('ix_replay_dataset_profiles_name'), table_name='replay_dataset_profiles')
    op.drop_table('replay_dataset_profiles')
    op.drop_index(op.f('ix_card_types_name'), table_name='card_types')
    op.drop_table('card_types')
    sa.Enum(name="benchmarkstatus").drop(op.get_bind(), checkfirst=False)
    sa.Enum(name="datasetbuildstatus").drop(op.get_bind(), checkfirst=False)
    sa.Enum(name="modulerunstatus").drop(op.get_bind(), checkfirst=False)
    sa.Enum(name="submissionstatus").drop(op.get_bind(), checkfirst=False)
    sa.Enum(name="userrole").drop(op.get_bind(), checkfirst=False)
