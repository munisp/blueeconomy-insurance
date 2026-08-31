"""Ops routes: health, capabilities (honesty registry), audit-chain verify."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from sqlalchemy import text

from insurance.api.deps import IdentityDep, SessionDep, SettingsDep, require_policy
from insurance.services import audit
from insurance.services.capabilities import capability_report

router = APIRouter(tags=["ops"])


@router.get("/healthz")
async def healthz() -> dict[str, Any]:
    return {"status": "ok", "service": "blueeconomy-insurance"}


@router.get("/v1/capabilities")
async def capabilities(request: Request, settings: SettingsDep, session: SessionDep) -> dict[str, Any]:
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


@router.get("/v1/audit/verify")
async def audit_verify(request: Request, identity: IdentityDep, session: SessionDep) -> dict[str, Any]:
    require_policy(request, identity, "audit", "verify", "CONFIDENTIAL")
    result = await audit.verify_chain(session)
    return {
        "ok": result.ok, "events": result.events,
        "firstBadId": result.first_bad_id, "detail": result.detail,
    }
