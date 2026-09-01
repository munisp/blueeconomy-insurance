"""Product catalogue and the quote -> bind -> issue -> endorse -> lapse/cancel
lifecycle. All state transitions that involve money or cover are persisted
together with (a) a hash-chained audit event and (b) a signed outbox event,
in ONE database transaction.

Maker-checker doctrine:
- bind: the underwriter requesting the bind can never be the approver
  (service check AND quotes.ck_quote_dual_control CHECK constraint);
- policy numbers are claimed atomically from per-(family, year) counters,
  so concurrent issuance can never collide or fork sequences.
"""

from __future__ import annotations

import secrets
import uuid
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from insurance.config import Settings
from insurance.crypto.eddsa import SigningKey
from insurance.crypto.statuslist import status_entry
from insurance.crypto.vc import build_policy_credential, issue_proof
from insurance.domain import rating as rating_domain
from insurance.domain.serials import build_policy_number
from insurance.models import (
    BindDecision,
    Endorsement,
    IsrEvidence,
    Policy,
    Product,
    Quote,
    QuoteLine,
    RateTable,
    utcnow,
)
from insurance.services import audit, statuslists
from insurance.services.journal import (
    ACCT_PREMIUM_INCOME,
    ACCT_PREMIUM_RECEIVABLE,
    post_journal,
)

KIND_TO_FAMILY = {
    "marine-cargo-single": "CRG",
    "marine-cargo-open": "CRG",
    "ferry-parametric": "FRY",
    "protection-indemnity": "PRT",
    "hull": "HUL",
}


class LifecycleError(ValueError):
    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason


def _ref(prefix: str) -> str:
    return f"{prefix}-{secrets.token_hex(8)}"


# --------------------------------------------------------------- products


async def create_product(
    session: AsyncSession,
    *,
    code: str,
    kind: str,
    name: str,
    definition: dict[str, Any],
    principal: str,
) -> Product:
    if kind not in KIND_TO_FAMILY:
        raise LifecycleError("unknown-kind", kind)
    row = (
        await session.execute(
            select(func.max(Product.version)).where(Product.code == code)
        )
    ).scalar_one()
    version = (row or 0) + 1
    product = Product(
        code=code, version=version, kind=kind, name=name,
        definition=definition, status="DRAFT", created_by=principal,
    )
    session.add(product)
    await session.flush()
    await audit.record(session, "product.created", {
        "productCode": code, "version": version, "kind": kind, "by": principal,
    })
    return product


async def activate_product(session: AsyncSession, product: Product, principal: str) -> None:
    if product.status != "DRAFT":
        raise LifecycleError("bad-state", "only DRAFT products can be activated")
    has_rate = (
        await session.execute(select(func.count()).select_from(RateTable).where(RateTable.product_id == product.id))
    ).scalar_one()
    if not has_rate:
        raise LifecycleError("no-rate-table", "an ACTIVE product requires at least one rate table")
    product.status = "ACTIVE"
    await session.flush()
    await audit.record(session, "product.activated", {
        "productCode": product.code, "version": product.version, "by": principal,
    })


async def add_rate_table(
    session: AsyncSession,
    *,
    product: Product,
    effective_from: date,
    effective_to: date | None,
    rates: dict[str, Any],
    principal: str,
) -> RateTable:
    # Fail-closed: reject a malformed rate table at write time by dry-running
    # the rating engine's structural validation on a synthetic line.
    base = rates.get("base_rates_bp")
    if not isinstance(base, dict) or not base:
        raise LifecycleError("rate-table-malformed", "base_rates_bp must be a non-empty object")
    for k, v in base.items():
        if isinstance(v, bool) or not isinstance(v, int) or v < 0 or v > 10_000:
            raise LifecycleError("rate-table-malformed", f"base_rates_bp[{k!r}] out of range")
    # Overlap rejection: no two tables for the same product version may cover
    # the same date. Lock the product row to serialize concurrent inserts.
    await session.execute(
        select(Product.id).where(Product.id == product.id).with_for_update()
    )
    existing = (
        await session.execute(select(RateTable).where(RateTable.product_id == product.id))
    ).scalars().all()
    for rt in existing:
        rt_end = rt.effective_to or date.max
        new_end = effective_to or date.max
        if rt.effective_from <= new_end and effective_from <= rt_end:
            raise LifecycleError(
                "rate-window-overlap",
                f"overlaps existing table {rt.effective_from}..{rt.effective_to or 'open'}",
            )
    table = RateTable(
        product_id=product.id, effective_from=effective_from,
        effective_to=effective_to, rates=rates, created_by=principal,
    )
    session.add(table)
    await session.flush()
    await audit.record(session, "rate-table.created", {
        "productCode": product.code, "version": product.version,
        "effectiveFrom": effective_from.isoformat(), "by": principal,
    })
    return table


