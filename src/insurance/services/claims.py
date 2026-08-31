"""Claims: FNOL -> document vault refs -> adjuster assignment (maker-checker)
-> settlement (maker-checker) via the double-entry journal -> payout receipt.

Settlement money movement is a balanced journal enforced by the deferred DB
trigger; payout is recorded from an external receipt that must match the
settlement exactly or be quarantined — never silently applied.
"""

from __future__ import annotations

import secrets
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from insurance.models import (
    Claim,
    ClaimDocument,
    PayoutReceipt,
    Policy,
    utcnow,
)
from insurance.services import audit
from insurance.services.journal import post_journal

# Settlement ledger accounts (kobo, double-entry).
ACCT_CLAIMS_PAYABLE = "claims:payable"
ACCT_INSURER_CLEARING = "insurer:clearing"
ACCT_PREMIUM_INCOME = "premium:income"
ACCT_PREMIUM_RECEIVABLE = "premium:receivable"


class ClaimError(ValueError):
    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason


def _ref() -> str:
    return f"C-{secrets.token_hex(8)}"


async def file_fnol(
    session: AsyncSession,
    *,
    policy: Policy,
    loss_occurred_at: datetime,
    loss_description: str,
    claimed_kobo: int,
    trigger_event_id: str,
    principal: str,
    idempotency_key: str = "",
) -> Claim:
    """First notice of loss. Cover must be in force at the loss instant;
    the claimed amount can never exceed the insured value."""
    if policy.status != "ACTIVE":
        raise ClaimError("policy-not-active", f"policy is {policy.status}")
    if not (policy.inception_at <= loss_occurred_at <= policy.expiry_at):
        raise ClaimError("loss-outside-cover", "loss did not occur within the cover window")
    if claimed_kobo <= 0 or claimed_kobo > policy.insured_value_kobo:
        raise ClaimError("amount-out-of-range", "claimed amount must be in (0, insured value]")
    product_kind = (
        await session.execute(select(Policy.family_code).where(Policy.id == policy.id))
    ).scalar_one()
    if product_kind == "FRY" and not trigger_event_id:
        raise ClaimError(
            "trigger-required",
            "parametric ferry claims require the signed disruption trigger event id",
        )
    claim = Claim(
        claim_ref=_ref(),
        policy_id=policy.id,
        status="FNOL",
        loss_occurred_at=loss_occurred_at,
        loss_description=loss_description[:4000],
        trigger_event_id=trigger_event_id,
        claimed_kobo=claimed_kobo,
        reported_by=principal,
        idempotency_key=idempotency_key,
    )
    session.add(claim)
    await session.flush()
    await audit.record(session, "claim.fnol", {
        "claimRef": claim.claim_ref, "policyNumber": policy.policy_number,
        "claimedKobo": claimed_kobo, "triggerEventId": trigger_event_id, "by": principal,
    })
    return claim


async def attach_document(
    session: AsyncSession,
    *,
    claim: Claim,
    vault_ref: str,
    sha256: str,
    description: str,
    principal: str,
) -> ClaimDocument:
    if claim.status not in ("FNOL", "UNDER_REVIEW"):
        raise ClaimError("bad-state", f"claim is {claim.status}")
    if len(sha256) != 64 or any(c not in "0123456789abcdef" for c in sha256):
        raise ClaimError("malformed-digest", "sha256 must be lowercase hex")
    doc = ClaimDocument(
        claim_id=claim.id, vault_ref=vault_ref, sha256=sha256,
        description=description[:256], uploaded_by=principal,
    )
    session.add(doc)
    if claim.status == "FNOL":
        claim.status = "UNDER_REVIEW"
    await session.flush()
    await audit.record(session, "claim.document-attached", {
        "claimRef": claim.claim_ref, "vaultRef": vault_ref, "sha256": sha256, "by": principal,
    })
    return doc


async def propose_adjuster(
    session: AsyncSession, *, claim: Claim, adjuster_sub: str, principal: str
) -> None:
    """Maker: propose the adjuster assignment."""
    await session.execute(select(Claim.id).where(Claim.id == claim.id).with_for_update())
    await session.refresh(claim)
    if claim.status != "UNDER_REVIEW":
        raise ClaimError("bad-state", f"claim is {claim.status}, not UNDER_REVIEW")
    claim.status = "ADJUSTER_PENDING"
    claim.adjuster_sub = adjuster_sub
    claim.assignment_proposed_by = principal
    await session.flush()
    await audit.record(session, "claim.adjuster-proposed", {
        "claimRef": claim.claim_ref, "adjuster": adjuster_sub, "by": principal,
    })


async def confirm_adjuster(session: AsyncSession, *, claim: Claim, principal: str) -> None:
    """Checker: confirm the assignment; checker != proposer (also DB CHECK)."""
    await session.execute(select(Claim.id).where(Claim.id == claim.id).with_for_update())
    await session.refresh(claim)
    if claim.status != "ADJUSTER_PENDING":
        raise ClaimError("bad-state", f"claim is {claim.status}, not ADJUSTER_PENDING")
    if principal == claim.assignment_proposed_by:
        raise ClaimError("dual-control-violation", "confirmer must differ from proposer")
    claim.status = "ADJUSTER_ASSIGNED"
    claim.assignment_confirmed_by = principal
    await session.flush()
    await audit.record(session, "claim.adjuster-confirmed", {
        "claimRef": claim.claim_ref, "adjuster": claim.adjuster_sub, "by": principal,
    })


