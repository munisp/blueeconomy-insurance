"""Race tests: concurrent bind requests, concurrent issuance, concurrent
policy-number sequence claims — exactly one winner, no collisions, no forks."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import text

from .conftest import auth, make_active_product, make_quote

pytestmark = pytest.mark.asyncio


async def test_concurrent_bind_requests_single_winner(client, session_factory):
    c, mint = client
    await make_active_product(c, mint)
    quote = await make_quote(c, mint)
    ref = quote["quoteRef"]
    uw = auth(mint("uw-1", ["underwriter"]))

    results = await asyncio.gather(*[
        c.post(f"/v1/quotes/{ref}:bind", headers=uw) for _ in range(8)
    ])
    codes = sorted(r.status_code for r in results)
    # Exactly one transitions to BIND_PENDING; the rest see a state guard.
    assert codes.count(200) == 1
    assert all(code == 409 for code in codes if code != 200)
    async with session_factory() as s:
        status = (await s.execute(
            text("SELECT status FROM quotes WHERE quote_ref = :r"), {"r": ref}
        )).scalar_one()
    assert status == "BIND_PENDING"


async def test_concurrent_issue_single_policy(client, session_factory):
    c, mint = client
    await make_active_product(c, mint)
    quote = await make_quote(c, mint)
    ref = quote["quoteRef"]
    uw = auth(mint("uw-1", ["underwriter"]))
    ap = auth(mint("ap-1", ["underwriter-approver"]))
    await c.post(f"/v1/quotes/{ref}:bind", headers=uw)
    await c.post(f"/v1/quotes/{ref}:bind-decision", json={"decision": "BIND"}, headers=ap)

    body = {"inception_at": "2026-01-01T00:00:00Z", "expiry_at": "2027-01-01T00:00:00Z"}
    results = await asyncio.gather(*[
        c.post(f"/v1/quotes/{ref}:issue", json=body, headers=uw) for _ in range(6)
    ])
    codes = [r.status_code for r in results]
    # The row lock serializes issuance: the first commits, the rest observe
    # status != BOUND and fail closed. No unique-constraint violation can
    # ever produce two policies for one quote.
    assert codes.count(201) == 1
    async with session_factory() as s:
        n = (await s.execute(text(
            "SELECT count(*) FROM policies p JOIN quotes q ON q.id = p.quote_id "
            "WHERE q.quote_ref = :r"), {"r": ref})).scalar_one()
        numbers = [row[0] for row in (await s.execute(text("SELECT policy_number FROM policies"))).all()]
    assert n == 1
    assert len(numbers) == len(set(numbers))


async def test_concurrent_sequence_claims_unique_numbers(client, session_factory):
    """Independent quotes issued concurrently must draw distinct atomic
    sequence numbers (upsert + RETURNING; no lost updates)."""
    c, mint = client
    await make_active_product(c, mint)
    uw = auth(mint("uw-1", ["underwriter"]))
    ap = auth(mint("ap-1", ["underwriter-approver"]))

    async def issue_one():
        quote = await make_quote(c, mint)
        ref = quote["quoteRef"]
        await c.post(f"/v1/quotes/{ref}:bind", headers=uw)
        await c.post(f"/v1/quotes/{ref}:bind-decision", json={"decision": "BIND"}, headers=ap)
        r = await c.post(f"/v1/quotes/{ref}:issue", json={
            "inception_at": "2026-01-01T00:00:00Z", "expiry_at": "2027-01-01T00:00:00Z",
        }, headers=uw)
        assert r.status_code == 201, r.text
        return r.json()["policyNumber"]

    numbers = await asyncio.gather(*[issue_one() for _ in range(5)])
    assert len(set(numbers)) == 5
    async with session_factory() as s:
        seqs = sorted(row[0] for row in (await s.execute(
            text("SELECT sequence FROM policies ORDER BY sequence"))).all())
    assert seqs == list(range(5))