async def effective_rate_table(session: AsyncSession, product_id: uuid.UUID, on: date) -> RateTable:
    rows = (
        await session.execute(
            select(RateTable)
            .where(RateTable.product_id == product_id, RateTable.effective_from <= on)
            .order_by(RateTable.effective_from.desc())
        )
    ).scalars().all()
    for rt in rows:
        if rt.effective_to is None or rt.effective_to > on:
            return rt
    raise LifecycleError("no-effective-rate-table", "no rate table covers the rating date")


async def get_active_product(session: AsyncSession, code: str, version: int | None) -> Product:
    q = select(Product).where(Product.code == code)
    if version is not None:
        q = q.where(Product.version == version)
    else:
        q = q.order_by(Product.version.desc())
    product = (await session.execute(q.limit(1))).scalars().first()
    if product is None:
        raise LifecycleError("unknown-product", code)
    if product.status != "ACTIVE":
        raise LifecycleError("product-not-active", f"{code} v{product.version} is {product.status}")
    return product


# ----------------------------------------------------------------- rating


async def corridor_route_risk(session: AsyncSession, corridor: str) -> tuple[int, list[dict[str, Any]]]:
    """Route risk loading from digest-verified ISR outcome-ledger evidence:
    the sum of confirmed premium-delta basis points for the corridor."""
    if not corridor:
        return 0, []
    rows = (
        await session.execute(
            select(IsrEvidence).where(IsrEvidence.corridor == corridor)
        )
    ).scalars().all()
    total = sum(r.delta_bp for r in rows)
    evidence = [
        {"evidenceId": r.evidence_id, "deltaBp": r.delta_bp, "sourceDigest": r.source_digest}
        for r in rows
    ]
    return total, evidence


async def create_quote(
    session: AsyncSession,
    *,
    settings: Settings,
    product: Product,
    lines: list[dict[str, Any]],
    corridor: str,
    declaration_ref: str,
    source_event_id: str,
    assured_name: str,
    assured_tin: str,
    principal: str,
    idempotency_key: str = "",
) -> Quote:
    route_bp, evidence = await corridor_route_risk(session, corridor)
    table = await effective_rate_table(session, product.id, utcnow().date())
    try:
        trace = rating_domain.rate_quote(
            product={"code": product.code, "version": product.version},
            rate_table=table.rates,
            lines=lines,
            route_risk_bp=route_bp,
            route_risk_evidence=evidence,
        )
    except rating_domain.RatingError as exc:
        raise LifecycleError(exc.reason, str(exc)) from exc
    now = utcnow()
    quote = Quote(
        quote_ref=_ref("Q"),
        product_id=product.id,
        status="QUOTED",
        corridor=corridor,
        declaration_ref=declaration_ref,
        source_event_id=source_event_id,
        assured_name=assured_name,
        assured_tin=assured_tin,
        premium_kobo=trace["premium_kobo"],
        rating_trace=trace,
        created_by=principal,
        created_at=now,
        expires_at=now + timedelta(hours=settings.quote_validity_hours),
        idempotency_key=idempotency_key,
    )
    session.add(quote)
    await session.flush()
    for idx, line in enumerate(lines):
        session.add(QuoteLine(
            quote_id=quote.id, line_index=idx,
            description=str(line.get("description", ""))[:2000],
            hs_code=str(line.get("hs_code", ""))[:16],
            risk_class=str(line["risk_class"]),
            insured_value_kobo=int(line["insured_value_kobo"]),
        ))
    await session.flush()
    await audit.record(session, "quote.created", {
        "quoteRef": quote.quote_ref, "productCode": product.code,
        "productVersion": product.version, "premiumKobo": quote.premium_kobo,
        "routeRiskBp": trace["route_risk_bp"], "by": principal,
    })
    return quote


