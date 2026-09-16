"""JWKS refresh on unknown kid: key rotation must not take the service down.

An unknown ``kid`` triggers ONE rate-limited reload of the JWKS source
before the token is rejected; a bogus kid is still rejected after the
refresh, and a failed refresh keeps the last good key set.
"""

from __future__ import annotations

import base64
import json
import time

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from insurance.api.auth import AuthError, JwksKeyring, verify_bearer
from insurance.config import Settings


def _b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _jwk(kid: str, key: Ed25519PrivateKey) -> dict:
    x = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return {"kty": "OKP", "crv": "Ed25519", "kid": kid, "x": _b64u(x), "alg": "EdDSA"}


def _token(key: Ed25519PrivateKey, kid: str) -> str:
    header = _b64u(json.dumps({"alg": "EdDSA", "kid": kid, "typ": "JWT"}).encode())
    now = int(time.time())
    payload = _b64u(json.dumps({
        "iss": "https://keycloak.test/realms/blueeconomy",
        "sub": "user-1", "iat": now, "exp": now + 600,
        "realm_access": {"roles": ["underwriter"]},
    }).encode())
    signing_input = f"{header}.{payload}".encode("ascii")
    return f"{header}.{payload}.{_b64u(key.sign(signing_input))}"


def test_jwks_refresh_on_unknown_kid(tmp_path):
    key1 = Ed25519PrivateKey.generate()
    key2 = Ed25519PrivateKey.generate()  # the rotation key
    jwks_path = tmp_path / "jwks.json"
    jwks_path.write_text(json.dumps({"keys": [_jwk("kid-1", key1)]}))
    settings = Settings(
        oidc_jwks_path=str(jwks_path),
        oidc_issuer="https://keycloak.test/realms/blueeconomy",
    )
    keyring = JwksKeyring.load(settings)

    # The pre-rotation key works.
    identity = verify_bearer(_token(key1, "kid-1"), keyring, settings)
    assert identity.subject == "user-1"

    # A bogus kid is rejected even after the (rate-limited) refresh.
    with pytest.raises(AuthError, match="unknown-kid"):
        verify_bearer(_token(key2, "kid-bogus"), keyring, settings)

    # Rotation: the IdP publishes the new key; the next unknown-kid token
    # triggers a refresh and verifies without a restart.
    jwks_path.write_text(json.dumps({"keys": [_jwk("kid-1", key1), _jwk("kid-2", key2)]}))
    keyring._last_refresh = 0.0  # bypass the refresh rate limit for the test
    identity = verify_bearer(_token(key2, "kid-2"), keyring, settings)
    assert identity.subject == "user-1"
    # The old key still verifies (both keys served).
    assert verify_bearer(_token(key1, "kid-1"), keyring, settings).subject == "user-1"


def test_jwks_refresh_failure_keeps_last_good_set(tmp_path):
    key1 = Ed25519PrivateKey.generate()
    jwks_path = tmp_path / "jwks.json"
    jwks_path.write_text(json.dumps({"keys": [_jwk("kid-1", key1)]}))
    settings = Settings(oidc_jwks_path=str(jwks_path))
    keyring = JwksKeyring.load(settings)

    # Source becomes unreadable: refresh fails, keyring keeps serving.
    jwks_path.unlink()
    keyring._last_refresh = 0.0
    assert keyring.refresh() is False
    identity = verify_bearer(_token(key1, "kid-1"), keyring, settings)
    assert identity.subject == "user-1"

    # Rate limiting: a second refresh inside the window is a no-op.
    assert keyring.refresh() is False
