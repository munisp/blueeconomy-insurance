"""VC 2.0 eddsa-jcs-2022 round-trip, tamper detection, base58btc."""

import copy

import pytest

from insurance.crypto.vc import (
    VCError,
    base58btc_decode,
    base58btc_encode,
    build_policy_credential,
    issue_proof,
    verify_proof,
)


def _doc():
    return build_policy_credential(
        credential_id="urn:blueeconomy:insurance:policy:NG-CRG-2026-0000000001-A",
        issuer_did="did:web:insurance.blueeconomy.gov.ng",
        policy_number="NG-CRG-2026-0000000001-A",
        product_code="marine-cargo-single",
        product_kind="marine-cargo-single",
        corridor="lagos-onne",
        declaration_ref="D-2026-000001",
        valid_from="2026-07-01T00:00:00Z",
        valid_until="2027-07-01T00:00:00Z",
        status_entries=[],
    )


def test_sign_verify_round_trip(signing_key):
    signed = issue_proof(_doc(), signing_key, "did:web:insurance.blueeconomy.gov.ng#ed25519-blueeconomy-tax-stamps-0")
    assert signed["proof"]["cryptosuite"] == "eddsa-jcs-2022"
    assert signed["proof"]["proofValue"].startswith("z")
    verify_proof(signed, signing_key.public_key)  # no raise


def test_vc_context_and_type(signing_key):
    signed = issue_proof(_doc(), signing_key, "vm")
    assert signed["@context"] == ["https://www.w3.org/ns/credentials/v2"]
    assert signed["type"] == ["VerifiableCredential", "MarineInsurancePolicy"]
    subject = signed["credentialSubject"]
    assert subject["policyNumber"] == "NG-CRG-2026-0000000001-A"
    assert subject["productKind"] == "marine-cargo-single"
    assert subject["corridor"] == "lagos-onne"
    assert subject["declarationRef"] == "D-2026-000001"


def test_policy_credential_carries_no_commercial_terms(signing_key):
    """A presented policy credential must never disclose commercial terms."""
    signed = issue_proof(_doc(), signing_key, "vm")
    subject = signed["credentialSubject"]
    for forbidden in ("premiumKobo", "insuredValueKobo", "premium",
                      "assuredTin", "assuredName"):
        assert forbidden not in subject
        assert forbidden not in signed


def test_tampered_subject_rejected(signing_key):
    signed = issue_proof(_doc(), signing_key, "vm")
    tampered = copy.deepcopy(signed)
    tampered["credentialSubject"]["dutyPaidKobo"] = 1
    with pytest.raises(VCError, match="invalid-proof"):
        verify_proof(tampered, signing_key.public_key)


def test_tampered_proof_created_rejected(signing_key):
    signed = issue_proof(_doc(), signing_key, "vm")
    tampered = copy.deepcopy(signed)
    tampered["proof"]["created"] = "2020-01-01T00:00:00Z"
    with pytest.raises(VCError, match="invalid-proof"):
        verify_proof(tampered, signing_key.public_key)


def test_wrong_key_rejected(signing_key):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    signed = issue_proof(_doc(), signing_key, "vm")
    with pytest.raises(VCError, match="invalid-proof"):
        verify_proof(signed, Ed25519PrivateKey.generate().public_key())


def test_missing_and_malformed_proof(signing_key):
    with pytest.raises(VCError, match="missing-proof"):
        verify_proof(_doc(), signing_key.public_key)
    signed = issue_proof(_doc(), signing_key, "vm")
    bad = copy.deepcopy(signed)
    bad["proof"]["cryptosuite"] = "eddsa-rdfc-2022"
    with pytest.raises(VCError, match="unsupported-cryptosuite"):
        verify_proof(bad, signing_key.public_key)
    bad2 = copy.deepcopy(signed)
    bad2["proof"]["proofValue"] = "x" + signed["proof"]["proofValue"][1:]
    with pytest.raises(VCError, match="malformed-proof"):
        verify_proof(bad2, signing_key.public_key)


def test_double_sign_rejected(signing_key):
    signed = issue_proof(_doc(), signing_key, "vm")
    with pytest.raises(VCError, match="already-signed"):
        issue_proof(signed, signing_key, "vm")


def test_base58btc_round_trip():
    for raw in [b"", b"\x00", b"\x00\x00\x01", bytes(range(64)), b"hello world"]:
        assert base58btc_decode(base58btc_encode(raw)) == raw


def test_base58btc_known_vector():
    assert base58btc_encode(b"hello world") == "StV1DL6CwTryKyV"
    assert base58btc_decode("StV1DL6CwTryKyV") == b"hello world"