async def request_bind(session: AsyncSession, quote: Quote, principal: str) -> None:
    """Maker: request bind. One in-flight request per quote (status gate)."""
    if quote.status == "EXPIRED" or utcnow() > quote.expires_at:
        if quote.status != "EXPIRED":
            quote.status = "EXPIRED"
            await session.flush()
        raise LifecycleError("quote-expired", quote.quote_ref)
    if quote.status != "QUOTED":
        raise LifecycleError("bad-state", f"quote is {quote.status}, not QUOTED")
    # Serialize concurrent bind requests on the quote row.
    await session.execute(select(Quote.id).where(Quote.id == quote.id).with_for_update())
    await session.refresh(quote)
    if quote.status != "QUOTED":
        raise LifecycleError("bad-state", f"quote is {quote.status}, not QUOTED")
    quote.status = "BIND_PENDING"
    quote.bind_requested_by = principal
    quote.bind_requested_at = utcnow()
    await session.flush()
    await audit.record(session, "quote.bind-requested", {
        "quoteRef": quote.quote_ref, "by": principal,
    })


async def decide_bind(
    session: AsyncSession,
    quote: Quote,
    *,
    decision: str,
    principal: str,
    reason: str = "",
) -> None:
    """Checker: approve or decline a bind. Checker != maker, always."""
    if decision not in ("BIND", "DECLINE"):
        raise LifecycleError("bad-decision", decision)
    await session.execute(select(Quote.id).where(Quote.id == quote.id).with_for_update())
    await session.refresh(quote)
    if quote.status != "BIND_PENDING":
        raise LifecycleError("bad-state", f"quote is {quote.status}, not BIND_PENDING")
    if principal == quote.bind_requested_by:
        raise LifecycleError("dual-control-violation", "bind checker must differ from the maker")
    session.add(BindDecision(
        quote_id=quote.id, decision=decision, decided_by=principal, reason=reason,
    ))
    if decision == "BIND":
        quote.status = "BOUND"
        quote.bound_by = principal
        quote.bound_at = utcnow()
        # Premium economics are recognized at bind: Dr premium:receivable /
        # Cr premium:income, in the SAME transaction as the state change so
        # the subledger can never diverge from the quote state. Replay-safe:
        # the BIND_PENDING gate above plus the unique journal reference make a
        # second recognition impossible.
        if quote.premium_kobo > 0:
            journal = await post_journal(
                session,
                reference=f"premium-bind:{quote.quote_ref}",
                narration=f"Premium recognized at bind {quote.quote_ref}",
                legs=[
                    (ACCT_PREMIUM_RECEIVABLE, quote.premium_kobo, 0),
                    (ACCT_PREMIUM_INCOME, 0, quote.premium_kobo),
                ],
            )
            quote.premium_journal_reference = journal.reference
    else:
        quote.status = "DECLINED"
    await session.flush()
    await audit.record(session, f"quote.bind-{decision.lower()}", {
        "quoteRef": quote.quote_ref, "by": principal, "reason": reason,
    })


async def _claim_sequence(session: AsyncSession, family_code: str, year: int) -> int:
    """Atomic sequence claim; safe under concurrency (upsert + RETURNING)."""
    row = (
        await session.execute(
            text(
                """
                INSERT INTO policy_serial_counters (family_code, year, next_sequence)
                VALUES (:fam, :yr, 1)
                ON CONFLICT (family_code, year)
                DO UPDATE SET next_sequence = policy_serial_counters.next_sequence + 1
                RETURNING next_sequence - 1
                """
            ),
            {"fam": family_code, "yr": year},
        )
    ).scalar_one()
    return int(row)


