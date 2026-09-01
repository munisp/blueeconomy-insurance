"""Transactional outbox. Domain services enqueue signed envelope-v1.0 events
in the SAME transaction as the state change; a separate publisher process
drains the outbox to Kafka (at-least-once, outbox id as the Kafka key).

When Kafka is unconfigured the messages remain PENDING — fail-closed, never
silently dropped — and the capabilities registry reports the publisher as
unavailable.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from insurance.config import PRODUCER, Settings
from insurance.crypto.eddsa import SigningKey
from insurance.events.envelope import build_envelope, sign_envelope
from insurance.models import OutboxMessage

TOPIC_BY_EVENT = {
    "insurance.policy.v1": "insurance.policy",
    "insurance.policy-endorsed.v1": "insurance.policy",
    "insurance.policy-cancelled.v1": "insurance.policy",
    "insurance.policy-suspended.v1": "insurance.policy",
    "insurance.policy-reinstated.v1": "insurance.policy",
    "insurance.premium-received.v1": "insurance.policy",
    "insurance.claim.v1": "insurance.claim",
    "insurance.claim-paid.v1": "insurance.claim",
}

_CLASSIFICATION_BY_EVENT = {
    "insurance.policy.v1": "CONFIDENTIAL",
    "insurance.policy-endorsed.v1": "CONFIDENTIAL",
    "insurance.policy-cancelled.v1": "CONFIDENTIAL",
    "insurance.policy-suspended.v1": "CONFIDENTIAL",
    "insurance.policy-reinstated.v1": "CONFIDENTIAL",
    "insurance.premium-received.v1": "CONFIDENTIAL",
    "insurance.claim.v1": "CONFIDENTIAL",
    "insurance.claim-paid.v1": "CONFIDENTIAL",
}

async def enqueue(
    session: AsyncSession,
    *,
    event_type: str,
    resource: dict[str, Any],
    signing_key: SigningKey,
    principal_id: str,
    principal_role: str,
    correlation_id: str | None = None,
) -> OutboxMessage:
    """Build + sign an envelope and persist it in the outbox (caller's tx)."""
    topic = TOPIC_BY_EVENT[event_type]
    envelope = build_envelope(
        event_type=event_type,
        resource=resource,
        producer=PRODUCER,
        classification=_CLASSIFICATION_BY_EVENT[event_type],
        principal_id=principal_id,
        principal_role=principal_role,
        correlation_id=correlation_id,
    )
    signed = sign_envelope(envelope, signing_key)
    msg = OutboxMessage(
        id=uuid.uuid4(),
        event_id=str(signed["eventId"]),
        topic=topic,
        event_type=event_type,
        classification=_CLASSIFICATION_BY_EVENT[event_type],
        envelope=signed,
    )
    session.add(msg)
    await session.flush()
    return msg


def publisher_available(settings: Settings) -> tuple[bool, str]:
    if not settings.kafka_configured:
        return False, "INSURANCE_KAFKA_BOOTSTRAP_SERVERS not configured"
    return True, ""
