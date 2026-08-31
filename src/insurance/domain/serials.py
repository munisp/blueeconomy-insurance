"""Insurance policy numbers with a Luhn mod-N check digit.

Format: ``NG-<KIND3>-<YYYY>-<SEQ10>-<CHECK>``  e.g. ``NG-CRG-2026-0000000042-7``.

- ``KIND3`` three-letter product-family code (CRG cargo, FRY ferry
  parametric, PRT protection & indemnity, HUL hull) — letters I and O never
  appear in family codes so every body character is inside the check-digit
  alphabet.
- ``YYYY`` issuance year.
- ``SEQ10`` zero-padded 10-digit sequence claimed atomically from the
  per-(family, year) serial counter.
- ``CHECK`` Luhn mod-34 check character over ``NG<KIND3><YYYY><SEQ10>``
  (body without separators) using a 34-symbol alphabet: digits 0-9 plus
  uppercase letters A-Z excluding I and O.

The check character gives typo rejection before any database lookup — the
same doctrine as tax-stamp serials.
"""

from __future__ import annotations

import re

__all__ = [
    "ALPHABET",
    "FAMILIES",
    "PolicyNumberError",
    "luhn_mod_n_check",
    "luhn_mod_n_validate",
    "build_policy_number",
    "parse_policy_number",
    "validate_policy_number",
    "PolicyNumberParts",
]

# 34 symbols: unambiguous digits and letters.
ALPHABET = "0123456789ABCDEFGHJKLMNPQRSTUVWXYZ"
assert len(ALPHABET) == 34

FAMILIES: dict[str, str] = {
    "marine-cargo": "CRG",
    "ferry-parametric": "FRY",
    "protection-indemnity": "PRT",
    "hull": "HUL",
}

_NUMBER_RE = re.compile(r"^NG-([A-Z]{3})-(\d{4})-(\d{10})-([0-9A-Z])$")

_INDEX = {ch: i for i, ch in enumerate(ALPHABET)}


class PolicyNumberError(ValueError):
    pass


def luhn_mod_n_check(body: str) -> str:
    """Return the check character for ``body`` under Luhn mod-34."""
    n = len(ALPHABET)
    factor = 2  # the rightmost body character is doubled first
    total = 0
    try:
        code_points = [_INDEX[ch] for ch in body]
    except KeyError as exc:
        raise PolicyNumberError(f"character {exc.args[0]!r} not in policy-number alphabet") from exc
    for cp in reversed(code_points):
        addend = factor * cp
        factor = 1 if factor == 2 else 2
        addend = (addend // n) + (addend % n)
        total += addend
    remainder = total % n
    check_index = (n - remainder) % n
    return ALPHABET[check_index]


def luhn_mod_n_validate(body: str, check: str) -> bool:
    """Validate ``check`` against ``body``."""
    if len(check) != 1 or check not in _INDEX:
        return False
    try:
        expected = luhn_mod_n_check(body)
    except PolicyNumberError:
        return False
    return expected == check


def build_policy_number(family_code: str, year: int, sequence: int) -> str:
    """Build a full policy number with check character.

    ``sequence`` must be in [0, 9999999999]; counters guarantee uniqueness
    per (family_code, year).
    """
    fam = family_code.upper()
    if not re.fullmatch(r"[A-Z]{3}", fam):
        raise PolicyNumberError(f"invalid family code {family_code!r}")
    if not (1900 <= year <= 9999):
        raise PolicyNumberError(f"invalid year {year}")
    if not (0 <= sequence <= 9_999_999_999):
        raise PolicyNumberError(f"sequence {sequence} out of range")
    body = f"NG{fam}{year:04d}{sequence:010d}"
    return f"NG-{fam}-{year:04d}-{sequence:010d}-{luhn_mod_n_check(body)}"


class PolicyNumberParts:
    __slots__ = ("family_code", "year", "sequence", "check", "number")

    def __init__(self, family_code: str, year: int, sequence: int, check: str, number: str) -> None:
        self.family_code = family_code
        self.year = year
        self.sequence = sequence
        self.check = check
        self.number = number

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"PolicyNumberParts({self.number!r})"


def parse_policy_number(number: str) -> PolicyNumberParts:
    """Parse and check-digit-validate a policy number. Raises on any defect.

    Callers must run this before any database lookup so malformed or
    mis-transcribed numbers never touch the store.
    """
    if not isinstance(number, str):
        raise PolicyNumberError("policy number must be a string")
    number = number.strip().upper()
    m = _NUMBER_RE.match(number)
    if not m:
        raise PolicyNumberError("policy number does not match NG-<KIND3>-<YYYY>-<SEQ10>-<CHECK>")
    fam, year_s, seq_s, check = m.groups()
    body = f"NG{fam}{year_s}{seq_s}"
    if not luhn_mod_n_validate(body, check):
        raise PolicyNumberError("policy number check digit mismatch")
    return PolicyNumberParts(fam, int(year_s), int(seq_s), check, number)


def validate_policy_number(number: str) -> bool:
    try:
        parse_policy_number(number)
        return True
    except PolicyNumberError:
        return False
