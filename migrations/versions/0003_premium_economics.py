"""Premium economics + endorsement maker-checker + suspension status.

Phase-11 remediation of the premium subledger gaps:

- quotes.premium_journal_reference: the Dr premium:receivable / Cr
  premium:income journal posted at bind;
- policies.premium_paid_at: stamped when an exact-match premium receipt is
  applied (Dr insurer:clearing / Cr premium:receivable);
- premium_receipts: external receipts with the same exact-match-or-quarantine
  doctrine as payout_receipts; unique external_reference kills replays;
- endorsements: PROPOSED/APPROVED/REJECTED maker-checker for premium-delta
  endorsements, DB-enforced dual control (approver <> creator), approval
  posts the balanced delta journal (journal_reference);
- policies: SUSPENDED status whose transitions set/clear the suspension
  status-list bit so offline verifiers fail closed.

Existing endorsements predate the maker-checker flow and were effective when
recorded, so they are migrated as APPROVED (with an empty journal_reference —
honest: no journal leg was ever posted for them); new rows default to
PROPOSED.

Revision ID: 0003_premium_economics
Revises: 0002_perf_indexes
Create Date: 2026-09-01
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003_premium_economics"
down_revision = "0002_perf_indexes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "quotes",
        sa.Column("premium_journal_reference", sa.String(128), nullable=False, server_default=""),
    )
    op.add_column(
        "policies",
        sa.Column("premium_paid_at", sa.DateTime(timezone=True), nullable=True),
    )
    # SUSPENDED joins the policy state machine.
    op.drop_constraint("ck_policy_status", "policies", type_="check")
    op.create_check_constraint(
        "ck_policy_status", "policies",
        "status IN ('ACTIVE','SUSPENDED','LAPSED','CANCELLED')",
    )
    # Endorsement maker-checker. Endorsements were append-only, but a
    # maker-checker state machine must transition PROPOSED -> APPROVED /
    # REJECTED on the row — the same pattern as claims (which are mutable
    # with DB-enforced dual-control CHECKs). The append-only trigger is
    # dropped for endorsements; every transition writes a hash-chained audit
    # event and premium deltas are sealed by the immutable journal.
    op.execute("DROP TRIGGER IF EXISTS trg_endorsements_append_only ON endorsements")
    op.add_column(
        "endorsements",
        sa.Column("status", sa.String(16), nullable=False, server_default="APPROVED"),
    )
    op.add_column(
        "endorsements",
        sa.Column("approved_by", sa.String(256), nullable=False, server_default=""),
    )
    op.add_column(
        "endorsements",
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.alter_column("endorsements", "status", server_default="PROPOSED")
    op.create_check_constraint(
        "ck_endorsement_status", "endorsements",
        "status IN ('PROPOSED','APPROVED','REJECTED')",
    )
    op.create_check_constraint(
        "ck_endorsement_dual_control", "endorsements",
        "approved_by = '' OR approved_by <> created_by",
    )
    op.create_table(
        "premium_receipts",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("policy_id", sa.Uuid(), sa.ForeignKey("policies.id"), nullable=True),
        sa.Column("external_reference", sa.String(128), nullable=False, unique=True),
        sa.Column("amount_kobo", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("quarantine_reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("journal_reference", sa.String(128), nullable=False, server_default=""),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("status IN ('APPLIED','QUARANTINED')", name="ck_premium_receipt_status"),
        sa.CheckConstraint("amount_kobo >= 0", name="ck_premium_receipt_amount"),
    )
    # Premium receipts are payment evidence: immutable, like payout_receipts.
    op.execute(
        """
        CREATE TRIGGER trg_premium_receipts_append_only
        BEFORE UPDATE OR DELETE ON premium_receipts
        FOR EACH ROW EXECUTE FUNCTION reject_mutation();
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_premium_receipts_append_only ON premium_receipts")
    op.drop_table("premium_receipts")
    op.drop_constraint("ck_endorsement_dual_control", "endorsements", type_="check")
    op.drop_constraint("ck_endorsement_status", "endorsements", type_="check")
    op.drop_column("endorsements", "approved_at")
    op.drop_column("endorsements", "approved_by")
    op.drop_column("endorsements", "status")
    op.execute(
        """
        CREATE TRIGGER trg_endorsements_append_only
        BEFORE UPDATE OR DELETE ON endorsements
        FOR EACH ROW EXECUTE FUNCTION reject_mutation();
        """
    )
    op.drop_constraint("ck_policy_status", "policies", type_="check")
    op.create_check_constraint(
        "ck_policy_status", "policies",
        "status IN ('ACTIVE','LAPSED','CANCELLED')",
    )
    op.drop_column("policies", "premium_paid_at")
    op.drop_column("quotes", "premium_journal_reference")
