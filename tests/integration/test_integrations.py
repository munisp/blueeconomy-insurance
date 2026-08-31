"""Declaration-time attach (NTP VAS), ISR evidence ingestion (digest-verified),
partner gateway fail-closed doctrine, regulator aggregator."""

from __future__ import annotations

import hashlib
import json

import pytest
from sqlalchemy import text

from insurance.crypto.eddsa import SigningKey
from insurance.crypto.jcs import canonicalize_bytes
from insurance.events.envelope import sign_envelope

from .conftest import auth, make_active_product

pytestmark = pytest.mark.asyncio


# ------------------------------------------------------- fromDeclaration


async def test_quote_from_declaration(client, session_factory):
    c, mint = client
    await make_active_product(c, mint)
    vas = auth(mint("sw-vas-1", ["singlewindow-vas"]))
    body = {
        "declaration_ref": "D-2026-100001",
        "source_event_id": "evt-decl-1",
        "product_code": "marine-cargo-single",
        "corridor": "lagos-onne",
        "consignee_name": "Eko Traders Ltd",
        "consignee_tin": "12345678-0001",
        "occurred_at": "2026-03-01T12:00:00Z",
        "lines": [{"hs_code": "1006.30", "description": "rice",
                   "risk_class": "general-cargo", "customs_value_kobo": 10_000_000}],
    }
    r = await c.post("/v1/quotes:fromDeclaration", json=body, headers=vas)
    assert r.status_code == 201, r.text
    quote = r.json()
    assert quote["declarationRef"] == "D-2026-100001"
    assert quote["premiumKobo"] == 200_000  # 150bp + fee, server-side

    # Replay of the same source_event_id returns the ORIGINAL quote (dedupe).
    r = await c.post("/v1/quotes:fromDeclaration", json=body, headers=vas)
    assert r.status_code == 201
    assert r.json()["quoteRef"] == quote["quoteRef"]
    async with session_factory() as s:
        n = (await s.execute(text(
            "SELECT count(*) FROM quotes WHERE declaration_ref='D-2026-100001'"
        ))).scalar_one()
    assert n == 1


# ---------------------------------------------------------- ISR evidence


def _evidence_envelope(feed_key: SigningKey, *, corridor="lagos-onne", delta_bp=200,
                       entry_id="entry-1", event_id="evt-isr-1"):
    resource = {
        "entryId": entry_id,
        "entryKind": "premium-delta",
        "incidentRef": "inc-1",
        "metric": "premium-delta-basis-points",
        "unit": "basis-points",
        "quantity": delta_bp,
        "corridor": corridor,
        "occurredAt": "2026-02-01T00:00:00Z",
    }
    # The ISR feed producer uses its own envelope event type; the insurance
    # engine verifies structure + signature + digest, not the event type.
    envelope = {
        "envelopeVersion": "1.0",
        "eventId": event_id,
        "eventType": "maritime.outcome.v1",
        "occurredAt": "2026-02-01T00:00:00Z",
        "producer": "blueeconomy-maritime-intelligence",
        "correlationId": event_id,
        "classification": "CONFIDENTIAL",
        "fhir": {
            "resourceType": "Bundle", "type": "message", "bundleId": "bdl-1",
            "entry": [{"fullUrl": "urn:uuid:1", "resource": resource}],
        },
        "provenance": {"principalId": "isr", "principalRole": "outcome-ledger",
                       "ledgerCommitHash": "", "signature": ""},
    }
    return sign_envelope(envelope, feed_key), resource