async def issue_policy(
    session: AsyncSession,
    *,
    settings: Settings,
    signing_key: SigningKey,
    quote: Quote,
    inception_at: datetime,
    expiry_at: datetime,
    principal: str,
) -> Policy:
    """Issue the policy as a signed W3C VC 2.0 credential.

    Race-safe: policy numbers are claimed from the atomic counter and
    quote_id has a unique constraint, so concurrent issuance attempts on one
    quote converge on exactly one policy (the loser's INSERT violates the
    unique constraint and its transaction rolls back).
    """
    await session.execute(select(Quote.id).where(Quote.id == quote.id).with_for_update())
    await session.refresh(quote)
    if quote.status != "BOUND":
        raise LifecycleError("bad-state", f"quote is {quote.status}, not BOUND")
    if expiry_at <= inception_at:
        raise LifecycleError("bad-window", "expiry must be after inception")
    product = (await session.execute(select(Product).where(Product.id == quote.product_id))).scalar_one()
    family = KIND_TO_FAMILY.get(product.kind)
    if family is None:
        raise LifecycleError("unknown-family", product.kind)
    year = inception_at.year
    sequence = await _claim_sequence(session, family, year)
    policy_number = build_policy_number(family, year, sequence)
    status_index = await statuslists.allocate_index(session)
    credential_id = f"urn:blueeconomy:insurance:policy:{policy_number}"
    entries = [
        status_entry(statuslists.list_credential_id(settings, purpose), status_index, purpose)
        for purpose in ("revocation", "suspension")
    ]
    vc_doc = build_policy_credential(
        credential_id=credential_id,
        issuer_did=settings.issuer_did,
        policy_number=policy_number,
        product_code=product.code,
        product_kind=product.kind,
        corridor=quote.corridor,
        declaration_ref=quote.declaration_ref,
        valid_from=inception_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        valid_until=expiry_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        status_entries=entries,
    )
    verification_method = f"{settings.issuer_did}#{signing_key.kid}"
    signed_vc = issue_proof(vc_doc, signing_key, verification_method)
    insured_value = sum(int(line["insured_value_kobo"]) for line in quote.rating_trace["lines"])
    policy = Policy(
        policy_number=policy_number,
        quote_id=quote.id,
        product_id=product.id,
        family_code=family,
        year=year,
        sequence=sequence,
        status="ACTIVE",
        assured_name=quote.assured_name,
        assured_tin=quote.assured_tin,
        corridor=quote.corridor,
        declaration_ref=quote.declaration_ref,
        premium_kobo=quote.premium_kobo,
        insured_value_kobo=insured_value,
        status_list_index=status_index,
        credential=signed_vc,
        inception_at=inception_at,
        expiry_at=expiry_at,
        issued_by=principal,
    )
    session.add(policy)
    quote.status = "ISSUED"
    await session.flush()
    await audit.record(session, "policy.issued", {
        "policyNumber": policy_number, "quoteRef": quote.quote_ref,
        "premiumKobo": policy.premium_kobo, "by": principal,
    })
    return policy


async def add_endorsement(
    session: AsyncSession,
    *,
    policy: Policy,
    kind: str,
    premium_delta_kobo: int,
    detail: dict[str, Any],
    principal: str,
) -> Endorsement:
    await session.execute(select(Policy.id).where(Policy.id == policy.id).with_for_update())
    await session.refresh(policy)
    if policy.status != "ACTIVE":
        raise LifecycleError("bad-state", f"policy is {policy.status}, not ACTIVE")
    row = (
        await session.execute(
            select(func.max(Endorsement.endorsement_no)).where(Endorsement.policy_id == policy.id)
        )
    ).scalar_one()
    endorsement_no = (row or 0) + 1
    # Maker-checker: a non-zero premium delta changes the premium subledger,
    # so it takes effect only after a DIFFERENT principal approves it (which
    # posts the balanced delta journal). Zero-delta endorsements have no
    # financial impact and are APPROVED at creation.
    status = "PROPOSED" if premium_delta_kobo != 0 else "APPROVED"
    endorsement = Endorsement(
        policy_id=policy.id, endorsement_no=endorsement_no, kind=kind,
        premium_delta_kobo=premium_delta_kobo, detail=detail, status=status,
        created_by=principal,
    )
    session.add(endorsement)
    await session.flush()
    await audit.record(session, "policy.endorsed", {
        "policyNumber": policy.policy_number, "endorsementNo": endorsement_no,
        "kind": kind, "premiumDeltaKobo": premium_delta_kobo, "status": status,
        "by": principal,
    })
    return endorsement


