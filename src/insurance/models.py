"""SQLAlchemy 2.0 models. Invariants that must hold under concurrency live in
the DATABASE (CHECK constraints, unique constraints, triggers in the Alembic
migration), not in application convention.

Money is integer minor units (kobo) everywhere; rates are basis points.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------- principals


class Principal(Base):
    """Platform identity resolved from a verified OIDC token."""

    __tablename__ = "principals"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    subject: Mapped[str] = mapped_column(String(256), unique=True)  # OIDC sub
    display_name: Mapped[str] = mapped_column(String(256), default="")
    tenant: Mapped[str] = mapped_column(String(128), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# ----------------------------------------------------- product catalogue


PRODUCT_KINDS = (
    "marine-cargo-single",      # single-transit cargo
    "marine-cargo-open",        # open cover
    "ferry-parametric",         # parametric ferry passenger cover
    "protection-indemnity",     # P&I evidence-pack
    "hull",                     # hull evidence-pack
)

PRODUCT_STATUSES = ("DRAFT", "ACTIVE", "RETIRED")


class Product(Base):
    """Versioned product definition. Versions are immutable once ACTIVE
    (service-enforced; (code, version) is the natural key)."""

    __tablename__ = "products"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    code: Mapped[str] = mapped_column(String(64))
    version: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(32))
    name: Mapped[str] = mapped_column(String(256))
    status: Mapped[str] = mapped_column(String(16), default="DRAFT")
    # Risk classes this product accepts and the declaration line shape it
    # expects (JSON Schema-ish descriptor, server-side only).
    definition: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_by: Mapped[str] = mapped_column(String(256))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (
        UniqueConstraint("code", "version", name="uq_product_code_version"),
        CheckConstraint(f"kind IN {PRODUCT_KINDS!r}", name="ck_product_kind"),
        CheckConstraint(f"status IN {PRODUCT_STATUSES!r}", name="ck_product_status"),
        CheckConstraint("version >= 1", name="ck_product_version"),
    )


class RateTable(Base):
    """Effective-dated rate table (basis points), server-side only. Exactly
    one table may cover a given date per product version (service-enforced
    via overlap check in a serializable transaction)."""

    __tablename__ = "rate_tables"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    product_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("products.id"), index=True)
    effective_from: Mapped[date] = mapped_column(Date)
    effective_to: Mapped[date | None] = mapped_column(Date)  # NULL = open-ended
    rates: Mapped[dict[str, Any]] = mapped_column(JSONB)  # see domain.rating
    created_by: Mapped[str] = mapped_column(String(256))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (
        CheckConstraint("effective_to IS NULL OR effective_to > effective_from", name="ck_rate_window"),
    )


# ------------------------------------------------------ ISR risk evidence


class IsrEvidence(Base):
    """Digest-verified premium-delta evidence consumed from the
    maritime-intelligence ISR outcome ledger. Each row pins the sha256 digest
    of the verified source payload; rows are immutable (trigger)."""

    __tablename__ = "isr_evidence"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    evidence_id: Mapped[str] = mapped_column(String(128), unique=True)  # source entry_id, dedupe
    corridor: Mapped[str] = mapped_column(String(128), index=True)      # route/corridor key
    delta_bp: Mapped[int] = mapped_column(BigInteger)                   # premium delta, basis points
    source_digest: Mapped[str] = mapped_column(String(64))              # sha256 hex of source payload
    source_event_id: Mapped[str] = mapped_column(String(128), unique=True)  # envelope eventId dedupe
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    envelope: Mapped[dict[str, Any]] = mapped_column(JSONB)             # full verified envelope v1.0
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (
        CheckConstraint("delta_bp >= 0", name="ck_isr_delta_nonneg"),
    )


# ------------------------------------------------------------------ quotes


QUOTE_STATUSES = (
    "QUOTED",           # rated, awaiting bind request
    "BIND_PENDING",     # maker requested bind, awaiting checker
    "BOUND",            # checker approved bind; cover in force pending issuance
    "ISSUED",           # policy issued (terminal for the quote)
    "DECLINED",         # checker declined bind
    "EXPIRED",          # quote validity lapsed before bind
)


class Quote(Base):
    __tablename__ = "quotes"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    quote_ref: Mapped[str] = mapped_column(String(40), unique=True)  # Q-<ulid-ish>
    product_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("products.id"), index=True)
    status: Mapped[str] = mapped_column(String(16), default="QUOTED")
    corridor: Mapped[str] = mapped_column(String(128), default="")
    # Declaration linkage (NTP VAS model): the platform declaration this
    # quote was generated from, when any.
    declaration_ref: Mapped[str] = mapped_column(String(64), default="", index=True)
    source_event_id: Mapped[str] = mapped_column(String(128), default="")  # envelope dedupe
    assured_name: Mapped[str] = mapped_column(String(256), default="")
    assured_tin: Mapped[str] = mapped_column(String(32), default="")
    currency: Mapped[str] = mapped_column(String(3), default="NGN")
    premium_kobo: Mapped[int] = mapped_column(BigInteger)
    rating_trace: Mapped[dict[str, Any]] = mapped_column(JSONB)  # full deterministic trace
    created_by: Mapped[str] = mapped_column(String(256))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    bind_requested_by: Mapped[str] = mapped_column(String(256), default="")
    bind_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    bound_by: Mapped[str] = mapped_column(String(256), default="")
    bound_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    idempotency_key: Mapped[str] = mapped_column(String(128), default="")
    __table_args__ = (
        CheckConstraint(f"status IN {QUOTE_STATUSES!r}", name="ck_quote_status"),
        CheckConstraint("premium_kobo >= 0", name="ck_quote_premium"),
        CheckConstraint("expires_at > created_at", name="ck_quote_expiry"),
        # maker-checker, DB-enforced: binder must differ from bind requester
        CheckConstraint("bound_by = '' OR bound_by <> bind_requested_by", name="ck_quote_dual_control"),
    )


class QuoteLine(Base):
    __tablename__ = "quote_lines"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    quote_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("quotes.id"), index=True)
    line_index: Mapped[int] = mapped_column(Integer)
    description: Mapped[str] = mapped_column(Text, default="")
    hs_code: Mapped[str] = mapped_column(String(16), default="")
    risk_class: Mapped[str] = mapped_column(String(64))
    insured_value_kobo: Mapped[int] = mapped_column(BigInteger)
    __table_args__ = (
        UniqueConstraint("quote_id", "line_index", name="uq_quote_line"),
        CheckConstraint("insured_value_kobo > 0", name="ck_qline_value"),
    )


class BindDecision(Base):
    """Maker-checker bind record. Immutable (trigger); one decision per
    quote; the checker can never equal the maker (service + DB CHECK on
    quotes)."""

    __tablename__ = "bind_decisions"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    quote_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("quotes.id"), unique=True)
    decision: Mapped[str] = mapped_column(String(8))  # BIND | DECLINE
    decided_by: Mapped[str] = mapped_column(String(256))
    reason: Mapped[str] = mapped_column(Text, default="")
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (
        CheckConstraint("decision IN ('BIND','DECLINE')", name="ck_bind_decision"),
    )


# ----------------------------------------------------------------- policies


POLICY_STATUSES = ("ACTIVE", "LAPSED", "CANCELLED")


class PolicySerialCounter(Base):
    """Atomic policy-number sequence claims (INSERT ... ON CONFLICT +
    UPDATE ... RETURNING in one transaction)."""

    __tablename__ = "policy_serial_counters"
    family_code: Mapped[str] = mapped_column(String(3), primary_key=True)
    year: Mapped[int] = mapped_column(Integer, primary_key=True)
    next_sequence: Mapped[int] = mapped_column(BigInteger, default=0)


class Policy(Base):
    __tablename__ = "policies"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    policy_number: Mapped[str] = mapped_column(String(40), unique=True)  # Luhn-gated
    quote_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("quotes.id"), unique=True)
    product_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("products.id"), index=True)
    family_code: Mapped[str] = mapped_column(String(3))
    year: Mapped[int] = mapped_column(Integer)
    sequence: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(16), default="ACTIVE")
    assured_name: Mapped[str] = mapped_column(String(256), default="")
    assured_tin: Mapped[str] = mapped_column(String(32), default="")
    corridor: Mapped[str] = mapped_column(String(128), default="")
    declaration_ref: Mapped[str] = mapped_column(String(64), default="")
    currency: Mapped[str] = mapped_column(String(3), default="NGN")
    premium_kobo: Mapped[int] = mapped_column(BigInteger)
    insured_value_kobo: Mapped[int] = mapped_column(BigInteger)
    status_list_index: Mapped[int] = mapped_column(Integer, unique=True)
    credential: Mapped[dict[str, Any]] = mapped_column(JSONB)  # signed W3C VC 2.0
    inception_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expiry_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    issued_by: Mapped[str] = mapped_column(String(256))
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        CheckConstraint(f"status IN {POLICY_STATUSES!r}", name="ck_policy_status"),
        CheckConstraint("premium_kobo >= 0", name="ck_policy_premium"),
        CheckConstraint("insured_value_kobo > 0", name="ck_policy_value"),
        CheckConstraint("expiry_at > inception_at", name="ck_policy_window"),
    )


ENDORSEMENT_KINDS = ("EXTENSION", "VALUE_CHANGE", "ASSURED_CHANGE", "CANCELLATION", "REINSTATEMENT")


class Endorsement(Base):
    """Policy endorsement; append-only (trigger). Monetary deltas are integer
    kobo and posted to the double-entry journal by the service."""

    __tablename__ = "endorsements"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    policy_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("policies.id"), index=True)
    endorsement_no: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(24))
    premium_delta_kobo: Mapped[int] = mapped_column(BigInteger, default=0)  # signed
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB)
    journal_reference: Mapped[str] = mapped_column(String(128), default="")
    created_by: Mapped[str] = mapped_column(String(256))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (
        UniqueConstraint("policy_id", "endorsement_no", name="uq_endorsement_no"),
        CheckConstraint(f"kind IN {ENDORSEMENT_KINDS!r}", name="ck_endorsement_kind"),
    )


# ------------------------------------------------------------------- claims


CLAIM_STATUSES = (
    "FNOL",                # first notice of loss recorded
    "UNDER_REVIEW",        # documents assembled, adjuster review open
    "ADJUSTER_PENDING",    # adjuster assignment proposed (maker)
    "ADJUSTER_ASSIGNED",   # assignment confirmed (checker)
    "SETTLEMENT_PENDING",  # settlement proposed (maker)
    "SETTLED",             # settlement approved + journal posted (checker)
    "PAID",                # payout receipt posted
    "REJECTED",
)


class Claim(Base):
    __tablename__ = "claims"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    claim_ref: Mapped[str] = mapped_column(String(40), unique=True)  # C-<...>
    policy_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("policies.id"), index=True)
    status: Mapped[str] = mapped_column(String(24), default="FNOL")
    loss_occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    loss_description: Mapped[str] = mapped_column(Text, default="")
    # Parametric products: the signed disruption event that triggered cover.
    trigger_event_id: Mapped[str] = mapped_column(String(128), default="")
    claimed_kobo: Mapped[int] = mapped_column(BigInteger)
    adjuster_sub: Mapped[str] = mapped_column(String(256), default="")
    assignment_proposed_by: Mapped[str] = mapped_column(String(256), default="")
    assignment_confirmed_by: Mapped[str] = mapped_column(String(256), default="")
    settlement_proposed_by: Mapped[str] = mapped_column(String(256), default="")
    settlement_approved_by: Mapped[str] = mapped_column(String(256), default="")
    settled_kobo: Mapped[int] = mapped_column(BigInteger, default=0)
    journal_reference: Mapped[str] = mapped_column(String(128), default="")
    reported_by: Mapped[str] = mapped_column(String(256))
    reported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    idempotency_key: Mapped[str] = mapped_column(String(128), default="")
    __table_args__ = (
        CheckConstraint(f"status IN {CLAIM_STATUSES!r}", name="ck_claim_status"),
        CheckConstraint("claimed_kobo > 0", name="ck_claim_amount"),
        CheckConstraint("settled_kobo >= 0", name="ck_claim_settled"),
        # dual control, DB-enforced on both maker-checker pairs
        CheckConstraint(
            "assignment_confirmed_by = '' OR assignment_confirmed_by <> assignment_proposed_by",
            name="ck_claim_assign_dual",
        ),
        CheckConstraint(
            "settlement_approved_by = '' OR settlement_approved_by <> settlement_proposed_by",
            name="ck_claim_settle_dual",
        ),
    )


class ClaimDocument(Base):
    """Vault references for claim evidence. Only the vault reference and the
    sha256 content digest are stored — never the document bytes."""

    __tablename__ = "claim_documents"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    claim_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("claims.id"), index=True)
    vault_ref: Mapped[str] = mapped_column(String(256))
    sha256: Mapped[str] = mapped_column(String(64))
    description: Mapped[str] = mapped_column(String(256), default="")
    uploaded_by: Mapped[str] = mapped_column(String(256))
    uploaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (
        UniqueConstraint("claim_id", "vault_ref", name="uq_claim_doc"),
    )


# ------------------------------------------------------------------ journal


class Journal(Base):
    __tablename__ = "journals"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    reference: Mapped[str] = mapped_column(String(128), unique=True)
    narration: Mapped[str] = mapped_column(Text, default="")
    posted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class LedgerEntry(Base):
    """Double-entry legs. A deferred constraint trigger rejects COMMIT of any
    journal whose legs do not balance (sum debits == sum credits)."""

    __tablename__ = "ledger_entries"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    journal_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("journals.id"), index=True)
    account: Mapped[str] = mapped_column(String(64))
    debit_kobo: Mapped[int] = mapped_column(BigInteger, default=0)
    credit_kobo: Mapped[int] = mapped_column(BigInteger, default=0)
    __table_args__ = (
        CheckConstraint("debit_kobo >= 0 AND credit_kobo >= 0", name="ck_entry_nonneg"),
        CheckConstraint("NOT (debit_kobo > 0 AND credit_kobo > 0)", name="ck_entry_one_side"),
    )


class PayoutReceipt(Base):
    """Payout execution receipt for a settled claim. Exact amount match
    against the settlement is REQUIRED; anything else is quarantined and
    never silently applied."""

    __tablename__ = "payout_receipts"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    claim_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("claims.id"))
    external_reference: Mapped[str] = mapped_column(String(128), unique=True)  # replay killer
    amount_kobo: Mapped[int] = mapped_column(BigInteger)
    currency: Mapped[str] = mapped_column(String(3))
    status: Mapped[str] = mapped_column(String(16))  # APPLIED | QUARANTINED
    quarantine_reason: Mapped[str] = mapped_column(Text, default="")
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (
        CheckConstraint("status IN ('APPLIED','QUARANTINED')", name="ck_receipt_status"),
        CheckConstraint("amount_kobo >= 0", name="ck_receipt_amount"),
    )


# ------------------------------------------------------- partner gateway


class GatewayCall(Base):
    """Append-only record of partner-insurer gateway interactions (request
    digests only, never payloads containing PII or secrets)."""

    __tablename__ = "gateway_calls"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    partner_id: Mapped[str] = mapped_column(String(64), index=True)
    operation: Mapped[str] = mapped_column(String(64))
    request_digest: Mapped[str] = mapped_column(String(64))  # sha256 hex
    response_status: Mapped[int] = mapped_column(Integer, default=0)
    outcome: Mapped[str] = mapped_column(String(24))  # OK | ADAPTER_UNCONFIGURED | UPSTREAM_ERROR
    created_by: Mapped[str] = mapped_column(String(256))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (
        CheckConstraint("outcome IN ('OK','ADAPTER_UNCONFIGURED','UPSTREAM_ERROR')", name="ck_gw_outcome"),
    )


# --------------------------------------------------------------------- ops


class IdempotencyRecord(Base):
    __tablename__ = "idempotency_records"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    key: Mapped[str] = mapped_column(String(128))
    principal_sub: Mapped[str] = mapped_column(String(256))
    request_hash: Mapped[str] = mapped_column(String(64))
    response_status: Mapped[int] = mapped_column(Integer)
    response_body: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (
        UniqueConstraint("key", "principal_sub", name="uq_idem_key_principal"),
    )


class AuditEvent(Base):
    """Hash-chained append-only audit. UPDATE/DELETE trigger-rejected."""

    __tablename__ = "audit_events"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    event_type: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    prev_hash: Mapped[str] = mapped_column(String(64))
    hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class OutboxMessage(Base):
    __tablename__ = "outbox_messages"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    event_id: Mapped[str] = mapped_column(String(128), unique=True)
    topic: Mapped[str] = mapped_column(String(128))
    event_type: Mapped[str] = mapped_column(String(64))
    classification: Mapped[str] = mapped_column(String(32))
    envelope: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str] = mapped_column(Text, default="")
    __table_args__ = (
        Index("ix_outbox_unpublished", "created_at", postgresql_where=(published_at.is_(None))),
    )


class StatusListSnapshot(Base):
    __tablename__ = "status_list_snapshots"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    purpose: Mapped[str] = mapped_column(String(32))  # revocation | suspension
    bitstring: Mapped[str] = mapped_column(Text)      # base64url compressed bitstring
    credential: Mapped[dict[str, Any]] = mapped_column(JSONB)  # signed StatusList2021-style VC
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ProcessedEvent(Base):
    """Inbound envelope dedupe (consumer replay killer)."""

    __tablename__ = "processed_events"
    event_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    processed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
