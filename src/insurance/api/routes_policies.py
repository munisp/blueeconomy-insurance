"""Policy routes: read (Luhn-gated), endorse, cancel/lapse, status lists."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from insurance.api.deps import IdentityDep, JsonDict, SessionDep, SettingsDep, get_signing_key, require_policy
from insurance.api.schemas import CancelIn, EndorsementIn
from insurance.domain.serials import PolicyNumberError, parse_policy_number
from insurance.models import Claim, Endorsement, Policy
from insurance.services import lifecycle, outbox, statuslists
from insurance.services.lifecycle import LifecycleError

router = APIRouter(prefix="/v1", tags=["policies"])


def _err(exc: LifecycleError) -> HTTPException:
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
                "createdBy": e.created_by, "createdAt": e.created_at.isoformat(),
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