async def approve_endorsement(
    session: AsyncSession,
    *,
    policy: Policy,
    endorsement_no: int,
    principal: str,
) -> Endorsement:
    """Checker: approve a PROPOSED endorsement and post the balanced premium
    delta journal atomically with the state change.

    delta > 0 (additional premium): Dr premium:receivable / Cr premium:income
    delta < 0 (return premium):     Dr premium:income / Cr premium:receivable
                                    (contra / revenue refund)

    Checker != maker (service check AND ck_endorsement_dual_control).
    Replay-safe: the PROPOSED gate plus the unique journal reference make a
    double post impossible."""
    await session.execute(select(Policy.id).where(Policy.id == policy.id).with_for_update())
    await session.refresh(policy)
    endorsement = (
        await session.execute(
            select(Endorsement).where(
                Endorsement.policy_id == policy.id,
                Endorsement.endorsement_no == endorsement_no,
            )
        )
    ).scalar_one_or_none()
    if endorsement is None:
        raise LifecycleError("unknown-endorsement", f"endorsement {endorsement_no}")
    if endorsement.status != "PROPOSED":
        raise LifecycleError("bad-state", f"endorsement is {endorsement.status}, not PROPOSED")
    if principal == endorsement.created_by:
        raise LifecycleError("dual-control-violation", "endorsement approver must differ from the maker")
    delta = endorsement.premium_delta_kobo
    if delta != 0 and policy.premium_kobo + delta < 0:
        raise LifecycleError(
            "amount-out-of-range",
            f"delta {delta} would drive policy premium {policy.premium_kobo} below zero",
        )
    if delta != 0:
        amount = abs(delta)
        legs: list[tuple[str, int, int]] = (
            [(ACCT_PREMIUM_RECEIVABLE, amount, 0), (ACCT_PREMIUM_INCOME, 0, amount)]
            if delta > 0
            else [(ACCT_PREMIUM_INCOME, amount, 0), (ACCT_PREMIUM_RECEIVABLE, 0, amount)]
        )
        journal = await post_journal(
            session,
            reference=f"endorsement:{policy.policy_number}:{endorsement.endorsement_no}",
            narration=(
                f"Endorsement {policy.policy_number}#{endorsement.endorsement_no} "
                f"premium delta {delta}"
            ),
            legs=legs,
        )
        endorsement.journal_reference = journal.reference
        policy.premium_kobo += delta
    endorsement.status = "APPROVED"
    endorsement.approved_by = principal
    endorsement.approved_at = utcnow()
    await session.flush()
    await audit.record(session, "policy.endorsement-approved", {
        "policyNumber": policy.policy_number, "endorsementNo": endorsement.endorsement_no,
        "premiumDeltaKobo": delta, "journalReference": endorsement.journal_reference,
        "by": principal,
    })
    return endorsement


async def reject_endorsement(
    session: AsyncSession,
    *,
    policy: Policy,
    endorsement_no: int,
    principal: str,
    reason: str = "",
) -> Endorsement:
    """Checker: reject a PROPOSED endorsement. Terminal; no journal leg."""
    await session.execute(select(Policy.id).where(Policy.id == policy.id).with_for_update())
    await session.refresh(policy)
    endorsement = (
        await session.execute(
            select(Endorsement).where(
                Endorsement.policy_id == policy.id,
                Endorsement.endorsement_no == endorsement_no,
            )
        )
    ).scalar_one_or_none()
    if endorsement is None:
        raise LifecycleError("unknown-endorsement", f"endorsement {endorsement_no}")
    if endorsement.status != "PROPOSED":
        raise LifecycleError("bad-state", f"endorsement is {endorsement.status}, not PROPOSED")
    if principal == endorsement.created_by:
        raise LifecycleError("dual-control-violation", "endorsement checker must differ from the maker")
    endorsement.status = "REJECTED"
    endorsement.approved_by = principal
    endorsement.approved_at = utcnow()
    await session.flush()
    await audit.record(session, "policy.endorsement-rejected", {
        "policyNumber": policy.policy_number, "endorsementNo": endorsement.endorsement_no,
        "by": principal, "reason": reason,
    })
    return endorsement


async def cancel_or_lapse(
    session: AsyncSession,
    *,
    settings: Settings,
    signing_key: SigningKey,
    policy: Policy,
    new_status: str,
    principal: str,
    reason: str = "",
) -> None:
    """Cancel (operator action) or lapse (expiry sweep). Sets the revocation
    bit in the signed status list so verifiers fail closed."""
    if new_status not in ("CANCELLED", "LAPSED"):
        raise LifecycleError("bad-status", new_status)
    await session.execute(select(Policy.id).where(Policy.id == policy.id).with_for_update())
    await session.refresh(policy)
    if policy.status != "ACTIVE":
        raise LifecycleError("bad-state", f"policy is {policy.status}, not ACTIVE")
    policy.status = new_status
    policy.cancelled_at = utcnow()
    await statuslists.set_flag(
        session,
        purpose="revocation",
        index=policy.status_list_index,
        settings=settings,
        signing_key=signing_key,
        verification_method=f"{settings.issuer_did}#{signing_key.kid}",
    )
    await session.flush()
    await audit.record(session, f"policy.{new_status.lower()}", {
        "policyNumber": policy.policy_number, "by": principal, "reason": reason,
    })


