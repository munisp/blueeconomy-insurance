"""Claims: FNOL -> documents -> adjuster maker-checker -> settlement
maker-checker with a DB-balanced double-entry journal -> payout receipt."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from .conftest import auth, make_active_product, make_policy

pytestmark = pytest.mark.asyncio


async def _fnol(c, mint, policy_number: str, **over) -> dict:
    adj = auth(mint("adj-1", ["claims-adjuster"]))
    body = {
        "policy_number": policy_number,
        "loss_occurred_at": "2026-06-01T00:00:00Z",
        "loss_description": "container overboard",
        "claimed_kobo": 5_000_000,
    }
    body.update(over)
    r = await c.post("/v1/claims", json=body, headers=adj)
    assert r.status_code == 201, r.text
    return r.json()


async def test_full_claim_journey(client, session_factory):
    c, mint = client
    await make_active_product(c, mint)
    policy = await make_policy(c, mint)
    claim = await _fnol(c, mint, policy["policyNumber"])
    ref = claim["claimRef"]
    adj = auth(mint("adj-1", ["claims-adjuster"]))
    ap = auth(mint("cap-1", ["claims-approver"]))
    fin = auth(mint("fin-1", ["finance-officer"]))

    # Documents (vault refs + digests only).
    r = await c.post(f"/v1/claims/{ref}/documents", json={
        "vault_ref": "vault://claims/doc-1", "sha256": "a" * 64,
        "description": "survey report",
    }, headers=adj)
    assert r.status_code == 201

    # Adjuster assignment maker-checker: self-confirm rejected.
    r = await c.post(f"/v1/claims/{ref}:propose-adjuster",
                     json={"adjuster_sub": "adj-1"}, headers=adj)
    assert r.status_code == 200 and r.json()["status"] == "ADJUSTER_PENDING"
    r = await c.post(f"/v1/claims/{ref}:confirm-adjuster", headers=adj)
    assert r.status_code == 403  # PBAC: claims-adjuster cannot confirm
    adj2 = auth(mint("adj-9", ["claims-adjuster"]))
    # Even with the right role, same-principal confirm hits dual control.
    r = await c.post(f"/v1/claims/{ref}:confirm-adjuster", headers=ap)
    assert r.status_code == 200 and r.json()["status"] == "ADJUSTER_ASSIGNED"

    # Settlement: only the assigned adjuster proposes.
    r = await c.post(f"/v1/claims/{ref}:propose-settlement",
                     json={"settled_kobo": 4_000_000}, headers=adj2)
    assert r.status_code == 409
    assert r.json()["detail"]["reason"] == "not-assigned-adjuster"
    r = await c.post(f"/v1/claims/{ref}:propose-settlement",
                     json={"settled_kobo": 4_000_000}, headers=adj)
    assert r.status_code == 200 and r.json()["status"] == "SETTLEMENT_PENDING"

    # Approver posts the balanced journal atomically with the state change.
    r = await c.post(f"/v1/claims/{ref}:approve-settlement", headers=ap)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "SETTLED"
    assert body["journalReference"] == f"settlement:{ref}"
    async with session_factory() as s:
        legs = (await s.execute(text(
            "SELECT account, debit_kobo, credit_kobo FROM ledger_entries le "
            "JOIN journals j ON j.id = le.journal_id WHERE j.reference = :r"
        ), {"r": f"settlement:{ref}"})).all()
    assert sorted((a, d, cr) for a, d, cr in legs) == [
        ("claims:payable", 4_000_000, 0), ("insurer:clearing", 0, 4_000_000),
    ]

    # Payout receipt: exact match applies; mismatched amounts quarantine.
    r = await c.post("/v1/claims:payout-receipt", json={
        "external_reference": "rail-tx-wrong", "claim_ref": ref,
        "amount_kobo": 3_999_999, "currency": "NGN",
    }, headers=fin)
    assert r.status_code == 201 and r.json()["status"] == "QUARANTINED"
    r = await c.post("/v1/claims:payout-receipt", json={
        "external_reference": "rail-tx-1", "claim_ref": ref,
        "amount_kobo": 4_000_000, "currency": "NGN",
    }, headers=fin)
    assert r.status_code == 201 and r.json()["status"] == "APPLIED"
    r = await c.get(f"/v1/claims/{ref}", headers=adj)
    assert r.json()["status"] == "PAID"

    # insurance.claim.v1 + insurance.claim-paid.v1 in the outbox.
    async with session_factory() as s:
        events = [r[0] for r in (await s.execute(text(
            "SELECT event_type FROM outbox_messages"
        ))).all()]
    assert "insurance.claim.v1" in events
    assert "insurance.claim-paid.v1" in events


async def test_claim_amount_and_window_guards(client):
    c, mint = client
    await make_active_product(c, mint)
    policy = await make_policy(c, mint)
    adj = auth(mint("adj-1", ["claims-adjuster"]))
    # Claim above insured value rejected.
    r = await c.post("/v1/claims", json={
        "policy_number": policy["policyNumber"],
        "loss_occurred_at": "2026-06-01T00:00:00Z",
        "claimed_kobo": 10_000_001,
    }, headers=adj)
    assert r.status_code == 400
    # Loss outside cover window rejected.
    r = await c.post("/v1/claims", json={
        "policy_number": policy["policyNumber"],
        "loss_occurred_at": "2028-06-01T00:00:00Z",
        "claimed_kobo": 1_000,
    }, headers=adj)
    assert r.status_code == 409
    assert r.json()["detail"]["reason"] == "loss-outside-cover"


async def test_unbalanced_journal_rejected_by_db(session):
    """The DB trigger is the actual invariant: an unbalanced journal cannot
    COMMIT even if application code were bypassed."""
    import uuid

    from sqlalchemy.exc import DBAPIError

    from insurance.models import Journal, LedgerEntry, utcnow

    j = Journal(id=uuid.uuid4(), reference="evil-1", narration="bypass attempt", posted_at=utcnow())
    session.add(j)
    await session.flush()
    session.add(LedgerEntry(id=uuid.uuid4(), journal_id=j.id, account="a",
                            debit_kobo=100, credit_kobo=0))
    session.add(LedgerEntry(id=uuid.uuid4(), journal_id=j.id, account="b",
                            debit_kobo=0, credit_kobo=99))
    with pytest.raises(DBAPIError):
        await session.commit()
    await session.rollback()


async def test_append_only_tables_reject_mutation(session):
    from sqlalchemy import text as sql_text
    from sqlalchemy.exc import DBAPIError

    from insurance.services import audit

    await audit.record(session, "test.event", {"k": 1})
    await session.commit()
    with pytest.raises(DBAPIError):
        await session.execute(sql_text("UPDATE audit_events SET payload = '{}'::jsonb"))
    await session.rollback()
