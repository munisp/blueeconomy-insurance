"""Phase 11 performance audit: status-list snapshot lookup index.

statuslists._current_list queries:
    WHERE purpose = :p ORDER BY created_at DESC LIMIT 1
for every credential issuance and verification. status_list_snapshots had no
index on (purpose, created_at), so the lookup degenerates to a full scan +
sort as snapshots accumulate.

Revision ID: 0002_perf_indexes
Revises: 0001_initial
Create Date: 2026-09-01
"""

from __future__ import annotations

from alembic import op

revision = "0002_perf_indexes"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_status_list_snapshots_purpose_time "
        "ON status_list_snapshots (purpose, created_at DESC)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_status_list_snapshots_purpose_time")
