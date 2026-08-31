"""Bitstring status list: W3C bit ordering, gzip multibase round-trip."""

import pytest

from insurance.crypto.statuslist import (
    PURPOSES,
    StatusList,
    StatusListError,
    build_status_list_credential,
    parse_status_list_credential,
)


def test_purposes_are_vc20():
    assert PURPOSES == ("revocation", "suspension")


def test_bit_ordering_msb_first():
    sl = StatusList(size_bits=16)
    sl.set(0, True)
    assert sl.raw_bytes()[0] == 0b1000_0000
    sl.set(7, True)
    assert sl.raw_bytes()[0] == 0b1000_0001
    sl.set(8, True)
    assert sl.raw_bytes()[1] == 0b1000_0000


def test_encode_decode_round_trip():
    sl = StatusList()
    for i in (0, 3, 511, 131071):
        sl.set(i, True)
    decoded = StatusList.decode(sl.encode())
    for i in (0, 1, 3, 511, 131071):
        assert decoded.get(i) == (i in (0, 3, 511, 131071))


def test_malformed_encoded_list_rejected():
    with pytest.raises(StatusListError):
        StatusList.decode("not-multibase")
    with pytest.raises(StatusListError):
        StatusList.decode("uAAAA=")  # padding prohibited


def test_credential_round_trip(signing_key):
    sl = StatusList()
    sl.set(42, True)
    cred = build_status_list_credential(
        list_credential_id="urn:test:list",
        issuer_did="did:web:insurance.blueeconomy.gov.ng",
        status_purpose="revocation",
        status_list=sl,
        key=signing_key,
        verification_method="did:web:insurance.blueeconomy.gov.ng#insurance-0",
    )
    assert cred["proof"]["cryptosuite"] == "eddsa-jcs-2022"
    purpose, decoded = parse_status_list_credential(cred)
    assert purpose == "revocation"
    assert decoded.get(42)
    assert not decoded.get(41)


def test_unknown_purpose_rejected():
    with pytest.raises(StatusListError):
        build_status_list_credential(
            list_credential_id="x", issuer_did="y", status_purpose="void",
            status_list=StatusList(size_bits=8),
        )
