"""Request/response schemas. Strict models: unknown fields are rejected so
clients cannot smuggle in server-computed fields (e.g. premium totals)."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------- products


class ProductCreate(Strict):
    code: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,63}$")
    kind: Literal[
        "marine-cargo-single", "marine-cargo-open", "ferry-parametric",
        "protection-indemnity", "hull",
    ]
    name: str = Field(min_length=1, max_length=256)
    definition: dict[str, Any] = Field(default_factory=dict)


class RateTableCreate(Strict):
    effective_from: date
    effective_to: date | None = None
    rates: dict[str, Any]


# ------------------------------------------------------------------ quotes


class QuoteLineIn(Strict):
    description: str = Field(default="", max_length=2000)
    hs_code: str = Field(default="", max_length=16)
    risk_class: str = Field(min_length=1, max_length=64)
    insured_value_kobo: int = Field(gt=0, le=10**15)


class QuoteCreate(Strict):
    product_code: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,63}$")
    product_version: int | None = Field(default=None, ge=1)
    corridor: str = Field(default="", max_length=128)
    declaration_ref: str = Field(default="", max_length=64)
    assured_name: str = Field(default="", max_length=256)
    assured_tin: str = Field(default="", max_length=32)
    lines: list[QuoteLineIn] = Field(min_length=1, max_length=500)
    # Client-supplied totals are advisory-only and REJECTED on mismatch;
    # the premium is computed server-side from the rate table.
    expected_premium_kobo: int | None = Field(default=None, ge=0)


class DeclarationLineIn(Strict):
    """The platform declaration line shape consumed by the NTP VAS attach
    API (singlewindow declaration-time insurance offer)."""

    hs_code: str = Field(default="", max_length=16)
    description: str = Field(default="", max_length=2000)
    risk_class: str = Field(default="general-cargo", max_length=64)
    customs_value_kobo: int = Field(gt=0, le=10**15)


class FromDeclaration(Strict):
    declaration_ref: str = Field(min_length=1, max_length=64)
    source_event_id: str = Field(default="", max_length=128)
    product_code: str = Field(default="marine-cargo-single", max_length=64)
    corridor: str = Field(default="", max_length=128)
    consignee_name: str = Field(default="", max_length=256)
    consignee_tin: str = Field(default="", max_length=32)
    occurred_at: datetime
    lines: list[DeclarationLineIn] = Field(min_length=1, max_length=500)


class BindDecisionIn(Strict):
    decision: Literal["BIND", "DECLINE"]
    reason: str = Field(default="", max_length=2000)


class IssueIn(Strict):
    inception_at: datetime
    expiry_at: datetime


class EndorsementIn(Strict):
    kind: Literal["EXTENSION", "VALUE_CHANGE", "ASSURED_CHANGE", "CANCELLATION", "REINSTATEMENT"]
    premium_delta_kobo: int = 0
    detail: dict[str, Any] = Field(default_factory=dict)


class CancelIn(Strict):
    reason: str = Field(default="", max_length=2000)


class EndorsementRejectIn(Strict):
    reason: str = Field(default="", max_length=2000)


class SuspendIn(Strict):
    reason: str = Field(default="", max_length=2000)


class PremiumReceiptIn(Strict):
    external_reference: str = Field(min_length=1, max_length=128)
    policy_number: str = Field(min_length=1, max_length=40)
    amount_kobo: int = Field(ge=0)
    currency: str = Field(min_length=3, max_length=3)


# ------------------------------------------------------------------ claims


class FnolIn(Strict):
    policy_number: str = Field(min_length=1, max_length=40)
    loss_occurred_at: datetime
    loss_description: str = Field(default="", max_length=4000)
    claimed_kobo: int = Field(gt=0)
    trigger_event_id: str = Field(default="", max_length=128)


class DocumentIn(Strict):
    vault_ref: str = Field(min_length=1, max_length=256)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    description: str = Field(default="", max_length=256)


class AdjusterProposal(Strict):
    adjuster_sub: str = Field(min_length=1, max_length=256)


class SettlementProposal(Strict):
    settled_kobo: int = Field(gt=0)


class RejectIn(Strict):
    reason: str = Field(min_length=1, max_length=2000)


class PayoutReceiptIn(Strict):
    external_reference: str = Field(min_length=1, max_length=128)
    claim_ref: str = Field(min_length=1, max_length=40)
    amount_kobo: int = Field(ge=0)
    currency: str = Field(min_length=3, max_length=3)


# ------------------------------------------------------------ integrations


class IsrEvidenceIn(Strict):
    envelope: dict[str, Any]


class PartnerCallIn(Strict):
    operation: Literal["quote-cede-offer", "claim-notification", "policy-evidence-pack"]
    payload: dict[str, Any]
