"""Premium economics: premium journaled at bind, premium receipts settle the
receivable, endorsement deltas are maker-checkered + journaled, and the
suspension status-list bit tracks suspend/reinstate."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from insurance.crypto.statuslist import parse_status_list_credential

from .conftest import auth, make_active_product, make_policy, make_quote

pytestmark = pytest.mark.asyncio

PREMIUM = 200_000  # kobo; matches conftest.make_quote(premium_expected=...)


async def _legs(session_factory, reference: str) -> list[tuple[str, int, int]]:
    async with session_factory() as s:
        rows = (await s.execute(text(
            "SELECT account, debit_kobo, credit_kobo FROM ledger_entries le "
            "JOIN journals j ON j.id = le.journal_id WHERE j.reference = :r"
        ), {"r": reference})).all()
    return sorted((a, d, c) for a, d, c in rows)


async def _make_bound_quote(c, mint) -> dict:
    quote = await make_quote(c, mint, premium_expected=PREMIUM)
    ref = quote["quoteRef"]
    uw = auth(mint("uw-1", ["underwriter"]))
    ap = auth(mint("ap-1", ["underwriter-approver"]))
    r = await c.post(f"/v1/quotes/{ref}:bind", headers=uw)
    assert r.status_code == 200, r.text
    r = await c.post(f"/v1/quotes/{ref}:bind-decision", json={"decision": "BIND"}, headers=ap)
    assert r.status_code == 200, r.text
    return quote


async def test_premium_journaled_at_bind(client, session_factory):
    """Bind recognition: Dr premium:receivable / Cr premium:income, posted
    atomically with the BIND decision."""
    c, mint = client
    await make_active_product(c, mint)
    quote = await _make_bound_quote(c, mint)
    legs = await _legs(session_factory, f"premium-bind:{quote['quoteRef']}")
    assert legs == [
        ("premium:income", 0, PREMIUM),
        ("premium:receivable", PREMIUM, 0),
    ]


async def test_declined_bind_journals_nothing(client, session_factory):
    c, mint = client
    await make_active_product(c, mint)
    quote = await make_quote(c, mint, premium_expected=PREMIUM)
    ref = quote["quoteRef"]
    uw = auth(mint("uw-1", ["underwriter"]))
    ap = auth(mint("ap-1", ["underwriter-approver"]))
    r = await c.post(f"/v1/quotes/{ref}:bind", headers=uw)
    assert r.status_code == 200
    r = await c.post(f"/v1/quotes/{ref}:bind-decision", json={"decision": "DECLINE"}, headers=ap)
    assert r.status_code == 200
    assert await _legs(session_factory, f"premium-bind:{ref}") == []


async def test_premium_receipt_exact_match_or_quarantine(client, session_factory):
    c, mint = client
    await make_active_product(c, mint)
    policy = await make_policy(c, mint)
    number = policy["policyNumber"]
    fin = auth(mint("fin-1", ["finance-officer"]))

    # Wrong amount: quarantined, never applied.
    r = await c.post("/v1/policies:premium-receipt", json={
        "external_reference": "prem-tx-wrong", "policy_number": number,
        "amount_kobo": PREMIUM - 1, "currency": "NGN",
    }, headers=fin)
    assert r.status_code == 201 and r.json()["status"] == "QUARANTINED"

    # Unknown policy: quarantined.
    r = await c.post("/v1/policies:premium-receipt", json={
        "external_reference": "prem-tx-unknown", "policy_number": "CRG-2099-000001-X",
        "amount_kobo": PREMIUM, "currency": "NGN",
    }, headers=fin)
    assert r.status_code == 201 and r.json()["status"] == "QUARANTINED"

    # Exact match: applied, settlement leg posted, policy stamped paid.
    r = await c.post("/v1/policies:premium-receipt", json={
        "external_reference": "prem-tx-1", "policy_number": number,
        "amount_kobo": PREMIUM, "currency": "NGN",
    }, headers=fin)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "APPLIED"
    assert body["journalReference"] == f"premium-receipt:{number}"
    legs = await _legs(session_factory, f"premium-receipt:{number}")
    assert legs == [
        ("insurer:clearing", PREMIUM, 0),
        ("premium:receivable", 0, PREMIUM),
    ]
    async with session_factory() as s:
        paid_at = (await s.execute(text(
            "SELECT premium_paid_at FROM policies WHERE policy_number = :n"
        ), {"n": number})).scalar_one()
    assert paid_at is not None

    # Money arriving after settlement: quarantined, never double-applied.
    r = await c.post("/v1/policies:premium-receipt", json={
        "external_reference": "prem-tx-2", "policy_number": number,
        "amount_kobo": PREMIUM, "currency": "NGN",
    }, headers=fin)
    assert r.status_code == 201
    assert r.json()["status"] == "QUARANTINED"
    assert r.json()["quarantineReason"] == "premium already settled"


async def test_endorsement_delta_maker_checker_and_journal(client, session_factory):
    c, mint = client
    await make_active_product(c, mint)
    policy = await make_policy(c, mint)
    number = policy["policyNumber"]
    uw = auth(mint("uw-1", ["underwriter"]))
    ap = auth(mint("ap-1", ["underwriter-approver"]))

    # Maker proposes a +delta endorsement: PROPOSED, no journal yet.
    r = await c.post(f"/v1/policies/{number}/endorsements", json={
        "kind": "VALUE_CHANGE", "premium_delta_kobo": 50_000, "detail": {"why": "more cargo"},
    }, headers=uw)
    assert r.status_code == 201, r.text
    assert r.json()["status"] == "PROPOSED"
    assert r.json()["endorsementNo"] == 1
    assert await _legs(session_factory, f"endorsement:{number}:1") == []

    # PBAC: the underwriter role cannot decide endorsements.
    r = await c.post(f"/v1/policies/{number}/endorsements/1:approve", headers=uw)
    assert r.status_code == 403

    # Dual control: the SAME principal can never approve their own proposal,
    # even with the approver role (mint a token for uw-1 with both roles).
    uw_both = auth(mint("uw-1", ["underwriter", "underwriter-approver"]))
    r = await c.post(f"/v1/policies/{number}/endorsements/1:approve", headers=uw_both)
    assert r.status_code == 409
    assert r.json()["detail"]["reason"] == "dual-control-violation"

    # Checker approves: balanced additional-premium journal, policy premium
    # updated atomically.
    r = await c.post(f"/v1/policies/{number}/endorsements/1:approve", headers=ap)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "APPROVED"
    assert r.json()["journalReference"] == f"endorsement:{number}:1"
    legs = await _legs(session_factory, f"endorsement:{number}:1")
    assert legs == [
        ("premium:income", 0, 50_000),
        ("premium:receivable", 50_000, 0),
    ]
    r = await c.get(f"/v1/policies/{number}", headers=uw)
    assert r.json()["premiumKobo"] == PREMIUM + 50_000

    # Replay: an approved endorsement is terminal.
    r = await c.post(f"/v1/policies/{number}/endorsements/1:approve", headers=ap)
    assert r.status_code == 409

    # Negative delta: contra legs (Dr premium:income / Cr premium:receivable).
    r = await c.post(f"/v1/policies/{number}/endorsements", json={
        "kind": "VALUE_CHANGE", "premium_delta_kobo": -20_000, "detail": {"why": "less cargo"},
    }, headers=uw)
    assert r.status_code == 201 and r.json()["endorsementNo"] == 2
    r = await c.post(f"/v1/policies/{number}/endorsements/2:approve", headers=ap)
    assert r.status_code == 200, r.text
    legs = await _legs(session_factory, f"endorsement:{number}:2")
    assert legs == [
        ("premium:income", 20_000, 0),
        ("premium:receivable", 0, 20_000),
    ]

    # Zero delta: no financial impact, APPROVED at creation, no journal.
    r = await c.post(f"/v1/policies/{number}/endorsements", json={
        "kind": "ASSURED_CHANGE", "premium_delta_kobo": 0, "detail": {"name": "Eko Traders II"},
    }, headers=uw)
    assert r.status_code == 201 and r.json()["status"] == "APPROVED"
    assert await _legs(session_factory, f"endorsement:{number}:3") == []

    # Rejection is terminal and posts nothing.
    r = await c.post(f"/v1/policies/{number}/endorsements", json={
        "kind": "EXTENSION", "premium_delta_kobo": 10_000, "detail": {},
    }, headers=uw)
    assert r.status_code == 201 and r.json()["endorsementNo"] == 4
    r = await c.post(f"/v1/policies/{number}/endorsements/4:reject",
                     json={"reason": "unsupported"}, headers=ap)
    assert r.status_code == 200 and r.json()["status"] == "REJECTED"
    assert await _legs(session_factory, f"endorsement:{number}:4") == []
    r = await c.post(f"/v1/policies/{number}/endorsements/4:approve", headers=ap)
    assert r.status_code == 409


async def _suspension_bit(session_factory, number: str) -> tuple[int, bool]:
    async with session_factory() as s:
        index = (await s.execute(text(
            "SELECT status_list_index FROM policies WHERE policy_number = :n"
        ), {"n": number})).scalar_one()
        credential = (await s.execute(text(
            "SELECT credential FROM status_list_snapshots WHERE purpose = 'suspension' "
            "ORDER BY created_at DESC LIMIT 1"
        ))).scalar_one()
    _, status_list = parse_status_list_credential(credential)
    return index, status_list.get(index)


async def test_suspension_bit_set_and_cleared(client, session_factory):
    c, mint = client
    await make_active_product(c, mint)
    policy = await make_policy(c, mint)
    number = policy["policyNumber"]
    ap = auth(mint("ap-1", ["underwriter-approver"]))

    r = await c.post(f"/v1/policies/{number}:suspend",
                     json={"reason": "premium default"}, headers=ap)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "SUSPENDED"
    _, bit = await _suspension_bit(session_factory, number)
    assert bit is True

    # Suspending a non-ACTIVE policy fails closed.
    r = await c.post(f"/v1/policies/{number}:suspend", json={"reason": "again"}, headers=ap)
    assert r.status_code == 409

    r = await c.post(f"/v1/policies/{number}:reinstate",
                     json={"reason": "premium cured"}, headers=ap)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "ACTIVE"
    _, bit = await _suspension_bit(session_factory, number)
    assert bit is False


async def test_lapse_sweep_lapses_expired_policies(client, session_factory):
    c, mint = client
    await make_active_product(c, mint)
    quote = await _make_bound_quote(c, mint)
    ref = quote["quoteRef"]
    uw = auth(mint("uw-1", ["underwriter"]))
    ap = auth(mint("ap-1", ["underwriter-approver"]))
    # Issue with a cover window entirely in the past.
    r = await c.post(f"/v1/quotes/{ref}:issue", json={
        "inception_at": "2025-01-01T00:00:00Z", "expiry_at": "2026-01-01T00:00:00Z",
    }, headers=uw)
    assert r.status_code == 201, r.text
    number = r.json()["policyNumber"]

    r = await c.post("/v1/ops:lapse-sweep", headers=ap)
    assert r.status_code == 200, r.text
    assert r.json()["lapsed"] == 1
    r = await c.get(f"/v1/policies/{number}", headers=uw)
    assert r.json()["status"] == "LAPSED"

    # Idempotent: a second sweep finds nothing to do.
    r = await c.post("/v1/ops:lapse-sweep", headers=ap)
    assert r.status_code == 200 and r.json()["lapsed"] == 0

    # The revocation bit is set for the lapsed policy.
    async with session_factory() as s:
        index = (await s.execute(text(
            "SELECT status_list_index FROM policies WHERE policy_number = :n"
        ), {"n": number})).scalar_one()
        credential = (await s.execute(text(
            "SELECT credential FROM status_list_snapshots WHERE purpose = 'revocation' "
            "ORDER BY created_at DESC LIMIT 1"
        ))).scalar_one()
    _, status_list = parse_status_list_credential(credential)
    assert status_list.get(index) is True
