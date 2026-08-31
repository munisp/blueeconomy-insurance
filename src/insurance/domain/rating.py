"""Deterministic premium rating. Server-side only; integer arithmetic only.

All money is integer minor units (kobo: 1 NGN = 100 kobo). All rates and
risk factors are basis points (1 bp = 0.01%). No floats anywhere.

A rating run is a PURE function of (product rate table, quote lines, route
risk evidence) and produces a full, persisted trace so every premium is
re-derivable and auditable. Client-supplied premium totals are NEVER
trusted: the API recomputes and any client total mismatching the server
computation is rejected (see api.routes_quotes).
"""

from __future__ import annotations

from typing import Any

__all__ = ["RatingError", "rate_quote", "bp_mul"]

_BP_DENOM = 10_000


class RatingError(ValueError):
    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason


def bp_mul(amount_minor: int, basis_points: int) -> int:
    """amount * bp / 10000 with half-up rounding, integers only."""
    if amount_minor < 0:
        raise RatingError("negative-amount", "insured value cannot be negative")
    if basis_points < 0:
        raise RatingError("negative-rate", "rate cannot be negative")
    numerator = amount_minor * basis_points
    return int((numerator + _BP_DENOM // 2) // _BP_DENOM)


def _require_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise RatingError("malformed-input", f"{field} must be an integer")
    return int(value)


def rate_quote(
    *,
    product: dict[str, Any],
    rate_table: dict[str, Any],
    lines: list[dict[str, Any]],
    route_risk_bp: int,
    route_risk_evidence: list[dict[str, Any]],
) -> dict[str, Any]:
    """Rate one quote. Returns a trace dict:

    {
      "currency": "NGN",
      "lines": [ {index, insured_value_kobo, risk_class, base_bp,
                  route_risk_bp, loadings_bp, total_bp, premium_kobo} ],
      "subtotal_kobo": int,
      "policy_fee_kobo": int,
      "premium_kobo": int,   # subtotal + fee
      "route_risk_bp": int,
      "route_risk_evidence": [...],  # digest-pinned ISR evidence applied
    }

    ``product`` is the versioned product definition row payload;
    ``rate_table`` is the effective-dated rate table payload:

    {
      "currency": "NGN",
      "base_rates_bp": {"<risk_class>": bp, ...},
      "default_base_bp": bp,
      "policy_fee_kobo": int,
      "max_route_risk_bp": bp,      # cap on evidence-derived route loading
      "loadings_bp": {"<risk_class>": bp, ...}   # optional per-class loading
    }

    Fail-closed: unknown risk class without a default rate, values/rates out
    of range, or a non-integer anywhere aborts rating with a reason code.
    """
    currency = rate_table.get("currency")
    if currency != "NGN":
        raise RatingError("unsupported-currency", repr(currency))
    base_rates = rate_table.get("base_rates_bp")
    if not isinstance(base_rates, dict) or not base_rates:
        raise RatingError("rate-table-malformed", "base_rates_bp missing")
    default_base = rate_table.get("default_base_bp")
    loadings = rate_table.get("loadings_bp") or {}
    if not isinstance(loadings, dict):
        raise RatingError("rate-table-malformed", "loadings_bp must be an object")
    policy_fee = _require_int(rate_table.get("policy_fee_kobo", 0), "policy_fee_kobo")
    if policy_fee < 0:
        raise RatingError("rate-table-malformed", "policy_fee_kobo negative")
    max_route_bp = _require_int(rate_table.get("max_route_risk_bp", 10_000), "max_route_risk_bp")
    route_risk_bp = _require_int(route_risk_bp, "route_risk_bp")
    if route_risk_bp < 0:
        raise RatingError("route-risk-malformed", "route_risk_bp negative")
    applied_route_bp = min(route_risk_bp, max_route_bp)

    if not lines:
        raise RatingError("no-lines", "a quote requires at least one declaration line")

    trace_lines: list[dict[str, Any]] = []
    subtotal = 0
    for idx, line in enumerate(lines):
        value = _require_int(line.get("insured_value_kobo"), f"lines[{idx}].insured_value_kobo")
        if value <= 0:
            raise RatingError("nonpositive-value", f"lines[{idx}] insured value must be positive")
        if value > 10**15:
            raise RatingError("value-out-of-range", f"lines[{idx}] insured value absurd")
        risk_class = line.get("risk_class")
        if not isinstance(risk_class, str) or not risk_class:
            raise RatingError("malformed-input", f"lines[{idx}].risk_class required")
        base_bp = base_rates.get(risk_class, default_base)
        if base_bp is None:
            raise RatingError("unrated-risk-class", f"no rate for risk class {risk_class!r}")
        base_bp = _require_int(base_bp, f"base_rates_bp[{risk_class}]")
        loading_bp = _require_int(loadings.get(risk_class, 0), f"loadings_bp[{risk_class}]")
        total_bp = base_bp + applied_route_bp + loading_bp
        if total_bp > 10_000:
            raise RatingError("rate-out-of-range", f"lines[{idx}] total rate {total_bp}bp exceeds 100%")
        premium = bp_mul(value, total_bp)
        subtotal += premium
        trace_lines.append({
            "index": idx,
            "description": str(line.get("description", ""))[:256],
            "insured_value_kobo": value,
            "risk_class": risk_class,
            "base_bp": base_bp,
            "route_risk_bp": applied_route_bp,
            "loadings_bp": loading_bp,
            "total_bp": total_bp,
            "premium_kobo": premium,
        })

    return {
        "currency": "NGN",
        "product_code": str(product.get("code", "")),
        "product_version": int(product.get("version", 0)),
        "lines": trace_lines,
        "subtotal_kobo": subtotal,
        "policy_fee_kobo": policy_fee,
        "premium_kobo": subtotal + policy_fee,
        "route_risk_bp": applied_route_bp,
        "route_risk_evidence": route_risk_evidence,
    }
