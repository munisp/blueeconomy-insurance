"""Integration fixtures: REAL PostgreSQL, no mocks.

Resolution order:
- INSURANCE_TEST_DATABASE_URL when set (CI service container);
- otherwise an embedded but real PostgreSQL via the ``pgserver`` dev package
  (bundled PostgreSQL binaries — a real server, not a stub) when installed;
- otherwise every integration test skips.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path

import pytest
import pytest_asyncio
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from insurance.config import get_settings
from insurance.crypto.eddsa import SigningKey, b64u_encode


def _resolve_database_url() -> str | None:
    url = os.environ.get("INSURANCE_TEST_DATABASE_URL")
    if url:
        return url
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR", "")
    if not runtime_dir or not os.access(runtime_dir, os.W_OK):
        runtime_dir = "/tmp/.pgserver-runtime"
        os.environ["XDG_RUNTIME_DIR"] = runtime_dir
    Path(runtime_dir).mkdir(parents=True, exist_ok=True)
    try:
        import pgserver  # type: ignore
    except ImportError:
        return None
    srv = pgserver.get_server("/tmp/.insurance-itest-pg")
    return srv.get_uri().replace("postgresql://", "postgresql+asyncpg://")


@pytest.fixture(scope="session")
def database_url() -> str:
    url = _resolve_database_url()
    if url is None:
        pytest.skip("no test PostgreSQL available (set INSURANCE_TEST_DATABASE_URL or install pgserver)")
    return url


@pytest.fixture(scope="session")
def migrated_url(database_url: str) -> str:
    """Apply Alembic migrations once per session against the real database."""
    import subprocess

    env = dict(os.environ, INSURANCE_DATABASE_URL=database_url)
    result = subprocess.run(
        ["python3", "-m", "alembic", "upgrade", "head"],
        capture_output=True, text=True, env=env,
        cwd=str(Path(__file__).resolve().parents[2]),
    )
    if result.returncode != 0:
        pytest.fail(f"alembic upgrade head failed:\n{result.stdout}\n{result.stderr}")
    return database_url


_TRUNCATE = """
    TRUNCATE payout_receipts, ledger_entries, journals, claim_documents, claims,
             endorsements, policies, policy_serial_counters, bind_decisions,
             quote_lines, quotes, isr_evidence, rate_tables, products,
             gateway_calls, idempotency_records, outbox_messages, audit_events,
             status_list_snapshots, processed_events, principals
    RESTART IDENTITY CASCADE
