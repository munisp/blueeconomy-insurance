"""Initial schema: marine insurance engine plus DB-enforced invariants.

Invariants enforced by the database, not by application convention:
- append-only audit_events (UPDATE/DELETE rejected by trigger);
- immutable bind decisions, endorsements, ISR evidence, claim documents,
  payout receipts, gateway calls, journals, ledger entries, idempotency
  records and processed events;
- double-entry balance: a DEFERRABLE INITIALLY DEFERRED constraint trigger
  rejects COMMIT of any journal whose legs do not balance;
- maker-checker dual control on binds, adjuster assignment and settlement:
  CHECK constraints make checker == maker unrepresentable.

Revision ID: 0001_initial
Revises:
Create Date: 2026-09-01
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None

QUOTE_STATUSES = ("QUOTED", "BIND_PENDING", "BOUND", "ISSUED", "DECLINED", "EXPIRED")
POLICY_STATUSES = ("ACTIVE", "LAPSED", "CANCELLED")
CLAIM_STATUSES = (
    "FNOL", "UNDER_REVIEW", "ADJUSTER_PENDING", "ADJUSTER_ASSIGNED",
    "SETTLEMENT_PENDING", "SETTLED", "PAID", "REJECTED",
)
PRODUCT_KINDS = (
    "marine-cargo-single", "marine-cargo-open", "ferry-parametric",
    "protection-indemnity", "hull",
)
ENDORSEMENT_KINDS = ("EXTENSION", "VALUE_CHANGE", "ASSURED_CHANGE", "CANCELLATION", "REINSTATEMENT")


def _uuid() -> sa.types.TypeEngine:
    return sa.Uuid()


def upgrade() -> None:
    op.create_table(
        "principals",
        sa.Column("id", _uuid(), primary_key=True),
        sa.Column("subject", sa.String(256), nullable=False, unique=True),
        sa.Column("display_name", sa.String(256), nullable=False, server_default=""),
        sa.Column("tenant", sa.String(128), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "products",
        sa.Column("id", _uuid(), primary_key=True),
        sa.Column("code", sa.String(64), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("name", sa.String(256), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="DRAFT"),
        sa.Column("definition", JSONB, nullable=False),
        sa.Column("created_by", sa.String(256), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("code", "version", name="uq_product_code_version"),
        sa.CheckConstraint(f"kind IN {PRODUCT_KINDS!r}", name="ck_product_kind"),
        sa.CheckConstraint("status IN ('DRAFT','ACTIVE','RETIRED')", name="ck_product_status"),
        sa.CheckConstraint("version >= 1", name="ck_product_version"),
    )
    op.create_table(
        "rate_tables",
        sa.Column("id", _uuid(), primary_key=True),
        sa.Column("product_id", _uuid(), sa.ForeignKey("products.id"), nullable=False),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("effective_to", sa.Date()),
        sa.Column("rates", JSONB, nullable=False),
        sa.Column("created_by", sa.String(256), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("effective_to IS NULL OR effective_to > effective_from", name="ck_rate_window"),
    )
    op.create_index("ix_rate_tables_product_id", "rate_tables", ["product_id"])
    op.create_table(
        "isr_evidence",
        sa.Column("id", _uuid(), primary_key=True),
        sa.Column("evidence_id", sa.String(128), nullable=False, unique=True),
        sa.Column("corridor", sa.String(128), nullable=False),
        sa.Column("delta_bp", sa.BigInteger(), nullable=False),
        sa.Column("source_digest", sa.String(64), nullable=False),
        sa.Column("source_event_id", sa.String(128), nullable=False, unique=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("envelope", JSONB, nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("delta_bp >= 0", name="ck_isr_delta_nonneg"),
    )
    op.create_index("ix_isr_evidence_corridor", "isr_evidence", ["corridor"])
    op.create_table(
        "quotes",
        sa.Column("id", _uuid(), primary_key=True),
        sa.Column("quote_ref", sa.String(40), nullable=False, unique=True),
        sa.Column("product_id", _uuid(), sa.ForeignKey("products.id"), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="QUOTED"),
        sa.Column("corridor", sa.String(128), nullable=False, server_default=""),
        sa.Column("declaration_ref", sa.String(64), nullable=False, server_default=""),
        sa.Column("source_event_id", sa.String(128), nullable=False, server_default=""),
        sa.Column("assured_name", sa.String(256), nullable=False, server_default=""),
        sa.Column("assured_tin", sa.String(32), nullable=False, server_default=""),
        sa.Column("currency", sa.String(3), nullable=False, server_default="NGN"),
        sa.Column("premium_kobo", sa.BigInteger(), nullable=False),
        sa.Column("rating_trace", JSONB, nullable=False),
        sa.Column("created_by", sa.String(256), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("bind_requested_by", sa.String(256), nullable=False, server_default=""),
        sa.Column("bind_requested_at", sa.DateTime(timezone=True)),
        sa.Column("bound_by", sa.String(256), nullable=False, server_default=""),
        sa.Column("bound_at", sa.DateTime(timezone=True)),
        sa.Column("idempotency_key", sa.String(128), nullable=False, server_default=""),
        sa.CheckConstraint(f"status IN {QUOTE_STATUSES!r}", name="ck_quote_status"),
        sa.CheckConstraint("premium_kobo >= 0", name="ck_quote_premium"),
        sa.CheckConstraint("expires_at > created_at", name="ck_quote_expiry"),
        sa.CheckConstraint("bound_by = '' OR bound_by <> bind_requested_by", name="ck_quote_dual_control"),
    )
    op.create_index("ix_quotes_product_id", "quotes", ["product_id"])
    op.create_index(
        "uq_quote_idem", "quotes", ["idempotency_key", "created_by"], unique=True,
        postgresql_where=sa.text("idempotency_key <> ''"),
    )
    op.create_index("ix_quotes_declaration_ref", "quotes", ["declaration_ref"])
    op.create_table(
        "quote_lines",
        sa.Column("id", _uuid(), primary_key=True),
        sa.Column("quote_id", _uuid(), sa.ForeignKey("quotes.id"), nullable=False),
        sa.Column("line_index", sa.Integer(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("hs_code", sa.String(16), nullable=False, server_default=""),
        sa.Column("risk_class", sa.String(64), nullable=False),
        sa.Column("insured_value_kobo", sa.BigInteger(), nullable=False),
        sa.UniqueConstraint("quote_id", "line_index", name="uq_quote_line"),
        sa.CheckConstraint("insured_value_kobo > 0", name="ck_qline_value"),
    )
    op.create_index("ix_quote_lines_quote_id", "quote_lines", ["quote_id"])
    op.create_table(
        "bind_decisions",
        sa.Column("id", _uuid(), primary_key=True),
        sa.Column("quote_id", _uuid(), sa.ForeignKey("quotes.id"), nullable=False, unique=True),
        sa.Column("decision", sa.String(8), nullable=False),
        sa.Column("decided_by", sa.String(256), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("decision IN ('BIND','DECLINE')", name="ck_bind_decision"),
    )
    op.create_table(
        "policy_serial_counters",
        sa.Column("family_code", sa.String(3), primary_key=True),
        sa.Column("year", sa.Integer(), primary_key=True),
        sa.Column("next_sequence", sa.BigInteger(), nullable=False, server_default="0"),
    )
    op.create_table(
        "policies",
        sa.Column("id", _uuid(), primary_key=True),
        sa.Column("policy_number", sa.String(40), nullable=False, unique=True),
        sa.Column("quote_id", _uuid(), sa.ForeignKey("quotes.id"), nullable=False, unique=True),
        sa.Column("product_id", _uuid(), sa.ForeignKey("products.id"), nullable=False),
        sa.Column("family_code", sa.String(3), nullable=False),
        sa.Column("year", sa.Integer(), nullable=False),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="ACTIVE"),
        sa.Column("assured_name", sa.String(256), nullable=False, server_default=""),
        sa.Column("assured_tin", sa.String(32), nullable=False, server_default=""),
        sa.Column("corridor", sa.String(128), nullable=False, server_default=""),
        sa.Column("declaration_ref", sa.String(64), nullable=False, server_default=""),
        sa.Column("currency", sa.String(3), nullable=False, server_default="NGN"),
        sa.Column("premium_kobo", sa.BigInteger(), nullable=False),
        sa.Column("insured_value_kobo", sa.BigInteger(), nullable=False),
        sa.Column("status_list_index", sa.Integer(), nullable=False, unique=True),
        sa.Column("credential", JSONB, nullable=False),
        sa.Column("inception_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expiry_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("issued_by", sa.String(256), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("cancelled_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(f"status IN {POLICY_STATUSES!r}", name="ck_policy_status"),
        sa.CheckConstraint("premium_kobo >= 0", name="ck_policy_premium"),
        sa.CheckConstraint("insured_value_kobo > 0", name="ck_policy_value"),
        sa.CheckConstraint("expiry_at > inception_at", name="ck_policy_window"),
    )
    op.create_index("ix_policies_product_id", "policies", ["product_id"])
    op.create_table(
        "endorsements",
        sa.Column("id", _uuid(), primary_key=True),
        sa.Column("policy_id", _uuid(), sa.ForeignKey("policies.id"), nullable=False),
        sa.Column("endorsement_no", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(24), nullable=False),
        sa.Column("premium_delta_kobo", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("detail", JSONB, nullable=False),
        sa.Column("journal_reference", sa.String(128), nullable=False, server_default=""),
        sa.Column("created_by", sa.String(256), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("policy_id", "endorsement_no", name="uq_endorsement_no"),
        sa.CheckConstraint(f"kind IN {ENDORSEMENT_KINDS!r}", name="ck_endorsement_kind"),
    )
    op.create_index("ix_endorsements_policy_id", "endorsements", ["policy_id"])
    op.create_table(
        "claims",
        sa.Column("id", _uuid(), primary_key=True),
        sa.Column("claim_ref", sa.String(40), nullable=False, unique=True),
        sa.Column("policy_id", _uuid(), sa.ForeignKey("policies.id"), nullable=False),
        sa.Column("status", sa.String(24), nullable=False, server_default="FNOL"),
        sa.Column("loss_occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("loss_description", sa.Text(), nullable=False, server_default=""),
        sa.Column("trigger_event_id", sa.String(128), nullable=False, server_default=""),
        sa.Column("claimed_kobo", sa.BigInteger(), nullable=False),
        sa.Column("adjuster_sub", sa.String(256), nullable=False, server_default=""),
        sa.Column("assignment_proposed_by", sa.String(256), nullable=False, server_default=""),
        sa.Column("assignment_confirmed_by", sa.String(256), nullable=False, server_default=""),
        sa.Column("settlement_proposed_by", sa.String(256), nullable=False, server_default=""),
        sa.Column("settlement_approved_by", sa.String(256), nullable=False, server_default=""),
        sa.Column("settled_kobo", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("journal_reference", sa.String(128), nullable=False, server_default=""),
        sa.Column("reported_by", sa.String(256), nullable=False),
        sa.Column("reported_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("settled_at", sa.DateTime(timezone=True)),
        sa.Column("idempotency_key", sa.String(128), nullable=False, server_default=""),
        sa.CheckConstraint(f"status IN {CLAIM_STATUSES!r}", name="ck_claim_status"),
        sa.CheckConstraint("claimed_kobo > 0", name="ck_claim_amount"),
        sa.CheckConstraint("settled_kobo >= 0", name="ck_claim_settled"),
        sa.CheckConstraint(
            "assignment_confirmed_by = '' OR assignment_confirmed_by <> assignment_proposed_by",
            name="ck_claim_assign_dual",
        ),
        sa.CheckConstraint(
            "settlement_approved_by = '' OR settlement_approved_by <> settlement_proposed_by",
            name="ck_claim_settle_dual",
        ),
    )
    op.create_index("ix_claims_policy_id", "claims", ["policy_id"])
    op.create_index(
        "uq_claim_idem", "claims", ["idempotency_key", "reported_by"], unique=True,
        postgresql_where=sa.text("idempotency_key <> ''"),
    )
    op.create_table(
        "claim_documents",
        sa.Column("id", _uuid(), primary_key=True),
        sa.Column("claim_id", _uuid(), sa.ForeignKey("claims.id"), nullable=False),
        sa.Column("vault_ref", sa.String(256), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("description", sa.String(256), nullable=False, server_default=""),
        sa.Column("uploaded_by", sa.String(256), nullable=False),
        sa.Column("uploaded_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("claim_id", "vault_ref", name="uq_claim_doc"),
    )
    op.create_index("ix_claim_documents_claim_id", "claim_documents", ["claim_id"])
    op.create_table(
        "journals",
        sa.Column("id", _uuid(), primary_key=True),
        sa.Column("reference", sa.String(128), nullable=False, unique=True),
        sa.Column("narration", sa.Text(), nullable=False, server_default=""),
        sa.Column("posted_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "ledger_entries",
        sa.Column("id", _uuid(), primary_key=True),
        sa.Column("journal_id", _uuid(), sa.ForeignKey("journals.id"), nullable=False),
        sa.Column("account", sa.String(64), nullable=False),
        sa.Column("debit_kobo", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("credit_kobo", sa.BigInteger(), nullable=False, server_default="0"),
        sa.CheckConstraint("debit_kobo >= 0 AND credit_kobo >= 0", name="ck_entry_nonneg"),
        sa.CheckConstraint("NOT (debit_kobo > 0 AND credit_kobo > 0)", name="ck_entry_one_side"),
    )
    op.create_index("ix_ledger_entries_journal_id", "ledger_entries", ["journal_id"])
    op.create_table(
        "payout_receipts",
        sa.Column("id", _uuid(), primary_key=True),
        sa.Column("claim_id", _uuid(), sa.ForeignKey("claims.id")),
        sa.Column("external_reference", sa.String(128), nullable=False, unique=True),
        sa.Column("amount_kobo", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("quarantine_reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("status IN ('APPLIED','QUARANTINED')", name="ck_receipt_status"),
        sa.CheckConstraint("amount_kobo >= 0", name="ck_receipt_amount"),
    )
    op.create_table(
        "gateway_calls",
        sa.Column("id", _uuid(), primary_key=True),
        sa.Column("partner_id", sa.String(64), nullable=False),
        sa.Column("operation", sa.String(64), nullable=False),
        sa.Column("request_digest", sa.String(64), nullable=False),
        sa.Column("response_status", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("outcome", sa.String(24), nullable=False),
        sa.Column("created_by", sa.String(256), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "outcome IN ('OK','ADAPTER_UNCONFIGURED','UPSTREAM_ERROR')", name="ck_gw_outcome"
        ),
    )
    op.create_index("ix_gateway_calls_partner_id", "gateway_calls", ["partner_id"])
    op.create_table(
        "idempotency_records",
        sa.Column("id", _uuid(), primary_key=True),
        sa.Column("key", sa.String(128), nullable=False),
        sa.Column("principal_sub", sa.String(256), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("response_status", sa.Integer(), nullable=False),
        sa.Column("response_body", JSONB, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("key", "principal_sub", name="uq_idem_key_principal"),
    )
    op.create_table(
        "outbox_messages",
        sa.Column("id", _uuid(), primary_key=True),
        sa.Column("event_id", sa.String(128), nullable=False, unique=True),
        sa.Column("topic", sa.String(128), nullable=False),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("classification", sa.String(32), nullable=False),
        sa.Column("envelope", JSONB, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True)),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text(), nullable=False, server_default=""),
    )
    op.create_index(
        "ix_outbox_unpublished", "outbox_messages", ["created_at"],
        postgresql_where=sa.text("published_at IS NULL"),
    )
    op.create_table(
        "audit_events",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("payload", JSONB, nullable=False),
        sa.Column("prev_hash", sa.String(64), nullable=False),
        sa.Column("hash", sa.String(64), nullable=False, unique=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "status_list_snapshots",
        sa.Column("id", _uuid(), primary_key=True),
        sa.Column("purpose", sa.String(32), nullable=False),
        sa.Column("bitstring", sa.Text(), nullable=False),
        sa.Column("credential", JSONB, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "processed_events",
        sa.Column("event_id", sa.String(128), primary_key=True),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=False),
    )

    _install_invariant_triggers()


def _install_invariant_triggers() -> None:
    # 1. Generic mutation rejection for append-only / immutable tables.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION reject_mutation() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'append-only violation: % on table % is rejected', TG_OP, TG_TABLE_NAME
                USING ERRCODE = 'raise_exception';
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    for table in (
        "audit_events",        # hash-chained append-only audit
        "bind_decisions",      # maker-checker decisions are immutable
        "endorsements",        # endorsement history never changes
        "isr_evidence",        # consumed risk evidence is immutable
        "claim_documents",     # evidence vault references are immutable
        "payout_receipts",     # payout evidence is immutable
        "gateway_calls",       # gateway interaction audit is immutable
        "journals",            # posted journals never change
        "ledger_entries",      # ledger legs never change (reversal = new journal)
        "idempotency_records", # idempotency evidence is immutable
        "processed_events",    # consumer dedupe evidence is immutable
    ):
        op.execute(
            f"""
            CREATE TRIGGER trg_{table}_append_only
            BEFORE UPDATE OR DELETE ON {table}
            FOR EACH ROW EXECUTE FUNCTION reject_mutation();
            """
        )

    # 2. Double-entry balance: deferred constraint trigger evaluated at COMMIT.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION check_journal_balanced() RETURNS trigger AS $$
        DECLARE
            j uuid;
            d bigint;
            c bigint;
        BEGIN
            j := COALESCE(NEW.journal_id, OLD.journal_id);
            SELECT COALESCE(SUM(debit_kobo), 0), COALESCE(SUM(credit_kobo), 0)
              INTO d, c
              FROM ledger_entries
             WHERE journal_id = j;
            IF d <> c THEN
                RAISE EXCEPTION 'journal % is not balanced: debits % <> credits %', j, d, c
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE CONSTRAINT TRIGGER trg_journal_balanced
        AFTER INSERT OR UPDATE OR DELETE ON ledger_entries
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION check_journal_balanced();
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_journal_balanced ON ledger_entries")
    op.execute("DROP FUNCTION IF EXISTS check_journal_balanced()")
    for table in (
        "audit_events", "bind_decisions", "endorsements", "isr_evidence",
        "claim_documents", "payout_receipts", "gateway_calls", "journals",
        "ledger_entries", "idempotency_records", "processed_events",
    ):
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_append_only ON {table}")
    op.execute("DROP FUNCTION IF EXISTS reject_mutation()")
    for table in (
        "processed_events", "status_list_snapshots", "audit_events",
        "outbox_messages", "idempotency_records", "gateway_calls",
        "payout_receipts", "ledger_entries", "journals", "claim_documents",
        "claims", "endorsements", "policies", "policy_serial_counters",
        "bind_decisions", "quote_lines", "quotes", "isr_evidence",
        "rate_tables", "products", "principals",
    ):
        op.drop_table(table)
