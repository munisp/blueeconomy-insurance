"""Quote lifecycle routes: quote -> bind (maker-checker) -> issue.

Includes the declaration-time attach API (NTP VAS model):
POST /v1/quotes:fromDeclaration consumes the platform declaration shape so
singlewindow can offer insurance at declaration time.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from insurance.api.auth import Identity
from insurance.api.deps import (
    IdempotencyKey,
    IdentityDep,
    JsonDict,
    SessionDep,
    SettingsDep,
    get_signing_key,
    require_policy,
)
from insurance.api.schemas import BindDecisionIn, FromDeclaration, IssueIn, QuoteCreate
from insurance.config import Settings
from insurance.models import Policy, Quote
from insurance.services import lifecycle, outbox
from insurance.services.lifecycle import LifecycleError

router = APIRouter(prefix="/v1/quotes", tags=["quotes"])


def _err(exc: LifecycleError) -> HTTPException:
    status = 409 if exc.reason in (
        "bad-state", "quote-expired", "dual-control-violation",
    ) else 400
    return HTTPException(status_code=status, detail={"reason": exc.reason, "detail": str(exc)})


def _quote_view(q: Quote) -> dict[str, Any]:
    return {
        "quoteRef": q.quote_ref, "status": q.status, "corridor": q.corridor,
        "declarationRef": q.declaration_ref, "currency": q.currency,
        "premiumKobo": q.premium_kobo, "ratingTrace": q.rating_trace,
        "createdBy": q.created_by, "createdAt": q.created_at.isoformat(),
        "expiresAt": q.expires_at.isoformat(),
        "bindRequestedBy": q.bind_requested_by, "boundBy": q.bound_by,
    }


async def _get_quote(session: AsyncSession, ref: str) -> Quote:
    quote: Quote | None = (
        await session.execute(select(Quote).where(Quote.quote_ref == ref))
    ).scalar_one_or_none()
    if quote is None:
        raise HTTPException(status_code=404, detail={"reason": "unknown-quote"})
    return quote


async def _create_quote_common(
    request: Request,
    session: AsyncSession,
    settings: Settings,
    identity: Identity,
    *,
    product_code: str,
    product_version: int | None,
    lines: list[dict[str, Any]],
    corridor: str,
    declaration_ref: str,
    source_event_id: str,
    assured_name: str,
    assured_tin: str,
    expected_premium_kobo: int | None,
    idempotency_key: str,
) -> Quote:
    product = await lifecycle.get_active_product(session, product_code, product_version)
    quote = await lifecycle.create_quote(
        session, settings=settings, product=product, lines=lines,
        corridor=corridor, declaration_ref=declaration_ref,
        source_event_id=source_event_id, assured_name=assured_name,
        assured_tin=assured_tin, principal=identity.subject,
        idempotency_key=idempotency_key or "",
    )
    if expected_premium_kobo is not None and expected_premium_kobo != quote.premium_kobo:
        await session.rollback()
        raise HTTPException(
            status_code=422,
            detail={
                "reason": "client-total-rejected",
                "detail": f"client expected {expected_premium_kobo} kobo but server rated "
                          f"{quote.premium_kobo} kobo; premiums are computed server-side only",
                "serverPremiumKobo": quote.premium_kobo,
            },
        )
    return quote


@router.post("", status_code=201)
async def create_quote(
    request: Request, body: QuoteCreate, identity: IdentityDep,
    session: SessionDep, settings: SettingsDep, idem: IdempotencyKey = None,
) -> dict[str, Any]:
    require_policy(request, identity, "quote", "create", "CONFIDENTIAL")
    try:
        quote = await _create_quote_common(
            request, session, settings, identity,
            product_code=body.product_code, product_version=body.product_version,
            lines=[line.model_dump() for line in body.lines],
            corridor=body.corridor, declaration_ref=body.declaration_ref,
            source_event_id="", assured_name=body.assured_name,
            assured_tin=body.assured_tin,
            expected_premium_kobo=body.expected_premium_kobo,
            idempotency_key=idem or "",
        )
        await session.commit()
    except LifecycleError as exc:
        await session.rollback()
        raise _err(exc) from exc
    return _quote_view(quote)


@router.post(":fromDeclaration", status_code=201)
async def quote_from_declaration(
    request: Request, body: FromDeclaration, identity: IdentityDep,
    session: SessionDep, settings: SettingsDep, idem: IdempotencyKey = None,
) -> dict[str, Any]:
    """Declaration-time attach (NTP VAS model): singlewindow posts the
    platform declaration shape and receives a bound-ready quote offer.
    Declaration dedupe: one quote per (declaration_ref, product) pair from a
    given source event; replays of the same source_event_id return the
    original quote."""
    require_policy(request, identity, "quote", "create", "CONFIDENTIAL")
    if body.source_event_id:
        existing = (
            await session.execute(
                select(Quote).where(Quote.source_event_id == body.source_event_id)
            )
        ).scalar_one_or_none()
        if existing is not None:
            return _quote_view(existing)
    try:
        quote = await _create_quote_common(
            request, session, settings, identity,
            product_code=body.product_code, product_version=None,
            lines=[
                {
                    "description": line.description, "hs_code": line.hs_code,
                    "risk_class": line.risk_class, "insured_value_kobo": line.customs_value_kobo,
                }
                for line in body.lines
            ],
            corridor=body.corridor, declaration_ref=body.declaration_ref,
            source_event_id=body.source_event_id, assured_name=body.consignee_name,
            assured_tin=body.consignee_tin, expected_premium_kobo=None,
            idempotency_key=idem or "",
        )
        await session.commit()
    except LifecycleError as exc:
        await session.rollback()
        raise _err(exc) from exc
    return _quote_view(quote)


@router.get("/{quote_ref}")
async def get_quote(quote_ref: str, request: Request, identity: IdentityDep, session: SessionDep) -> dict[str, Any]:
    require_policy(request, identity, "quote", "read", "INTERNAL")
    return _quote_view(await _get_quote(session, quote_ref))


@router.post("/{quote_ref}:bind")
async def request_bind(quote_ref: str, request: Request, identity: IdentityDep, session: SessionDep) -> dict[str, Any]:
    """Maker: request bind."""
    require_policy(request, identity, "quote", "bind", "CONFIDENTIAL")
    quote = await _get_quote(session, quote_ref)
    try:
        await lifecycle.request_bind(session, quote, identity.subject)
        await session.commit()
    except LifecycleError as exc:
        await session.rollback()
        raise _err(exc) from exc
    return _quote_view(quote)


@router.post("/{quote_ref}:bind-decision")
async def decide_bind(
    quote_ref: str, request: Request, body: BindDecisionIn,
    identity: IdentityDep, session: SessionDep,
) -> dict[str, Any]:
    """Checker: approve or decline. Checker != maker (service + DB CHECK)."""
    require_policy(request, identity, "quote", "bind-decide", "CONFIDENTIAL")
    quote = await _get_quote(session, quote_ref)
    try:
        await lifecycle.decide_bind(
            session, quote, decision=body.decision,
            principal=identity.subject, reason=body.reason,
        )
        await session.commit()
    except LifecycleError as exc:
        await session.rollback()
        raise _err(exc) from exc
    return _quote_view(quote)


@router.post("/{quote_ref}:issue", status_code=201)
async def issue_policy(
    quote_ref: str, request: Request, body: IssueIn,
    identity: IdentityDep, session: SessionDep, settings: SettingsDep,
) -> dict[str, Any]:
    """Issue the policy as a signed W3C VC 2.0 credential and publish
    insurance.policy.v1 (transactional outbox)."""
    require_policy(request, identity, "policy", "issue", "CONFIDENTIAL")
    quote = await _get_quote(session, quote_ref)
    try:
        policy = await lifecycle.issue_policy(
            session, settings=settings, signing_key=get_signing_key(request),
            quote=quote, inception_at=body.inception_at, expiry_at=body.expiry_at,
            principal=identity.subject,
        )
        await outbox.enqueue(
            session, event_type="insurance.policy.v1",
            resource=lifecycle.policy_event_resource(policy),
            signing_key=get_signing_key(request),
            principal_id=identity.subject, principal_role="underwriter",
            correlation_id=quote.quote_ref,
        )
        await session.commit()
    except LifecycleError as exc:
        await session.rollback()
        raise _err(exc) from exc
    return {
        "policyNumber": policy.policy_number, "status": policy.status,
        "premiumKobo": policy.premium_kobo, "insuredValueKobo": policy.insured_value_kobo,
        "inceptionAt": policy.inception_at.isoformat(), "expiryAt": policy.expiry_at.isoformat(),
        "credential": policy.credential,
    }


@router.get("/{quote_ref}/policy")
async def get_policy_for_quote(
    quote_ref: str, request: Request, identity: IdentityDep, session: SessionDep
) -> JsonDict:
    require_policy(request, identity, "policy", "read", "INTERNAL")
    quote = await _get_quote(session, quote_ref)
    policy = (
        await session.execute(select(Policy).where(Policy.quote_id == quote.id))
    ).scalar_one_or_none()
    if policy is None:
        raise HTTPException(status_code=404, detail={"reason": "no-policy"})
    return {
        "policyNumber": policy.policy_number, "status": policy.status,
        "premiumKobo": policy.premium_kobo, "insuredValueKobo": policy.insured_value_kobo,
        "inceptionAt": policy.inception_at.isoformat(), "expiryAt": policy.expiry_at.isoformat(),
        "credential": policy.credential,
    }
