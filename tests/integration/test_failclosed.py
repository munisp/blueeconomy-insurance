"""Fail-closed boot tests: placeholder keys, bad policy dirs, malformed
adapter config and missing required settings must refuse to boot — there is
no fail-open path."""

from __future__ import annotations

import pytest

from insurance.config import get_settings

from .conftest import REPO_ROOT

pytestmark = pytest.mark.asyncio


async def _boot_expect_failure(monkeypatch, tmp_path, migrated_url, *, env: dict[str, str],
                             files: dict[str, tuple[bytes, int]] | None = None):
    from insurance.main import app

    monkeypatch.setenv("INSURANCE_DATABASE_URL", migrated_url)
    monkeypatch.setenv("INSURANCE_ISSUER_DID", "did:web:insurance.blueeconomy.gov.ng")
    monkeypatch.setenv("INSURANCE_POLICY_DIR", str(REPO_ROOT / "policies"))
    monkeypatch.delenv("INSURANCE_OIDC_JWKS_PATH", raising=False)
    monkeypatch.delenv("INSURANCE_OIDC_JWKS_URL", raising=False)
    monkeypatch.delenv("INSURANCE_OIDC_ISSUER", raising=False)
    monkeypatch.delenv("INSURANCE_KEY_DIRECTORY_PATH", raising=False)
    monkeypatch.delenv("INSURANCE_PARTNER_ADAPTERS_JSON", raising=False)
    for name, (content, mode) in (files or {}).items():
        p = tmp_path / name
        p.write_bytes(content)
        p.chmod(mode)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    get_settings.cache_clear()
    try:
        async with app.router.lifespan_context(app):
            pass
    finally:
        get_settings.cache_clear()


async def test_placeholder_key_refused(monkeypatch, tmp_path, migrated_url):
    from insurance.crypto.eddsa import JwsError

    key = tmp_path / "key.pem"
    key.write_bytes(b"-----BEGIN PRIVATE KEY-----\nPLACEHOLDER CHANGE_ME\n-----END PRIVATE KEY-----\n")
    key.chmod(0o600)
    with pytest.raises(JwsError) as exc:
        await _boot_expect_failure(
            monkeypatch, tmp_path, migrated_url,
            env={"INSURANCE_SIGNING_KEY_PATH": str(key)},
        )
    assert exc.value.reason == "placeholder-key"


async def test_world_readable_key_refused(monkeypatch, tmp_path, migrated_url):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import (
        Encoding,
        NoEncryption,
        PrivateFormat,
    )

    from insurance.crypto.eddsa import JwsError

    key = tmp_path / "key.pem"
    key.write_bytes(Ed25519PrivateKey.generate().private_bytes(
        Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()))
    key.chmod(0o644)  # group/world-readable: refused
    with pytest.raises(JwsError) as exc:
        await _boot_expect_failure(
            monkeypatch, tmp_path, migrated_url,
            env={"INSURANCE_SIGNING_KEY_PATH": str(key)},
        )
    assert exc.value.reason == "key-unavailable"


async def test_missing_policy_dir_refused(monkeypatch, tmp_path, migrated_url):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import (
        Encoding,
        NoEncryption,
        PrivateFormat,
    )

    from insurance.api.pbac import PolicyError

    key = tmp_path / "key.pem"
    key.write_bytes(Ed25519PrivateKey.generate().private_bytes(
        Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()))
    key.chmod(0o600)
    with pytest.raises(PolicyError):
        await _boot_expect_failure(
            monkeypatch, tmp_path, migrated_url,
            env={
                "INSURANCE_SIGNING_KEY_PATH": str(key),
                "INSURANCE_POLICY_DIR": str(tmp_path / "does-not-exist"),
            },
        )


async def test_malformed_partner_registry_refused(monkeypatch, tmp_path, migrated_url):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import (
        Encoding,
        NoEncryption,
        PrivateFormat,
    )

    key = tmp_path / "key.pem"
    key.write_bytes(Ed25519PrivateKey.generate().private_bytes(
        Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()))
    key.chmod(0o600)
    with pytest.raises(ValueError, match="base_url"):
        await _boot_expect_failure(
            monkeypatch, tmp_path, migrated_url,
            env={
                "INSURANCE_SIGNING_KEY_PATH": str(key),
                # http:// (not https) partner endpoint: boot-fatal.
                "INSURANCE_PARTNER_ADAPTERS_JSON": '{"acme": {"base_url": "http://insecure.example"}}',
            },
        )


async def test_missing_database_url_refused(monkeypatch, tmp_path, migrated_url):
    from insurance.main import app

    monkeypatch.delenv("INSURANCE_DATABASE_URL", raising=False)
    monkeypatch.setenv("INSURANCE_SIGNING_KEY_PATH", str(tmp_path / "k"))
    monkeypatch.setenv("INSURANCE_ISSUER_DID", "did:web:x")
    monkeypatch.setenv("INSURANCE_POLICY_DIR", str(REPO_ROOT / "policies"))
    get_settings.cache_clear()
    try:
        with pytest.raises(RuntimeError, match="INSURANCE_DATABASE_URL"):
            async with app.router.lifespan_context(app):
                pass
    finally:
        get_settings.cache_clear()


async def test_capabilities_honest_about_unconfigured(client):
    from .conftest import auth

    c, mint = client
    r = await c.get("/v1/capabilities", headers=auth(mint("aud-1", ["auditor"])))
    assert r.status_code == 200
    caps = {cap["capability"]: cap for cap in r.json()["capabilities"]}
    assert caps["database"]["available"] is True
    assert caps["signing.eddsa-jcs-2022"]["available"] is True
    assert caps["auth.oidc"]["available"] is True
    assert caps["kafka.outbox-publisher"]["available"] is False
    assert caps["isr.outcome-ledger-evidence"]["available"] is False
    assert caps["partner.gateway"]["available"] is False
    # Honest negatives: no ceding stubs, no payout rail fabrication.
    assert caps["reinsurance.ceding"]["available"] is False
    assert "not implemented" in caps["reinsurance.ceding"]["reason"]


async def test_capabilities_requires_authentication(client):
    """S2 regression: /v1/capabilities leaks env/infra detail and must not be
    anonymous. 401 without a token, 403 for a non-auditor, 200 for auditor."""
    from .conftest import auth

    c, mint = client
    r = await c.get("/v1/capabilities")
    assert r.status_code == 401
    r = await c.get("/v1/capabilities", headers=auth(mint("uw-1", ["underwriter"])))
    assert r.status_code == 403
    r = await c.get("/v1/capabilities", headers=auth(mint("aud-1", ["auditor"])))
    assert r.status_code == 200


async def test_security_headers_present(client):
    """S5 regression: every response carries the platform security headers."""
    c, _ = client
    for path in ("/healthz", "/v1/capabilities"):
        r = await c.get(path)
        assert r.headers["strict-transport-security"].startswith("max-age=")
        assert r.headers["x-content-type-options"] == "nosniff"
        assert r.headers["x-frame-options"] == "DENY"
        assert r.headers["referrer-policy"] == "no-referrer"