"""


@pytest_asyncio.fixture
async def session(migrated_url: str) -> AsyncSession:
    engine = create_async_engine(migrated_url)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as s:
        yield s
    async with engine.begin() as conn:
        await conn.execute(text(_TRUNCATE))
    await engine.dispose()


@pytest_asyncio.fixture
async def session_factory(migrated_url: str):
    engine = create_async_engine(migrated_url)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield factory
    async with engine.begin() as conn:
        await conn.execute(text(_TRUNCATE))
    await engine.dispose()


@pytest.fixture(scope="session")
def signing_key() -> SigningKey:
    return SigningKey(kid="insurance-0", private_key=Ed25519PrivateKey.generate())


REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture()
def oidc_env(tmp_path, monkeypatch, migrated_url):
    """Environment for the app: real DB, real signing key file, local JWKS."""
    oidc_key = Ed25519PrivateKey.generate()
    pub_raw = oidc_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    jwks = {"keys": [{
        "kty": "OKP", "crv": "Ed25519", "kid": "test-oidc-0",
        "x": b64u_encode(pub_raw), "alg": "EdDSA",
    }]}
    jwks_path = tmp_path / "jwks.json"
    jwks_path.write_text(json.dumps(jwks))
    signing_path = tmp_path / "signing.pem"
    signing_path.write_bytes(
        Ed25519PrivateKey.generate().private_bytes(
            Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()
        )
    )
    signing_path.chmod(0o600)
    monkeypatch.setenv("INSURANCE_DATABASE_URL", migrated_url)
    monkeypatch.setenv("INSURANCE_SIGNING_KEY_PATH", str(signing_path))
    monkeypatch.setenv("INSURANCE_ISSUER_DID", "did:web:insurance.blueeconomy.gov.ng")
    monkeypatch.setenv("INSURANCE_POLICY_DIR", str(REPO_ROOT / "policies"))
    monkeypatch.setenv("INSURANCE_OIDC_JWKS_PATH", str(jwks_path))
    monkeypatch.setenv("INSURANCE_OIDC_ISSUER", "https://keycloak.test/realms/blueeconomy")
    monkeypatch.delenv("INSURANCE_KAFKA_BOOTSTRAP_SERVERS", raising=False)
    monkeypatch.delenv("INSURANCE_KEY_DIRECTORY_PATH", raising=False)
    monkeypatch.delenv("INSURANCE_PARTNER_ADAPTERS_JSON", raising=False)
    monkeypatch.delenv("INSURANCE_ISR_EVIDENCE_SOURCE_DIGESTS", raising=False)
    get_settings.cache_clear()
    yield oidc_key
    get_settings.cache_clear()


def mint_token(oidc_key: Ed25519PrivateKey, sub: str, roles: list[str]) -> str:
    """Mint a valid EdDSA bearer token for the local JWKS."""
    header = b64u_encode(json.dumps({"alg": "EdDSA", "kid": "test-oidc-0", "typ": "JWT"}).encode())
    now = int(time.time())
    payload = b64u_encode(json.dumps({
        "iss": "https://keycloak.test/realms/blueeconomy",
        "sub": sub, "iat": now, "exp": now + 600,
        "realm_access": {"roles": roles},
        "tenant": "t1", "clearance": "standard",
    }).encode())
    signing_input = f"{header}.{payload}".encode("ascii")
    return f"{header}.{payload}.{b64u_encode(oidc_key.sign(signing_input))}"


@pytest_asyncio.fixture()
async def client(oidc_env, session_factory):
    """HTTP client against the real app with OIDC configured (local JWKS).

    Yields (client, mint) where ``mint(sub, roles)`` produces a valid EdDSA
    bearer token, so PBAC-protected routes are exercised end-to-end.
    """
    import httpx

    from insurance.main import app

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        async with app.router.lifespan_context(app):
            yield c, lambda sub, roles: mint_token(oidc_env, sub, roles)


# ------------------------------------------------------------ helpers


def auth(token: str) -> dict[str, str]:
    return {"authorization": f"Bearer {token}"}


RATE_TABLE = {
    "currency": "NGN",
    "base_rates_bp": {"general-cargo": 150},
    "policy_fee_kobo": 50_000,
    "max_route_risk_bp": 10_000,
    "loadings_bp": {},
}


async def make_active_product(client, mint, code="marine-cargo-single", kind="marine-cargo-single") -> None:
    uw = auth(mint("uw-1", ["underwriter"]))
    ap = auth(mint("ap-1", ["underwriter-approver"]))
    r = await client.post("/v1/products", json={
        "code": code, "kind": kind, "name": "Marine Cargo Single Transit",
        "definition": {"riskClasses": ["general-cargo"]},
    }, headers=uw)
    assert r.status_code == 201, r.text
    r = await client.post(f"/v1/products/{code}/versions/1/rate-tables", json={
        "effective_from": "2020-01-01", "rates": RATE_TABLE,
    }, headers=uw)
    assert r.status_code == 201, r.text
    r = await client.post(f"/v1/products/{code}/versions/1/activate", headers=ap)
    assert r.status_code == 200, r.text


async def make_quote(client, mint, premium_expected=None, corridor="lagos-onne") -> dict:
    uw = auth(mint("uw-1", ["underwriter"]))
    body = {
        "product_code": "marine-cargo-single",
        "corridor": corridor,
        "declaration_ref": f"D-{uuid.uuid4().hex[:8]}",
        "assured_name": "Eko Traders Ltd", "assured_tin": "12345678-0001",
        "lines": [{"description": "rice", "risk_class": "general-cargo",
                   "insured_value_kobo": 10_000_000}],
    }
    if premium_expected is not None:
        body["expected_premium_kobo"] = premium_expected
    r = await client.post("/v1/quotes", json=body, headers=uw)
    assert r.status_code == 201, r.text
    return r.json()


async def make_policy(client, mint) -> dict:
    """Full happy path: quote -> bind (maker) -> bind (checker) -> issue."""
    quote = await make_quote(client, mint, premium_expected=200_000)
    ref = quote["quoteRef"]
    uw = auth(mint("uw-1", ["underwriter"]))
    ap = auth(mint("ap-1", ["underwriter-approver"]))
    r = await client.post(f"/v1/quotes/{ref}:bind", headers=uw)
    assert r.status_code == 200, r.text
    r = await client.post(f"/v1/quotes/{ref}:bind-decision", json={"decision": "BIND"}, headers=ap)
    assert r.status_code == 200, r.text
    r = await client.post(f"/v1/quotes/{ref}:issue", json={
        "inception_at": "2026-01-01T00:00:00Z", "expiry_at": "2027-01-01T00:00:00Z",
    }, headers=uw)
    assert r.status_code == 201, r.text
    return r.json()
