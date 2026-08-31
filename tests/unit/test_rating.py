"""Deterministic rating engine: integer-only, traced, fail-closed."""

import pytest

from insurance.domain.rating import RatingError, bp_mul, rate_quote

RATE_TABLE = {
    "currency": "NGN",
    "base_rates_bp": {"general-cargo": 150, "hazardous": 400},
    "default_base_bp": None,
    "policy_fee_kobo": 50_000,
    "max_route_risk_bp": 300,
    "loadings_bp": {"hazardous": 50},
}

PRODUCT = {"code": "marine-cargo-single", "version": 1}


def _lines(**over):
    line = {"description": "rice", "risk_class": "general-cargo", "insured_value_kobo": 10_000_000}
    line.update(over)
    return [line]


def test_bp_mul_half_up():
    assert bp_mul(10_000, 150) == 150
    assert bp_mul(1, 1) == 0
    assert bp_mul(3, 1) == 0  # 0.0003 -> 0 (half-up below .5)
    assert bp_mul(5_000, 1) == 1  # 0.5 -> 1 half-up
    with pytest.raises(RatingError):
        bp_mul(-1, 100)


def test_deterministic_and_traced():
    t1 = rate_quote(product=PRODUCT, rate_table=RATE_TABLE, lines=_lines(),
                    route_risk_bp=120, route_risk_evidence=[{"evidenceId": "e1"}])
    t2 = rate_quote(product=PRODUCT, rate_table=RATE_TABLE, lines=_lines(),
                    route_risk_bp=120, route_risk_evidence=[{"evidenceId": "e1"}])
    assert t1 == t2
    # 10_000_000 * (150 + 120) bp = 270_000, plus 50_000 fee
    assert t1["lines"][0]["premium_kobo"] == 270_000
    assert t1["subtotal_kobo"] == 270_000
    assert t1["premium_kobo"] == 320_000
    assert t1["route_risk_bp"] == 120
    assert t1["route_risk_evidence"] == [{"evidenceId": "e1"}]


def test_route_risk_capped():
    trace = rate_quote(product=PRODUCT, rate_table=RATE_TABLE, lines=_lines(),
                       route_risk_bp=10_000, route_risk_evidence=[])
    assert trace["route_risk_bp"] == 300  # capped by max_route_risk_bp


def test_hazardous_loading():
    trace = rate_quote(product=PRODUCT, rate_table=RATE_TABLE,
                       lines=_lines(risk_class="hazardous"),
                       route_risk_bp=0, route_risk_evidence=[])
    # (400 + 50) bp on 10_000_000 = 450_000 + 50_000 fee
    assert trace["premium_kobo"] == 500_000


def test_unknown_risk_class_rejected():
    with pytest.raises(RatingError) as exc:
        rate_quote(product=PRODUCT, rate_table=RATE_TABLE,
                   lines=_lines(risk_class="unobtanium"),
                   route_risk_bp=0, route_risk_evidence=[])
    assert exc.value.reason == "unrated-risk-class"


def test_floats_rejected():
    with pytest.raises(RatingError):
        rate_quote(product=PRODUCT, rate_table=RATE_TABLE,
                   lines=_lines(insured_value_kobo=1.5),  # type: ignore[arg-type]
                   route_risk_bp=0, route_risk_evidence=[])
    with pytest.raises(RatingError):
        rate_quote(product=PRODUCT, rate_table=RATE_TABLE, lines=_lines(),
                   route_risk_bp=1.5, route_risk_evidence=[])  # type: ignore[arg-type]


def test_no_lines_rejected():
    with pytest.raises(RatingError) as exc:
        rate_quote(product=PRODUCT, rate_table=RATE_TABLE, lines=[],
                   route_risk_bp=0, route_risk_evidence=[])
    assert exc.value.reason == "no-lines"


def test_non_ngn_rejected():
    bad = dict(RATE_TABLE, currency="USD")
    with pytest.raises(RatingError) as exc:
        rate_quote(product=PRODUCT, rate_table=bad, lines=_lines(),
                   route_risk_bp=0, route_risk_evidence=[])
    assert exc.value.reason == "unsupported-currency"
