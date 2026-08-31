"""Honesty registry: GET /v1/capabilities publishes exactly what is and is
not available. An unconfigured integration is reported unavailable-with-reason
and its dependent routes return 503 — success is never fabricated."""

from __future__ import annotations

from typing import Any

from insurance.config import Settings

_KAFKA_REASON = "INSURANCE_KAFKA_BOOTSTRAP_SERVERS not configured"


def capability_report(settings: Settings, runtime: dict[str, bool | str]) -> dict[str, Any]:
    """``runtime`` carries live probe results keyed by integration name."""
    caps: list[dict[str, Any]] = []

    def add(name: str, available: bool, reason: str = "") -> None:
        entry: dict[str, Any] = {"capability": name, "available": available}
        if not available:
            entry["reason"] = reason
        caps.append(entry)

    def reason(key: str, default: str) -> str:
        return str(runtime.get(key) or default)

    add("database", bool(runtime.get("database")), reason("database_reason", "database probe failed"))
    add("signing.eddsa-jcs-2022", bool(runtime.get("signing")), reason("signing_reason", "signing key unavailable"))
    add("status-list.bitstring", bool(runtime.get("signing")), "requires signing key")

    if settings.oidc_configured:
        add("auth.oidc", bool(runtime.get("oidc")), str(runtime.get("oidc_reason") or "JWKS unavailable"))
    else:
        add("auth.oidc", False, "INSURANCE_OIDC_JWKS_URL/PATH and INSURANCE_OIDC_ISSUER not configured")

    if settings.kafka_configured:
        add("kafka.outbox-publisher", bool(runtime.get("kafka")), reason("kafka_reason", "Kafka unreachable"))
    else:
        add("kafka.outbox-publisher", False, _KAFKA_REASON)

    if settings.key_directory_configured:
        add("envelope.inbound-verification", True, "")
    else:
        add("envelope.inbound-verification", False, "INSURANCE_KEY_DIRECTORY_PATH not configured")

    if settings.isr_evidence_configured:
        add("isr.outcome-ledger-evidence", True, "")
    else:
        add("isr.outcome-ledger-evidence", False,
            "INSURANCE_KEY_DIRECTORY_PATH and INSURANCE_ISR_EVIDENCE_SOURCE_DIGESTS not configured")

    partners = settings.partner_adapters()
    for pid, cfg in sorted(partners.items()):
        if cfg["token_present"] or not cfg["auth_token_env"]:
            add(f"partner.{pid}", True, "")
        else:
            add(
                f"partner.{pid}", False,
                f"auth token env {cfg['auth_token_env']} is not set",
            )
    if not partners:
        add("partner.gateway", False, "INSURANCE_PARTNER_ADAPTERS_JSON not configured")

    # Declared-but-not-implemented scope, stated truthfully.
    add("reinsurance.ceding", False,
        "not implemented: no ceding ledger exists in this service; ceding is omitted rather than stubbed")
    add("payments.payout-rail", False,
        "not implemented: payout execution rail is out of scope; payout receipts "
        "are recorded from the platform boundary")

    return {"service": "blueeconomy-insurance", "capabilities": caps}


def capability_available(report: dict[str, Any], name: str) -> bool:
    for cap in report["capabilities"]:
        if cap["capability"] == name:
            return bool(cap["available"])
    return False