@pytest.fixture()
def isr_setup(client, monkeypatch, tmp_path):
    """Configure the app for ISR ingestion: key directory + authorized digest."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from insurance.config import get_settings

    c, mint = client
    feed_key = SigningKey(kid="maritime-intelligence-0", private_key=Ed25519PrivateKey.generate())
    envelope, resource = _evidence_envelope(feed_key)
    digest = hashlib.sha256(canonicalize_bytes(resource)).hexdigest()
    kd_path = tmp_path / "kd.json"
    kd_path.write_text(json.dumps({feed_key.kid: feed_key.public_key_b64u()}))
    monkeypatch.setenv("INSURANCE_KEY_DIRECTORY_PATH", str(kd_path))
    monkeypatch.setenv("INSURANCE_ISR_EVIDENCE_SOURCE_DIGESTS", digest)
    get_settings.cache_clear()
    yield c, mint, feed_key, envelope, digest
    get_settings.cache_clear()


async def test_isr_evidence_unconfigured_is_503(client):
    c, mint = client
    feed = auth(mint("isr-1", ["isr-evidence-feed"]))
    r = await c.post("/v1/integrations/isr/evidence", json={"envelope": {}}, headers=feed)
    assert r.status_code == 503
    assert r.json()["detail"]["reason"] == "integration-unconfigured"


async def test_isr_evidence_ingest_and_risk_effect(isr_setup):
    """Digest-pinned evidence ingests and raises the corridor route risk,
    which raises the next quote's premium deterministically."""
    c, mint, feed_key, envelope, digest = isr_setup
    # NOTE: settings were re-resolved after env change; the app lifespan
    # already read settings at client fixture time, so reload app state.
    from insurance.config import get_settings
    from insurance.crypto.eddsa import KeyDirectory as KD
    from insurance.main import app

    app.state.settings = get_settings()
    app.state.key_directory = KD.load(app.state.settings.key_directory_path)

    await make_active_product(c, mint)
    feed = auth(mint("isr-1", ["isr-evidence-feed"]))
    r = await c.post("/v1/integrations/isr/evidence", json={"envelope": envelope}, headers=feed)
    assert r.status_code == 201, r.text
    assert r.json()["deltaBp"] == 200

    # New quote on the corridor now prices 150 + 200 = 350 bp.
    uw = auth(mint("uw-1", ["underwriter"]))
    r = await c.post("/v1/quotes", json={
        "product_code": "marine-cargo-single", "corridor": "lagos-onne",
        "lines": [{"risk_class": "general-cargo", "insured_value_kobo": 10_000_000}],
    }, headers=uw)
    assert r.status_code == 201
    trace = r.json()["ratingTrace"]
    assert trace["route_risk_bp"] == 200
    assert trace["premium_kobo"] == 350_000 + 50_000
    assert trace["route_risk_evidence"][0]["sourceDigest"] == digest


async def test_isr_evidence_rejects_bad_signature_and_digest(isr_setup):
    c, mint, feed_key, envelope, digest = isr_setup
    from insurance.config import get_settings
    from insurance.crypto.eddsa import KeyDirectory as KD
    from insurance.main import app

    app.state.settings = get_settings()
    app.state.key_directory = KD.load(app.state.settings.key_directory_path)
    feed = auth(mint("isr-1", ["isr-evidence-feed"]))

    # Tampered resource -> payload-mismatch.
    bad = json.loads(json.dumps(envelope))
    bad["fhir"]["entry"][0]["resource"]["quantity"] = 9999
    r = await c.post("/v1/integrations/isr/evidence", json={"envelope": bad}, headers=feed)
    assert r.status_code == 400

    # Valid signature but a digest NOT in the authorized set.
    env2, _ = _evidence_envelope(feed_key, corridor="bonny-escravos",
                                 entry_id="entry-2", event_id="evt-isr-2")
    r = await c.post("/v1/integrations/isr/evidence", json={"envelope": env2}, headers=feed)
    assert r.status_code == 400
    assert r.json()["detail"]["reason"] == "evidence-not-authorized"


# ------------------------------------------------------- partner gateway


async def test_partner_call_unconfigured_fails_before_network(client, session_factory):
    c, mint = client
    gw = auth(mint("gw-1", ["partner-gateway"]))
    r = await c.post("/v1/partners/acme-marine:call", json={
        "operation": "claim-notification", "payload": {"claimRef": "C-1"},
    }, headers=gw)
    assert r.status_code == 503
    assert r.json()["detail"]["reason"] == "ADAPTER_UNCONFIGURED"
    # Nothing recorded: the refusal happened before any network I/O, and the
    # transaction was rolled back.
    async with session_factory() as s:
        n = (await s.execute(text("SELECT count(*) FROM gateway_calls"))).scalar_one()
    assert n == 0


# ------------------------------------------------------------ aggregator


async def test_aggregator_role_reads_classification_free_aggregates(client):
    c, mint = client
    agg = auth(mint("reg-1", ["insurer-aggregator"]))
    r = await c.get("/v1/aggregates/portfolio", headers=agg)
    assert r.status_code == 200, r.text
    body = r.json()
    assert "policiesByStatus" in body and "premiumTotalKobo" in body
    # No identifiers leak into aggregates.
    assert "assured" not in json.dumps(body).lower()
    # Aggregator cannot mutate.
    r = await c.post("/v1/products", json={
        "code": "x", "kind": "hull", "name": "X", "definition": {},
    }, headers=agg)
    assert r.status_code == 403