async def propose_settlement(
    session: AsyncSession, *, claim: Claim, settled_kobo: int, principal: str
) -> None:
    """Maker (the assigned adjuster): propose the settlement amount."""
    await session.execute(select(Claim.id).where(Claim.id == claim.id).with_for_update())
    await session.refresh(claim)
    if claim.status != "ADJUSTER_ASSIGNED":
        raise ClaimError("bad-state", f"claim is {claim.status}, not ADJUSTER_ASSIGNED")
    if principal != claim.adjuster_sub:
        raise ClaimError("not-assigned-adjuster", "only the assigned adjuster proposes settlement")
    if settled_kobo <= 0 or settled_kobo > claim.claimed_kobo:
        raise ClaimError("amount-out-of-range", "settlement must be in (0, claimed]")
    claim.status = "SETTLEMENT_PENDING"
    claim.settled_kobo = settled_kobo
    claim.settlement_proposed_by = principal
    await session.flush()
    await audit.record(session, "claim.settlement-proposed", {
        "claimRef": claim.claim_ref, "settledKobo": settled_kobo, "by": principal,
    })


async def approve_settlement(session: AsyncSession, *, claim: Claim, principal: str) -> str:
    """Checker: approve the settlement and post the balanced journal
    (DR claims payable / CR insurer clearing) atomically with the state
    change. Returns the journal reference."""
    await session.execute(select(Claim.id).where(Claim.id == claim.id).with_for_update())
    await session.refresh(claim)
    if claim.status != "SETTLEMENT_PENDING":
        raise ClaimError("bad-state", f"claim is {claim.status}, not SETTLEMENT_PENDING")
    if principal == claim.settlement_proposed_by:
        raise ClaimError("dual-control-violation", "settlement approver must differ from proposer")
    reference = f"settlement:{claim.claim_ref}"
    journal = await post_journal(
        session,
        reference=reference,
        narration=f"Claim settlement {claim.claim_ref}",
        legs=[
            (ACCT_CLAIMS_PAYABLE, claim.settled_kobo, 0),
            (ACCT_INSURER_CLEARING, 0, claim.settled_kobo),
        ],
    )
    claim.status = "SETTLED"
    claim.settlement_approved_by = principal
    claim.journal_reference = journal.reference
    claim.settled_at = utcnow()
    await session.flush()
    await audit.record(session, "claim.settled", {
        "claimRef": claim.claim_ref, "settledKobo": claim.settled_kobo,
        "journalReference": journal.reference, "by": principal,
    })
    return journal.reference


async def reject_claim(session: AsyncSession, *, claim: Claim, principal: str, reason: str) -> None:
    await session.execute(select(Claim.id).where(Claim.id == claim.id).with_for_update())
    await session.refresh(claim)
    if claim.status in ("SETTLED", "PAID", "REJECTED"):
        raise ClaimError("bad-state", f"claim is {claim.status}")
    claim.status = "REJECTED"
    await session.flush()
    await audit.record(session, "claim.rejected", {
        "claimRef": claim.claim_ref, "by": principal, "reason": reason,
    })


async def record_payout_receipt(
    session: AsyncSession,
    *,
    external_reference: str,
    claim_ref: str,
    amount_kobo: int,
    currency: str,
    principal: str,
) -> PayoutReceipt:
    """Record a payout execution receipt. Exact amount + currency match
    against the settlement is REQUIRED; anything else is quarantined."""
    claim = (
        await session.execute(select(Claim).where(Claim.claim_ref == claim_ref))
    ).scalar_one_or_none()
    if claim is None:
        receipt = PayoutReceipt(
            claim_id=None, external_reference=external_reference,
            amount_kobo=amount_kobo, currency=currency,
            status="QUARANTINED", quarantine_reason="unknown claim_ref",
        )
        session.add(receipt)
        await session.flush()
        await audit.record(session, "claim.payout-quarantined", {
            "claimRef": claim_ref, "externalReference": external_reference,
            "reason": "unknown claim_ref", "by": principal,
        })
        return receipt
    if claim.status != "SETTLED":
        raise ClaimError("bad-state", f"claim is {claim.status}, not SETTLED")
    if currency != "NGN" or amount_kobo != claim.settled_kobo:
        receipt = PayoutReceipt(
            claim_id=claim.id, external_reference=external_reference,
            amount_kobo=amount_kobo, currency=currency,
            status="QUARANTINED", quarantine_reason="amount/currency mismatch with settlement",
        )
        session.add(receipt)
        await session.flush()
        await audit.record(session, "claim.payout-quarantined", {
            "claimRef": claim_ref, "externalReference": external_reference,
            "reason": "amount/currency mismatch", "by": principal,
        })
        return receipt
    receipt = PayoutReceipt(
        claim_id=claim.id, external_reference=external_reference,
        amount_kobo=amount_kobo, currency=currency, status="APPLIED",
    )
    session.add(receipt)
    claim.status = "PAID"
    await session.flush()
    await audit.record(session, "claim.paid", {
        "claimRef": claim_ref, "externalReference": external_reference,
        "amountKobo": amount_kobo, "by": principal,
    })
    return receipt


def claim_event_resource(claim: Claim, policy: Policy) -> dict[str, Any]:
    return {
        "resourceType": "Basic",
        "claimId": str(claim.id),
        "claimRef": claim.claim_ref,
        "policyNumber": policy.policy_number,
        "status": claim.status,
        "claimedKobo": claim.claimed_kobo,
        "settledKobo": claim.settled_kobo,
        "journalReference": claim.journal_reference,
        "triggerEventId": claim.trigger_event_id,
    }