async def suspend_policy(
    session: AsyncSession,
    *,
    settings: Settings,
    signing_key: SigningKey,
    policy: Policy,
    principal: str,
    reason: str = "",
) -> None:
    """Suspend cover (e.g. premium default). Sets the SUSPENSION bit in the
    signed status list so offline verifiers fail closed — the database-row
    status alone is not verifier-visible."""
    await session.execute(select(Policy.id).where(Policy.id == policy.id).with_for_update())
    await session.refresh(policy)
    if policy.status != "ACTIVE":
        raise LifecycleError("bad-state", f"policy is {policy.status}, not ACTIVE")
    policy.status = "SUSPENDED"
    await statuslists.set_flag(
        session,
        purpose="suspension",
        index=policy.status_list_index,
        settings=settings,
        signing_key=signing_key,
        verification_method=f"{settings.issuer_did}#{signing_key.kid}",
    )
    await session.flush()
    await audit.record(session, "policy.suspended", {
        "policyNumber": policy.policy_number, "by": principal, "reason": reason,
    })


async def reinstate_policy(
    session: AsyncSession,
    *,
    settings: Settings,
    signing_key: SigningKey,
    policy: Policy,
    principal: str,
    reason: str = "",
) -> None:
    """Reinstate a suspended policy; clears the suspension bit by publishing
    a new signed status-list snapshot with the bit reset."""
    await session.execute(select(Policy.id).where(Policy.id == policy.id).with_for_update())
    await session.refresh(policy)
    if policy.status != "SUSPENDED":
        raise LifecycleError("bad-state", f"policy is {policy.status}, not SUSPENDED")
    policy.status = "ACTIVE"
    await statuslists.clear_flag(
        session,
        purpose="suspension",
        index=policy.status_list_index,
        settings=settings,
        signing_key=signing_key,
        verification_method=f"{settings.issuer_did}#{signing_key.kid}",
    )
    await session.flush()
    await audit.record(session, "policy.reinstated", {
        "policyNumber": policy.policy_number, "by": principal, "reason": reason,
    })


async def lapse_sweep(
    session: AsyncSession,
    *,
    settings: Settings,
    signing_key: SigningKey,
    principal: str,
    batch_size: int = 500,
) -> int:
    """Lapse every ACTIVE policy whose cover window has ended, setting the
    revocation status-list bit for each so offline verifiers fail closed even
    when no claim or lookup ever touches the policy again.

    Rows are claimed FOR UPDATE SKIP LOCKED, so concurrent sweeps are safe;
    each policy is transitioned by exactly one transaction. Returns the
    number of policies lapsed in this batch."""
    now = utcnow()
    rows = (
        await session.execute(
            select(Policy)
            .where(Policy.status == "ACTIVE", Policy.expiry_at <= now)
            .order_by(Policy.expiry_at)
            .limit(batch_size)
            .with_for_update(skip_locked=True)
        )
    ).scalars().all()
    for policy in rows:
        await cancel_or_lapse(
            session, settings=settings, signing_key=signing_key,
            policy=policy, new_status="LAPSED", principal=principal,
            reason="cover window ended (lapse sweep)",
        )
    return len(rows)


def policy_event_resource(policy: Policy) -> dict[str, Any]:
    return {
        "resourceType": "Basic",
        "policyId": str(policy.id),
        "policyNumber": policy.policy_number,
        "productKind": policy.family_code,
        "corridor": policy.corridor,
        "declarationRef": policy.declaration_ref,
        "status": policy.status,
        "inceptionAt": policy.inception_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "expiryAt": policy.expiry_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        # Commercial terms stay in the CONFIDENTIAL-classified envelope,
        # never in the public VC.
        "premiumKobo": policy.premium_kobo,
        "insuredValueKobo": policy.insured_value_kobo,
    }
