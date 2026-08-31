"""Integration routes: ISR evidence ingest, partner gateway, regulator
aggregator (read-only, classification-free aggregates)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import func, select

from insurance.api.deps import IdentityDep, SessionDep, SettingsDep, require_policy
from insurance.api.schemas import IsrEvidenceIn, PartnerCallIn
from insurance.models import Claim, IsrEvidence, Policy, Quote
from insurance.services import isr, partners
from insurance.services.isr import IsrError
from insurance.services.partners import GatewayError

router = APIRouter(prefix="/v1", tags=["integrations"])


# ---------------------------------------------------------- ISR evidence


@router.post("/integrations/isr/evidence", status_code=201)
async def ingest_isr_evidence(
    request: Request, body: IsrEvidenceIn, identity: IdentityDep,
    session: SessionDep, settings: SettingsDep,
) -> dict[str, Any]:
    require_policy(request, identity, "integration", "isr-ingest", "CONFIDENTIAL")
    if not settings.isr_evidence_configured:
        raise HTTPException(
            status_code=503,
            detail={"reason": "integration-unconfigured",
                    "detail": "ISR evidence integration is not configured; see GET /v1/capabilities"},
        )
    directory = request.app.state.key_directory
    if directory is None:
        raise HTTPException(status_code=503, detail={"reason": "key-directory-unavailable"})
    try:
        row = await isr.ingest_evidence(
            session, settings=settings, directory=directory,
            envelope=body.envelope, principal=identity.subject,
        )
        await session.commit()
    except IsrError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=400, detail={"reason": exc.reason, "detail": str(exc)},
        ) from exc
    return {
        "evidenceId": row.evidence_id, "corridor": row.corridor,
        "deltaBp": row.delta_bp, "sourceDigest": row.source_digest,
    }


# -------------------------------------------------------- partner gateway


@router.get("/partners")
async def list_partners(request: Request, identity: IdentityDep, settings: SettingsDep) -> dict[str, Any]:
    require_policy(request, identity, "partner", "read", "INTERNAL")
    return {"partners": partners.registry_view(settings)}


@router.post("/partners/{partner_id}:call")
async def call_partner(
    partner_id: str, request: Request, body: PartnerCallIn,
    identity: IdentityDep, session: SessionDep, settings: SettingsDep,
) -> dict[str, Any]:
    require_policy(request, identity, "partner", "call", "CONFIDENTIAL")
    try:
        result = await partners.call_partner(
            session, settings=settings, partner_id=partner_id,
            operation=body.operation, payload=body.payload,
            principal=identity.subject,
        )
        await session.commit()
    except GatewayError as exc:
        await session.rollback()
        status = 503 if exc.reason == "ADAPTER_UNCONFIGURED" else 502
        raise HTTPException(status_code=status, detail={"reason": exc.reason, "detail": str(exc)}) from exc
    return result


# ------------------------------------------------- regulator aggregator


@router.get("/aggregates/portfolio")
async def portfolio_aggregates(request: Request, identity: IdentityDep, session: SessionDep) -> dict[str, Any]:
    """Read-only regulator aggregates. Classification-free: counts and sums
    only — no principal identities, no assured identifiers, no declaration
    content. Accessible to the ``insurer-aggregator`` role."""
    require_policy(request, identity, "aggregate", "read", "INTERNAL")
    policies_by_status: dict[str, int] = {
        str(k): int(v) for k, v in (await session.execute(
            select(Policy.status, func.count()).group_by(Policy.status))).all()
    }
    premium_total = (
        await session.execute(select(func.coalesce(func.sum(Policy.premium_kobo), 0)))
    ).scalar_one()
    insured_total = (
        await session.execute(select(func.coalesce(func.sum(Policy.insured_value_kobo), 0)))
    ).scalar_one()
    claims_by_status: dict[str, int] = {
        str(k): int(v) for k, v in (await session.execute(
            select(Claim.status, func.count()).group_by(Claim.status))).all()
    }
    settled_total = (
        await session.execute(select(func.coalesce(func.sum(Claim.settled_kobo), 0)))
    ).scalar_one()
    quotes_by_status: dict[str, int] = {
        str(k): int(v) for k, v in (await session.execute(
            select(Quote.status, func.count()).group_by(Quote.status))).all()
    }
    corridors = (
        await session.execute(
            select(IsrEvidence.corridor, func.coalesce(func.sum(IsrEvidence.delta_bp), 0))
            .group_by(IsrEvidence.corridor)
        )
    ).all()
    return {
        "policiesByStatus": policies_by_status,
        "premiumTotalKobo": premium_total,
        "insuredValueTotalKobo": insured_total,
        "claimsByStatus": claims_by_status,
        "settledTotalKobo": settled_total,
        "quotesByStatus": quotes_by_status,
        "routeRiskByCorridorBp": {c: int(bp) for c, bp in corridors},
    }
