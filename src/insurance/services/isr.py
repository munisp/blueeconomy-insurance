"""ISR outcome-ledger evidence ingestion (maritime-intelligence integration).

The engine consumes premium-delta evidence from the ISR outcome ledger
(db/migrations/0009_outcome_ledger.sql in blueeconomy-maritime-intelligence)
as envelope-v1.0 signed events. Ingestion is fail-closed on every axis:

- the envelope must verify against the mounted public-key directory
  (unknown-kid / invalid-signature / payload-mismatch all reject);
- the evidence payload must be digest-pinned: sha256(JCS(resource)) must be
  in INSURANCE_ISR_EVIDENCE_SOURCE_DIGESTS, so only operator-authorized
  evidence sources can move risk factors;
- replay is killed by (source_event_id, evidence_id) uniqueness;
- when the integration is not configured the route returns 503 and NOTHING
  is persisted (capabilities registry reports it unavailable).
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from insurance.config import Settings
from insurance.crypto.eddsa import KeyDirectory
from insurance.crypto.jcs import canonicalize_bytes
from insurance.events.envelope import EnvelopeError, verify_envelope
from insurance.models import IsrEvidence
from insurance.services import audit

# The maritime-intelligence outcome-ledger metric this engine consumes.
EXPECTED_METRIC = "premium-delta-basis-points"
EXPECTED_UNIT = "basis-points"
EXPECTED_ENTRY_KIND = "premium-delta"


class IsrError(ValueError):
    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason


def resource_digest(resource: dict[str, Any]) -> str:
    return hashlib.sha256(canonicalize_bytes(resource)).hexdigest()


async def ingest_evidence(
    session: AsyncSession,
    *,
    settings: Settings,
    directory: KeyDirectory,
    envelope: dict[str, Any],
    principal: str,
) -> IsrEvidence:
    """Verify, digest-pin and persist one ISR evidence envelope."""
    if not settings.isr_evidence_configured:
        raise IsrError("integration-unconfigured", "ISR evidence integration is not configured")
    try:
        resource = verify_envelope(envelope, directory)
    except EnvelopeError as exc:
        raise IsrError(exc.reason, str(exc)) from exc
    digest = resource_digest(resource)
    authorized = {d.strip() for d in settings.isr_evidence_source_digests.split(",") if d.strip()}
    if digest not in authorized:
        raise IsrError(
            "evidence-not-authorized",
            "evidence payload digest is not in the authorized source set",
        )
    # Structural binding to the outcome-ledger shape.
    if resource.get("entryKind") != EXPECTED_ENTRY_KIND:
        raise IsrError("evidence-shape", f"entryKind must be {EXPECTED_ENTRY_KIND!r}")
    if resource.get("metric") != EXPECTED_METRIC or resource.get("unit") != EXPECTED_UNIT:
        raise IsrError("evidence-shape", "metric/unit must be premium-delta-basis-points/basis-points")
    evidence_id = resource.get("entryId")
    corridor = resource.get("corridor")
    quantity = resource.get("quantity")
    if not isinstance(evidence_id, str) or not evidence_id:
        raise IsrError("evidence-shape", "entryId missing")
    if not isinstance(corridor, str) or not corridor:
        raise IsrError("evidence-shape", "corridor missing")
    if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity <= 0:
        raise IsrError("evidence-shape", "quantity must be a positive integer")
    occurred_raw = resource.get("occurredAt")
    try:
        occurred_at = datetime.strptime(str(occurred_raw), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError as exc:
        raise IsrError("evidence-shape", "occurredAt must be RFC 3339 UTC") from exc
    row = IsrEvidence(
        evidence_id=evidence_id,
        corridor=corridor,
        delta_bp=quantity,
        source_digest=digest,
        source_event_id=str(envelope["eventId"]),
        occurred_at=occurred_at,
        envelope=envelope,
    )
    session.add(row)
    await session.flush()
    await audit.record(session, "isr.evidence-ingested", {
        "evidenceId": evidence_id, "corridor": corridor, "deltaBp": quantity,
        "sourceDigest": digest, "by": principal,
    })
    return row
