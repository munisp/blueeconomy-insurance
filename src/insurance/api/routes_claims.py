"""Claims routes: FNOL -> documents -> adjuster maker-checker -> settlement
maker-checker (double-entry journal) -> payout receipt."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from insurance.api.deps import (
    IdempotencyKey,
    IdentityDep,
    JsonDict,
    SessionDep,
    get_signing_key,
    require_policy,
)
from insurance.api.schemas import (
    AdjusterProposal,
    DocumentIn,
    FnolIn,
    PayoutReceiptIn,
    RejectIn,
    SettlementProposal,
)
from insurance.domain.serials import PolicyNumberError, parse_policy_number
from insurance.models import Claim, ClaimDocument, Policy
from insurance.services import claims as claims_svc
from insurance.services import outbox
from insurance.services.claims import ClaimError

router = APIRouter(prefix="/v1/claims", tags=["claims"])


def _err(exc: ClaimError) -> HTTPException:
    status = 409 if exc.reason in (
        "bad-state", "dual-control-violation", "not-assigned-adjuster",
        "policy-not-active", "loss-outside-cover",
    ) else 400
    return HTTPException(status_code=status, detail={"reason": exc.reason, "detail": str(exc)})


def _claim_view(c: Claim) -> dict[str, Any]:
    return {
        "claimRef": c.claim_ref, "status": c.status,
        "claimedKobo": c.claimed_kobo, "settledKobo": c.settled_kobo,
        "adjusterSub": c.adjuster_sub, "journalReference": c.journal_reference,
        "triggerEventId": c.trigger_event_id,
        "reportedBy": c.reported_by, "reportedAt": c.reported_at.isoformat(),
        "lossOccurredAt": c.loss_occurred_at.isoformat(),
    }


async def _get_claim(session: AsyncSession, ref: str) -> Claim:
    claim: Claim | None = (
        await session.execute(select(Claim).where(Claim.claim_ref == ref))
    ).scalar_one_or_none()
    if claim is None:
        raise HTTPException(status_code=404, detail={"reason": "unknown-claim"})
    return claim


@router.post("", status_code=201)
async def file_fnol(
    request: Request, body: FnolIn, identity: IdentityDep,
    session: SessionDep, idem: IdempotencyKey = None,
) -> dict[str, Any]:
    require_policy(request, identity, "claim", "create", "CONFIDENTIAL")
    try:
        parse_policy_number(body.policy_number)
    except PolicyNumberError as exc:
        raise HTTPException(status_code=400, detail={"reason": "malformed-policy-number", "detail": str(exc)}) from exc
    policy = (
        await session.execute(select(Policy).where(Policy.policy_number == body.policy_number))
    ).scalar_one_or_none()
    if policy is None:
        raise HTTPException(status_code=404, detail={"reason": "unknown-policy"})
    try:
        claim = await claims_svc.file_fnol(
            session, policy=policy, loss_occurred_at=body.loss_occurred_at,
            loss_description=body.loss_description, claimed_kobo=body.claimed_kobo,
            trigger_event_id=body.trigger_event_id, principal=identity.subject,
            idempotency_key=idem or "",
        )
        await session.commit()
    except ClaimError as exc:
        await session.rollback()
        raise _err(exc) from exc
    return _claim_view(claim)


@router.get("/{claim_ref}")
async def get_claim(claim_ref: str, request: Request, identity: IdentityDep, session: SessionDep) -> dict[str, Any]:
    require_policy(request, identity, "claim", "read", "INTERNAL")
    claim = await _get_claim(session, claim_ref)
    docs = (
        await session.execute(select(ClaimDocument).where(ClaimDocument.claim_id == claim.id))
    ).scalars().all()
    out = _claim_view(claim)
    out["documents"] = [
        {"vaultRef": d.vault_ref, "sha256": d.sha256, "description": d.description}
        for d in docs
    ]
    return out


@router.post("/{claim_ref}/documents", status_code=201)
async def attach_document(
    claim_ref: str, request: Request, body: DocumentIn,
    identity: IdentityDep, session: SessionDep,
) -> dict[str, Any]:
    require_policy(request, identity, "claim", "document", "CONFIDENTIAL")
    claim = await _get_claim(session, claim_ref)
    try:
        doc = await claims_svc.attach_document(
            session, claim=claim, vault_ref=body.vault_ref, sha256=body.sha256,
            description=body.description, principal=identity.subject,
        )
        await session.commit()
    except ClaimError as exc:
        await session.rollback()
        raise _err(exc) from exc
    return {"vaultRef": doc.vault_ref, "sha256": doc.sha256}


@router.post("/{claim_ref}:propose-adjuster")
async def propose_adjuster(
    claim_ref: str, request: Request, body: AdjusterProposal,
    identity: IdentityDep, session: SessionDep,
) -> dict[str, Any]:
    require_policy(request, identity, "claim", "assign-propose", "CONFIDENTIAL")
    claim = await _get_claim(session, claim_ref)
    try:
        await claims_svc.propose_adjuster(
            session, claim=claim, adjuster_sub=body.adjuster_sub, principal=identity.subject,
        )
        await session.commit()
    except ClaimError as exc:
        await session.rollback()
        raise _err(exc) from exc
    return _claim_view(claim)


@router.post("/{claim_ref}:confirm-adjuster")
async def confirm_adjuster(claim_ref: str, request: Request, identity: IdentityDep, session: SessionDep) -> JsonDict:
    require_policy(request, identity, "claim", "assign-confirm", "CONFIDENTIAL")
    claim = await _get_claim(session, claim_ref)
    try:
        await claims_svc.confirm_adjuster(session, claim=claim, principal=identity.subject)
        await session.commit()
    except ClaimError as exc:
        await session.rollback()
        raise _err(exc) from exc
    return _claim_view(claim)


@router.post("/{claim_ref}:propose-settlement")
async def propose_settlement(
    claim_ref: str, request: Request, body: SettlementProposal,
    identity: IdentityDep, session: SessionDep,
) -> dict[str, Any]:
    require_policy(request, identity, "claim", "settle-propose", "FIDUCIARY_SEGREGATED")
    claim = await _get_claim(session, claim_ref)
    try:
        await claims_svc.propose_settlement(
            session, claim=claim, settled_kobo=body.settled_kobo, principal=identity.subject,
        )
        await session.commit()
    except ClaimError as exc:
        await session.rollback()
        raise _err(exc) from exc
    return _claim_view(claim)


@router.post("/{claim_ref}:approve-settlement")
async def approve_settlement(claim_ref: str, request: Request, identity: IdentityDep, session: SessionDep) -> JsonDict:
    require_policy(request, identity, "claim", "settle-approve", "FIDUCIARY_SEGREGATED")
    claim = await _get_claim(session, claim_ref)
    policy = (
        await session.execute(select(Policy).where(Policy.id == claim.policy_id))
    ).scalar_one()
    try:
        journal_ref = await claims_svc.approve_settlement(
            session, claim=claim, principal=identity.subject,
        )
        await outbox.enqueue(
            session, event_type="insurance.claim.v1",
            resource=claims_svc.claim_event_resource(claim, policy),
            signing_key=get_signing_key(request), principal_id=identity.subject,
            principal_role="claims-approver", correlation_id=claim.claim_ref,
        )
        await session.commit()
    except ClaimError as exc:
        await session.rollback()
        raise _err(exc) from exc
    out = _claim_view(claim)
    out["journalReference"] = journal_ref
    return out


@router.post("/{claim_ref}:reject")
async def reject_claim(
    claim_ref: str, request: Request, body: RejectIn,
    identity: IdentityDep, session: SessionDep,
) -> dict[str, Any]:
    require_policy(request, identity, "claim", "reject", "CONFIDENTIAL")
    claim = await _get_claim(session, claim_ref)
    try:
        await claims_svc.reject_claim(session, claim=claim, principal=identity.subject, reason=body.reason)
        await session.commit()
    except ClaimError as exc:
        await session.rollback()
        raise _err(exc) from exc
    return _claim_view(claim)


@router.post(":payout-receipt", status_code=201)
async def payout_receipt(
    request: Request, body: PayoutReceiptIn, identity: IdentityDep, session: SessionDep,
) -> dict[str, Any]:
    require_policy(request, identity, "claim", "payout", "FIDUCIARY_SEGREGATED")
    try:
        receipt = await claims_svc.record_payout_receipt(
            session, external_reference=body.external_reference,
            claim_ref=body.claim_ref, amount_kobo=body.amount_kobo,
            currency=body.currency, principal=identity.subject,
        )
        if receipt.status == "APPLIED" and receipt.claim_id is not None:
            claim = (
                await session.execute(select(Claim).where(Claim.id == receipt.claim_id))
            ).scalar_one()
            policy = (
                await session.execute(select(Policy).where(Policy.id == claim.policy_id))
            ).scalar_one()
            await outbox.enqueue(
                session, event_type="insurance.claim-paid.v1",
                resource=claims_svc.claim_event_resource(claim, policy),
                signing_key=get_signing_key(request), principal_id=identity.subject,
                principal_role="finance-officer", correlation_id=claim.claim_ref,
            )
        await session.commit()
    except ClaimError as exc:
        await session.rollback()
        raise _err(exc) from exc
    return {
        "externalReference": receipt.external_reference, "status": receipt.status,
        "quarantineReason": receipt.quarantine_reason,
    }
