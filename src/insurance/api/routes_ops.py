"""Ops routes.

PUBLIC: the liveness probe (``/healthz``).
INTERNAL: ``/v1/capabilities`` and the audit-chain verifier leak DB/Kafka
internals and audit integrity state, so they are policy-gated behind
require_policy("ops", "read") / ("audit", "verify") — anonymous callers get
401 (or 503 when OIDC is unconfigured: fail-closed), authenticated callers
without the auditor role get 403.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from sqlalchemy import text

from insurance.api.deps import (
    IdentityDep,
    SessionDep,
    SettingsDep,
    get_signing_key,
    require_policy,
)
from insurance.services import audit, lifecycle
from insurance.services.capabilities import capability_report

router = APIRouter(tags=["ops"])


@router.get("/healthz")
async def healthz() -> dict[str, Any]:
    return {"status": "ok", "service": "blueeconomy-insurance"}


@router.get("/v1/capabilities")
async def capabilities(request: Request, settings: SettingsDep, session: SessionDep,
                       identity: IdentityDep) -> dict[str, Any]:
    require_policy(request, identity, "ops", "read", "INTERNAL")
    runtime: dict[str, bool | str] = {}
    try:
        await session.execute(text("SELECT 1"))
        runtime["database"] = True
    except Exception as exc:  # probe honesty: report the failure, never hide it
        runtime["database"] = False
        runtime["database_reason"] = f"probe failed: {type(exc).__name__}"
    runtime["signing"] = getattr(request.app.state, "signing_key", None) is not None
    runtime["oidc"] = getattr(request.app.state, "keyring", None) is not None
    if settings.oidc_configured and not runtime["oidc"]:
        runtime["oidc_reason"] = "JWKS configured but keyring not loaded"
    runtime["kafka"] = bool(request.app.state.kafka_available)
    if settings.kafka_configured and not runtime["kafka"]:
        runtime["kafka_reason"] = "Kafka configured but producer not started"
    return capability_report(settings, runtime)


@router.post("/v1/ops:lapse-sweep")
async def lapse_sweep(
    request: Request, identity: IdentityDep, session: SessionDep, settings: SettingsDep
) -> dict[str, Any]:
    """Lapse every ACTIVE policy past its cover window, setting the
    revocation status-list bit for each so offline verifiers fail closed.
    Batch-sized and SKIP LOCKED; safe to invoke repeatedly (idempotent:
    already-lapsed policies are no longer ACTIVE and are never re-touched)."""
    require_policy(request, identity, "policy", "lapse-sweep", "CONFIDENTIAL")
    lapsed = await lifecycle.lapse_sweep(
        session, settings=settings, signing_key=get_signing_key(request),
        principal=identity.subject,
    )
    await session.commit()
    return {"lapsed": lapsed}


@router.get("/v1/audit/verify")
async def audit_verify(request: Request, identity: IdentityDep, session: SessionDep) -> dict[str, Any]:
    require_policy(request, identity, "audit", "verify", "CONFIDENTIAL")
    result = await audit.verify_chain(session)
    return {
        "ok": result.ok, "events": result.events,
        "firstBadId": result.first_bad_id, "detail": result.detail,
    }
