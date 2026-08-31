"""Quote -> bind (maker-checker) -> issue (VC) -> endorse -> cancel lifecycle,
plus rating honesty (client totals rejected), status-list revocation,
outbox events and the audit chain."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from insurance.crypto.vc import verify_proof
from insurance.domain.serials import validate_policy_number

from .conftest import auth, make_active_product, make_policy, make_quote

pytestmark = pytest.mark.asyncio


async def test_full_lifecycle(client, session_factory):
    c, mint = client
    await make_active_product(c, mint)

    # 1. Client-supplied total MISMATCH is rejected; nothing persisted.
    uw = auth(mint("uw-1", ["underwriter"]))
    r = await c.post("/v1/quotes", json={
        "product_code": "marine-cargo-single", "corridor": "lagos-onne",
        "lines": [{"risk_class": "general-cargo", "insured_value_kobo": 10_000_000}],
        "expected_premium_kobo": 999,
    }, headers=uw)
    assert r.status_code == 422
    assert r.json()["detail"]["reason"] == "client-total-rejected"
    async with session_factory() as s:
        n = (await s.execute(text("SELECT count(*) FROM quotes"))).scalar_one()
    assert n == 0

    # 2. Server-side rating: 150bp on 10_000_000 = 150_000 + 50_000 fee.
    quote = await make_quote(c, mint, premium_expected=200_000)
    assert quote["premiumKobo"] == 200_000
    assert quote["ratingTrace"]["lines"][0]["total_bp"] == 150
    ref = quote["quoteRef"]

    # 3. Bind maker-checker: maker cannot check their own bind.
    r = await c.post(f"/v1/quotes/{ref}:bind", headers=uw)
    assert r.status_code == 200 and r.json()["status"] == "BIND_PENDING"
    # A principal holding BOTH roles still cannot check their own bind.
    both = auth(mint("uw-1", ["underwriter", "underwriter-approver"]))
    r = await c.post(f"/v1/quotes/{ref}:bind-decision", json={"decision": "BIND"}, headers=both)
    assert r.status_code == 409
    assert r.json()["detail"]["reason"] == "dual-control-violation"

    # 4. Distinct checker binds; issue produces a Luhn number + valid VC.
    ap = auth(mint("ap-1", ["underwriter-approver"]))
    r = await c.post(f"/v1/quotes/{ref}:bind-decision", json={"decision": "BIND"}, headers=ap)
    assert r.status_code == 200 and r.json()["status"] == "BOUND"
    r = await c.post(f"/v1/quotes/{ref}:issue", json={
        "inception_at": "2026-01-01T00:00:00Z", "expiry_at": "2027-01-01T00:00:00Z",
    }, headers=uw)
    assert r.status_code == 201, r.text
    policy = r.json()
    assert validate_policy_number(policy["policyNumber"])
    assert policy["premiumKobo"] == 200_000
    assert policy["insuredValueKobo"] == 10_000_000
    vc = policy["credential"]
    assert vc["type"] == ["VerifiableCredential", "MarineInsurancePolicy"]
    assert vc["proof"]["cryptosuite"] == "eddsa-jcs-2022"
    # Minimal disclosure: no commercial terms inside the credential.
    assert "premiumKobo" not in vc["credentialSubject"]

    # 5. insurance.policy.v1 event is in the transactional outbox.
    async with session_factory() as s:
        rows = (await s.execute(text(
            "SELECT event_type FROM outbox_messages WHERE event_type='insurance.policy.v1'"
        ))).all()
    assert len(rows) == 1

    # 6. Endorsement append-only history + event.
    number = policy["policyNumber"]
    r = await c.post(f"/v1/policies/{number}/endorsements", json={
        "kind": "VALUE_CHANGE", "premium_delta_kobo": 5_000,
        "detail": {"reason": "additional cargo"},
    }, headers=uw)
    assert r.status_code == 201 and r.json()["endorsementNo"] == 1
    r = await c.get(f"/v1/policies/{number}/endorsements", headers=uw)
    assert len(r.json()["endorsements"]) == 1

    # 7. Cancel sets the revocation bit; status list is a signed VC.
    r = await c.post(f"/v1/policies/{number}:cancel", json={"reason": "assured request"}, headers=ap)
    assert r.status_code == 200 and r.json()["status"] == "CANCELLED"
    r = await c.get("/v1/status-list/revocation")  # public, unauthenticated
    assert r.status_code == 200
    sl_vc = r.json()
    assert sl_vc["credentialSubject"]["statusPurpose"] == "revocation"

    # 8. Audit chain verifies end-to-end.
    auditor = auth(mint("audit-1", ["auditor"]))
    r = await c.get("/v1/audit/verify", headers=auditor)
    assert r.json()["ok"] is True
    assert r.json()["events"] > 0


async def test_luhn_gate_before_lookup(client):
    c, mint = client
    uw = auth(mint("uw-1", ["underwriter"]))
    r = await c.get("/v1/policies/NGI-CRG-2026-0000000001-0", headers=uw)
    assert r.status_code == 400
    assert r.json()["detail"]["reason"] == "malformed-policy-number"


async def test_pbac_denies_wrong_role(client):
    c, mint = client
    await make_active_product(c, mint)
    # auditor is read-only: product creation is denied by PBAC.
    auditor = auth(mint("audit-1", ["auditor"]))
    r = await c.post("/v1/products", json={
        "code": "x", "kind": "hull", "name": "X", "definition": {},
    }, headers=auditor)
    assert r.status_code == 403
    assert r.json()["detail"]["reason"] == "pbac-denied"
    # underwriter cannot decide binds (PBAC), even before dual control.
    quote = await make_quote(c, mint)
    uw = auth(mint("uw-1", ["underwriter"]))
    await c.post(f"/v1/quotes/{quote['quoteRef']}:bind", headers=uw)
    r = await c.post(f"/v1/quotes/{quote['quoteRef']}:bind-decision",
                     json={"decision": "BIND"}, headers=uw)
    assert r.status_code == 403


async def test_expired_quote_cannot_bind(client, session_factory):
    c, mint = client
    await make_active_product(c, mint)
    quote = await make_quote(c, mint)
    ref = quote["quoteRef"]
    # Force expiry in the database, then bind must fail closed.
    async with session_factory() as s:
        await s.execute(text("UPDATE quotes SET expires_at = created_at + interval '1 millisecond'"))
        await s.commit()
    uw = auth(mint("uw-1", ["underwriter"]))
    r = await c.post(f"/v1/quotes/{ref}:bind", headers=uw)
    assert r.status_code == 409
    assert r.json()["detail"]["reason"] == "quote-expired"


async def test_policy_vc_proof_verifies(client, signing_key_directory=None):
    """Issued VC verifies against the app's public key."""
    c, mint = client
    await make_active_product(c, mint)
    policy = await make_policy(c, mint)
    from insurance.main import app
    key = app.state.signing_key
    verify_proof(policy["credential"], key.public_key)  # no raise
