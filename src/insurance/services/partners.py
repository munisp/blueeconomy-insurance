"""Partner insurer gateway. Fail-closed adapter doctrine (same as
singlewindow externalAdapters):

- adapters come ONLY from INSURANCE_PARTNER_ADAPTERS_JSON (env);
- calling an unconfigured/unknown partner raises ADAPTER_UNCONFIGURED
  BEFORE any network I/O happens;
- a configured partner whose auth token env var is unset also fails
  ADAPTER_UNCONFIGURED before network I/O;
- every call attempt is recorded in the append-only gateway_calls table
  (request digest only, never payloads).

No ceding/reinsurance stubs exist here: the ceding ledger is omitted, not
faked (see capabilities: reinsurance.ceding = unavailable/not-implemented).
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from insurance.config import Settings
from insurance.models import GatewayCall
from insurance.services import audit


class GatewayError(ValueError):
    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason


_TIMEOUT = httpx.Timeout(10.0, connect=5.0)
_ALLOWED_OPERATIONS = {"quote-cede-offer", "claim-notification", "policy-evidence-pack"}


def _adapter(settings: Settings, partner_id: str) -> dict[str, Any]:
    adapters = settings.partner_adapters()
    cfg = adapters.get(partner_id)
    if cfg is None:
        raise GatewayError(
            "ADAPTER_UNCONFIGURED",
            f"no adapter configured for partner {partner_id!r}; refusing before any network I/O",
        )
    if cfg["auth_token_env"] and not cfg["token_present"]:
        raise GatewayError(
            "ADAPTER_UNCONFIGURED",
            f"auth token env {cfg['auth_token_env']} is not set; refusing before any network I/O",
        )
    return cfg


async def call_partner(
    session: AsyncSession,
    *,
    settings: Settings,
    partner_id: str,
    operation: str,
    payload: dict[str, Any],
    principal: str,
) -> dict[str, Any]:
    """POST a canonical JSON payload to a partner adapter endpoint.

    Returns the upstream JSON body. Every attempt (including refused ones
    that never touched the network made by callers catching the error) is
    auditable; successful/failed network calls are recorded here.
    """
    if operation not in _ALLOWED_OPERATIONS:
        raise GatewayError("unknown-operation", operation)
    cfg = _adapter(settings, partner_id)  # raises ADAPTER_UNCONFIGURED pre-I/O
    body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    digest = hashlib.sha256(body).hexdigest()
    url = f"{cfg['base_url']}/v1/{operation}"
    headers = {"content-type": "application/json"}
    if cfg["auth_token_env"]:
        headers["authorization"] = f"Bearer {cfg['_token']}"
    record = GatewayCall(
        partner_id=partner_id, operation=operation, request_digest=digest,
        outcome="UPSTREAM_ERROR", created_by=principal,
    )
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(url, content=body, headers=headers)
        record.response_status = resp.status_code
        if resp.status_code >= 400:
            raise GatewayError("UPSTREAM_ERROR", f"partner returned HTTP {resp.status_code}")
        try:
            data = resp.json()
        except Exception as exc:
            raise GatewayError("UPSTREAM_ERROR", "partner response is not JSON") from exc
        record.outcome = "OK"
        return data if isinstance(data, dict) else {"result": data}
    except httpx.HTTPError as exc:
        raise GatewayError("UPSTREAM_ERROR", str(exc)) from exc
    finally:
        session.add(record)
        await session.flush()
        await audit.record(session, "gateway.call", {
            "partnerId": partner_id, "operation": operation,
            "requestDigest": digest, "outcome": record.outcome,
            "responseStatus": record.response_status, "by": principal,
        })


def registry_view(settings: Settings) -> list[dict[str, Any]]:
    """Public (non-secret) view of the configured partner registry."""
    out = []
    for pid, cfg in sorted(settings.partner_adapters().items()):
        out.append({
            "partnerId": pid,
            "baseUrl": cfg["base_url"],
            "authTokenEnv": cfg["auth_token_env"],
            "configured": cfg["token_present"] or not cfg["auth_token_env"],
        })
    return out
