"""Premium receipts: the settlement leg of the premium economics.

The premium is recognized at bind (Dr premium:receivable / Cr premium:income,
lifecycle.decide_bind); when the payment arrives, an external receipt is
recorded here and must match the policy premium EXACTLY (amount + NGN) or it
is QUARANTINED — never silently applied. An applied receipt posts the
balanced settlement leg (Dr insurer:clearing / Cr premium:receivable) and
stamps ``policy.premium_paid_at`` in the same transaction. The unique
external_reference makes replay a hard conflict, never a double post.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from insurance.models import Policy, PremiumReceipt, utcnow
from insurance.services import audit
from insurance.services.journal import (
    ACCT_INSURER_CLEARING,
    ACCT_PREMIUM_RECEIVABLE,
    post_journal,
)


class PremiumError(ValueError):
    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason


async def record_premium_receipt(
    session: AsyncSession,
    *,
    external_reference: str,
    policy_number: str,
    amount_kobo: int,
    currency: str,
    principal: str,
) -> PremiumReceipt:
    """Record a premium payment receipt. Exact amount + currency match
    against the policy premium is REQUIRED; anything else is quarantined."""
    policy = (
        await session.execute(select(Policy).where(Policy.policy_number == policy_number))
    ).scalar_one_or_none()
    if policy is None:
        receipt = PremiumReceipt(
            policy_id=None, external_reference=external_reference,
            amount_kobo=amount_kobo, currency=currency,
            status="QUARANTINED", quarantine_reason="unknown policy_number",
        )
        session.add(receipt)
        await session.flush()
        await audit.record(session, "policy.premium-quarantined", {
            "policyNumber": policy_number, "externalReference": external_reference,
            "reason": "unknown policy_number", "by": principal,
        })
        return receipt
    await session.execute(select(Policy.id).where(Policy.id == policy.id).with_for_update())
    await session.refresh(policy)
    if policy.status not in ("ACTIVE", "SUSPENDED"):
        raise PremiumError("bad-state", f"policy is {policy.status}")
    if policy.premium_paid_at is not None:
        # Fail closed: money arrived for an already-settled premium. It is
        # real money that cannot be matched, so quarantine — never apply.
        receipt = PremiumReceipt(
            policy_id=policy.id, external_reference=external_reference,
            amount_kobo=amount_kobo, currency=currency,
            status="QUARANTINED", quarantine_reason="premium already settled",
        )
        session.add(receipt)
        await session.flush()
        await audit.record(session, "policy.premium-quarantined", {
            "policyNumber": policy_number, "externalReference": external_reference,
            "reason": "premium already settled", "by": principal,
        })
        return receipt
    if currency != "NGN" or amount_kobo != policy.premium_kobo:
        receipt = PremiumReceipt(
            policy_id=policy.id, external_reference=external_reference,
            amount_kobo=amount_kobo, currency=currency,
            status="QUARANTINED", quarantine_reason="amount/currency mismatch with policy premium",
        )
        session.add(receipt)
        await session.flush()
        await audit.record(session, "policy.premium-quarantined", {
            "policyNumber": policy_number, "externalReference": external_reference,
            "reason": "amount/currency mismatch", "by": principal,
        })
        return receipt
    receipt = PremiumReceipt(
        policy_id=policy.id, external_reference=external_reference,
        amount_kobo=amount_kobo, currency=currency, status="APPLIED",
    )
    if amount_kobo > 0:
        journal = await post_journal(
            session,
            reference=f"premium-receipt:{policy_number}",
            narration=f"Premium received {policy_number}",
            legs=[
                (ACCT_INSURER_CLEARING, amount_kobo, 0),
                (ACCT_PREMIUM_RECEIVABLE, 0, amount_kobo),
            ],
        )
        receipt.journal_reference = journal.reference
    session.add(receipt)
    policy.premium_paid_at = utcnow()
    await session.flush()
    await audit.record(session, "policy.premium-received", {
        "policyNumber": policy_number, "externalReference": external_reference,
        "amountKobo": amount_kobo, "journalReference": receipt.journal_reference,
        "by": principal,
    })
    return receipt
