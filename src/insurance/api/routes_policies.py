"""Policy routes: read (Luhn-gated), endorse, cancel/lapse, status lists."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from insurance.api.deps import IdentityDep, JsonDict, SessionDep, SettingsDep, get_signing_key, require_policy
from insurance.api.schemas import (
    CancelIn,
    EndorsementIn,
    EndorsementRejectIn,
    PremiumReceiptIn,
    SuspendIn,
)
from insurance.domain.serials import PolicyNumberError, parse_policy_number
from insurance.models import Claim, Endorsement, Policy
from insurance.services import lifecycle, outbox, premiums, statuslists
from insurance.services.lifecycle import LifecycleError
from insurance.services.premiums import PremiumError

router = APIRouter(prefix="/v1", tags=["policies"])


def _err(exc: LifecycleError) -> HTTPException:
    status = 409 if exc.reason in ("bad-state", "dual-control-violation") else 400
    return HTTPException(status_code=status, detail={"reason": exc.reason, "detail": str(exc)})


def _premium_err(exc: PremiumError) -> HTTPException:
    status = 409 if exc.reason in ("bad-state",) else 400
    return HTTPException(status_code=status, detail={"reason": exc.reason, "detail": str(exc)})


async def _get_policy(session: AsyncSession, number: str) -> Policy:
    """Luhn-check-digit gate BEFORE any database lookup."""
    try:
        parse_policy_number(number)
    except PolicyNumberError as exc:
        raise HTTPException(status_code=400, detail={"reason": "malformed-policy-number", "detail": str(exc)}) from exc
    policy: Policy | None = (
        await session.execute(select(Policy).where(Policy.policy_number == number))
    ).scalar_one_or_none()
    if policy is None:
        raise HTTPException(status_code=404, detail={"reason": "unknown-policy"})
    return policy


def _policy_view(p: Policy, claim_count: int | None = None) -> dict[str, Any]:
    out = {
        "policyNumber": p.policy_number, "status": p.status,
        "productKind": p.family_code, "corridor": p.corridor,
        "declarationRef": p.declaration_ref, "currency": p.currency,
        "premiumKobo": p.premium_kobo, "insuredValueKobo": p.insured_value_kobo,
        "inceptionAt": p.inception_at.isoformat(), "expiryAt": p.expiry_at.isoformat(),
        "issuedBy": p.issued_by,
    }
    if claim_count is not None:
        out["claimCount"] = claim_count
    return out


@router.get("/policies/{policy_number}")
async def get_policy(policy_number: str, request: Request, identity: IdentityDep, session: SessionDep) -> JsonDict:
    require_policy(request, identity, "policy", "read", "INTERNAL")
    policy = await _get_policy(session, policy_number)
    claims = (
        await session.execute(select(func.count()).select_from(Claim).where(Claim.policy_id == policy.id))
    ).scalar_one()
    return _policy_view(policy, claim_count=claims)


@router.get("/policies/{policy_number}/credential")
async def get_policy_credential(
    policy_number: str,
    request: Request,
    identity: IdentityDep,
    session: SessionDep
) -> JsonDict:
    require_policy(request, identity, "policy", "read", "INTERNAL")
    policy = await _get_policy(session, policy_number)
    return policy.credential


@router.post("/policies/{policy_number}/endorsements", status_code=201)
async def add_endorsement(
    policy_number: str, request: Request, body: EndorsementIn,
    identity: IdentityDep, session: SessionDep,
) -> dict[str, Any]:
    require_policy(request, identity, "policy", "endorse", "CONFIDENTIAL")
    policy = await _get_policy(session, policy_number)
    try:
        endorsement = await lifecycle.add_endorsement(
            session, policy=policy, kind=body.kind,
            premium_delta_kobo=body.premium_delta_kobo, detail=body.detail,
            principal=identity.subject,
        )
        resource = lifecycle.policy_event_resource(policy)
        resource["endorsementNo"] = endorsement.endorsement_no
        resource["endorsementKind"] = endorsement.kind
        await outbox.enqueue(
            session, event_type="insurance.policy-endorsed.v1", resource=resource,
            signing_key=get_signing_key(request), principal_id=identity.subject,
            principal_role="underwriter", correlation_id=policy.policy_number,
        )
        await session.commit()
    except LifecycleError as exc:
        await session.rollback()
        raise _err(exc) from exc
    return {
        "policyNumber": policy.policy_number, "endorsementNo": endorsement.endorsement_no,
        "kind": endorsement.kind, "premiumDeltaKobo": endorsement.premium_delta_kobo,
        "status": endorsement.status,
    }


@router.post("/policies/{policy_number}/endorsements/{endorsement_no}:approve")
async def approve_endorsement(
    policy_number: str, endorsement_no: int, request: Request,
    identity: IdentityDep, session: SessionDep,
) -> dict[str, Any]:
    """Checker: approve a PROPOSED endorsement; posts the balanced premium
    delta journal. Checker != maker (service + DB CHECK)."""
    require_policy(request, identity, "policy", "endorse-decide", "CONFIDENTIAL")
    policy = await _get_policy(session, policy_number)
    try:
        endorsement = await lifecycle.approve_endorsement(
            session, policy=policy, endorsement_no=endorsement_no,
            principal=identity.subject,
        )
        resource = lifecycle.policy_event_resource(policy)
        resource["endorsementNo"] = endorsement.endorsement_no
        resource["endorsementKind"] = endorsement.kind
        await outbox.enqueue(
            session, event_type="insurance.policy-endorsed.v1", resource=resource,
            signing_key=get_signing_key(request), principal_id=identity.subject,
            principal_role="underwriter-approver", correlation_id=policy.policy_number,
        )
        await session.commit()
    except LifecycleError as exc:
        await session.rollback()
        raise _err(exc) from exc
    return {
        "policyNumber": policy.policy_number, "endorsementNo": endorsement.endorsement_no,
        "status": endorsement.status, "journalReference": endorsement.journal_reference,
    }


@router.post("/policies/{policy_number}/endorsements/{endorsement_no}:reject")
async def reject_endorsement(
    policy_number: str, endorsement_no: int, request: Request, body: EndorsementRejectIn,
    identity: IdentityDep, session: SessionDep,
) -> dict[str, Any]:
    """Checker: reject a PROPOSED endorsement. Terminal; no journal leg."""
    require_policy(request, identity, "policy", "endorse-decide", "CONFIDENTIAL")
    policy = await _get_policy(session, policy_number)
    try:
        endorsement = await lifecycle.reject_endorsement(
            session, policy=policy, endorsement_no=endorsement_no,
            principal=identity.subject, reason=body.reason,
        )
        await session.commit()
    except LifecycleError as exc:
        await session.rollback()
        raise _err(exc) from exc
    return {
        "policyNumber": policy.policy_number, "endorsementNo": endorsement.endorsement_no,
        "status": endorsement.status,
    }


@router.get("/policies/{policy_number}/endorsements")
async def list_endorsements(
    policy_number: str, request: Request, identity: IdentityDep, session: SessionDep
) -> JsonDict:
    require_policy(request, identity, "policy", "read", "INTERNAL")
    policy = await _get_policy(session, policy_number)
    rows = (
        await session.execute(
            select(Endorsement).where(Endorsement.policy_id == policy.id).order_by(Endorsement.endorsement_no)
        )
    ).scalars().all()
    return {
        "endorsements": [
            {
                "endorsementNo": e.endorsement_no, "kind": e.kind,
                "premiumDeltaKobo": e.premium_delta_kobo, "detail": e.detail,
                "status": e.status, "journalReference": e.journal_reference,
                "createdBy": e.created_by, "createdAt": e.created_at.isoformat(),
                "approvedBy": e.approved_by,
            }
            for e in rows
        ]
    }


@router.post("/policies/{policy_number}:cancel")
async def cancel_policy(
    policy_number: str, request: Request, body: CancelIn,
    identity: IdentityDep, session: SessionDep, settings: SettingsDep,
) -> dict[str, Any]:
    require_policy(request, identity, "policy", "cancel", "CONFIDENTIAL")
    policy = await _get_policy(session, policy_number)
    try:
        await lifecycle.cancel_or_lapse(
            session, settings=settings, signing_key=get_signing_key(request),
            policy=policy, new_status="CANCELLED", principal=identity.subject,
            reason=body.reason,
        )
        await outbox.enqueue(
            session, event_type="insurance.policy-cancelled.v1",
            resource=lifecycle.policy_event_resource(policy),
            signing_key=get_signing_key(request), principal_id=identity.subject,
            principal_role="underwriter", correlation_id=policy.policy_number,
        )
        await session.commit()
    except LifecycleError as exc:
        await session.rollback()
        raise _err(exc) from exc
    return _policy_view(policy)


@router.post("/policies/{policy_number}:suspend")
async def suspend_policy(
    policy_number: str, request: Request, body: SuspendIn,
    identity: IdentityDep, session: SessionDep, settings: SettingsDep,
) -> dict[str, Any]:
    """Suspend cover; sets the SUSPENSION status-list bit so offline
    verifiers fail closed."""
    require_policy(request, identity, "policy", "suspend", "CONFIDENTIAL")
    policy = await _get_policy(session, policy_number)
    try:
        await lifecycle.suspend_policy(
            session, settings=settings, signing_key=get_signing_key(request),
            policy=policy, principal=identity.subject, reason=body.reason,
        )
        await outbox.enqueue(
            session, event_type="insurance.policy-suspended.v1",
            resource=lifecycle.policy_event_resource(policy),
            signing_key=get_signing_key(request), principal_id=identity.subject,
            principal_role="underwriter-approver", correlation_id=policy.policy_number,
        )
        await session.commit()
    except LifecycleError as exc:
        await session.rollback()
        raise _err(exc) from exc
    return _policy_view(policy)


@router.post("/policies/{policy_number}:reinstate")
async def reinstate_policy(
    policy_number: str, request: Request, body: SuspendIn,
    identity: IdentityDep, session: SessionDep, settings: SettingsDep,
) -> dict[str, Any]:
    """Reinstate a suspended policy; clears the suspension status-list bit."""
    require_policy(request, identity, "policy", "suspend", "CONFIDENTIAL")
    policy = await _get_policy(session, policy_number)
    try:
        await lifecycle.reinstate_policy(
            session, settings=settings, signing_key=get_signing_key(request),
            policy=policy, principal=identity.subject, reason=body.reason,
        )
        await outbox.enqueue(
            session, event_type="insurance.policy-reinstated.v1",
            resource=lifecycle.policy_event_resource(policy),
            signing_key=get_signing_key(request), principal_id=identity.subject,
            principal_role="underwriter-approver", correlation_id=policy.policy_number,
        )
        await session.commit()
    except LifecycleError as exc:
        await session.rollback()
        raise _err(exc) from exc
    return _policy_view(policy)


@router.post("/policies:premium-receipt", status_code=201)
async def premium_receipt(
    request: Request, body: PremiumReceiptIn, identity: IdentityDep, session: SessionDep,
) -> dict[str, Any]:
    """Record a premium payment receipt. Exact amount + currency match or
    QUARANTINED; an applied receipt posts Dr insurer:clearing / Cr
    premium:receivable."""
    require_policy(request, identity, "policy", "receive-premium", "FIDUCIARY_SEGREGATED")
    try:
        receipt = await premiums.record_premium_receipt(
            session, external_reference=body.external_reference,
            policy_number=body.policy_number, amount_kobo=body.amount_kobo,
            currency=body.currency, principal=identity.subject,
        )
        if receipt.status == "APPLIED" and receipt.policy_id is not None:
            policy = (
                await session.execute(select(Policy).where(Policy.id == receipt.policy_id))
            ).scalar_one()
            await outbox.enqueue(
                session, event_type="insurance.premium-received.v1",
                resource=lifecycle.policy_event_resource(policy),
                signing_key=get_signing_key(request), principal_id=identity.subject,
                principal_role="finance-officer", correlation_id=policy.policy_number,
            )
        await session.commit()
    except PremiumError as exc:
        await session.rollback()
        raise _premium_err(exc) from exc
    return {
        "externalReference": receipt.external_reference, "status": receipt.status,
        "quarantineReason": receipt.quarantine_reason,
        "journalReference": receipt.journal_reference,
    }


@router.get("/status-list/{purpose}")
async def get_status_list(purpose: str, request: Request, session: SessionDep) -> dict[str, Any]:
    """Public, unauthenticated: the signed bitstring status-list credential
    is itself the authenticity object (verifiers check the proof)."""
    if purpose not in ("revocation", "suspension"):
        raise HTTPException(status_code=404, detail={"reason": "unknown-purpose"})
    credential = await statuslists.current_credential(session, purpose)
    if credential is None:
        raise HTTPException(status_code=404, detail={"reason": "status-list-not-published"})
    return credential
