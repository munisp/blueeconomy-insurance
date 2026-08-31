"""Policy number Luhn mod-34 check-digit properties."""

import pytest

from insurance.domain.serials import (
    PolicyNumberError,
    build_policy_number,
    luhn_mod_n_check,
    parse_policy_number,
    validate_policy_number,
)


def test_round_trip():
    number = build_policy_number("CRG", 2026, 42)
    parts = parse_policy_number(number)
    assert parts.family_code == "CRG"
    assert parts.year == 2026
    assert parts.sequence == 42
    assert parts.number == number


def test_format():
    number = build_policy_number("FRY", 2026, 1)
    assert number.startswith("NG-FRY-2026-0000000001-")
    assert len(number.split("-")[-1]) == 1


def test_check_digit_catches_single_char_typo():
    number = build_policy_number("PRT", 2026, 999)
    # Flip one body digit; the check digit must reject it.
    body = number.replace("0000000999", "0000000989")
    assert not validate_policy_number(body)


def test_check_digit_catches_transposition():
    number = build_policy_number("HUL", 2026, 1234567)
    bad = number.replace("1234567", "1234657")
    assert not validate_policy_number(bad)


def test_malformed_rejected():
    for bad in ("", "NG-CRG-2026-1-2", "NG-CRG-2026-1-A", "NG-CG-2026-0000000001-A",
                "NG-CRG-2026-0000000001", None, 123):
        assert not validate_policy_number(bad)  # type: ignore[arg-type]


def test_bounds():
    with pytest.raises(PolicyNumberError):
        build_policy_number("CRG", 2026, 10_000_000_000)
    with pytest.raises(PolicyNumberError):
        build_policy_number("cg!", 2026, 0)
    with pytest.raises(PolicyNumberError):
        build_policy_number("CRG", 1899, 0)


def test_luhn_alphabet_excludes_ambiguous():
    check = luhn_mod_n_check("NGCRG20260000000000")
    assert check in "0123456789ABCDEFGHJKLMNPQRSTUVWXYZ"
    assert check not in "IO"
