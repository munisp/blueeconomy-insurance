"""12-factor configuration. Every value comes from the environment (INSURANCE_*
prefix); no secrets in code, no defaults for secrets.

Fail-closed policy:
- the service refuses to boot with placeholder/dummy key material
  (see crypto.eddsa.load_signing_key);
- optional integrations (Kafka, JWKS endpoint, partner insurers) report
  unavailable via GET /v1/capabilities and return 503 from dependent routes
  rather than fabricating success;
- partner insurer adapters are env-configured; any call to an unconfigured
  partner fails with ADAPTER_UNCONFIGURED *before* any network I/O
  (same doctrine as singlewindow externalAdapters).
"""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Any

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PRODUCER = "blueeconomy-insurance"
KEY_EPOCH = 0
KID = f"insurance-{KEY_EPOCH}"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="INSURANCE_", env_file=".env", extra="ignore")

    # --- required ---
    database_url: str = ""
    signing_key_path: str = ""          # Ed25519 PKCS#8 PEM, file-mounted
    issuer_did: str = ""                # e.g. did:web:insurance.blueeconomy.gov.ng
    policy_dir: str = ""                # PBAC policy directory (boot-fatal when bad)

    # --- optional but fail-closed consumers when absent ---
    key_directory_path: str = ""        # {kid: b64u pubkey} for inbound envelope verification
    kafka_bootstrap_servers: str = ""   # outbox publisher
    kafka_topic_prefix: str = "insurance"
    kafka_consumer_group: str = "blueeconomy-insurance"

    # --- OIDC (Keycloak RS256/EdDSA via JWKS) ---
    oidc_jwks_url: str = ""             # https://keycloak/.../certs
    oidc_jwks_path: str = ""            # local JWKS file alternative (file-mounted)
    oidc_issuer: str = ""
    oidc_audience: str = ""

    # --- partner insurer gateway (fail-closed adapters) ---
    # JSON object: {"<partner_id>": {"base_url": "https://...", "auth_token_env": "ENV_NAME"}}
    # Auth tokens themselves are read from the referenced env var ONLY —
    # never inline, never defaulted.
    partner_adapters_json: str = ""

    # --- ISR evidence integration ---
    # Maritime-intelligence outcome-ledger premium-delta evidence must be
    # digest-pinned: sha256 hex digest of the authorized evidence source set.
    isr_evidence_source_digests: str = ""  # comma-separated sha256 hex digests

    # --- service ---
    http_host: str = "0.0.0.0"  # noqa: S104 -- container listener, ingress-terminated
    http_port: int = 8080
    status_list_base_url: str = ""      # public base for status-list credential ids
    quote_validity_hours: int = 72
    rate_limit_per_minute: int = 120

    @field_validator("database_url")
    @classmethod
    def _db_url(cls, v: str) -> str:
        if v and not v.startswith("postgresql+asyncpg://"):
            raise ValueError("database_url must be a postgresql+asyncpg:// URL")
        return v

    @field_validator("isr_evidence_source_digests")
    @classmethod
    def _digests(cls, v: str) -> str:
        if v:
            for d in v.split(","):
                d = d.strip()
                if len(d) != 64 or any(c not in "0123456789abcdef" for c in d):
                    raise ValueError("isr_evidence_source_digests must be comma-separated sha256 hex")
        return v

    @property
    def kid(self) -> str:
        return KID

    @property
    def kafka_configured(self) -> bool:
        return bool(self.kafka_bootstrap_servers)

    @property
    def oidc_configured(self) -> bool:
        return bool((self.oidc_jwks_url or self.oidc_jwks_path) and self.oidc_issuer)

    @property
    def key_directory_configured(self) -> bool:
        return bool(self.key_directory_path)

    @property
    def isr_evidence_configured(self) -> bool:
        return bool(self.key_directory_path and self.isr_evidence_source_digests)

    def partner_adapters(self) -> dict[str, dict[str, Any]]:
        """Parsed partner adapter registry. Malformed config is boot-fatal."""
        if not self.partner_adapters_json:
            return {}
        data = json.loads(self.partner_adapters_json)
        if not isinstance(data, dict):
            raise ValueError("partner_adapters_json must be a JSON object")
        out: dict[str, dict[str, Any]] = {}
        import os
        import re

        for pid, cfg in data.items():
            if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,63}", pid):
                raise ValueError(f"malformed partner id {pid!r}")
            if not isinstance(cfg, dict):
                raise ValueError(f"partner {pid}: config must be an object")
            base_url = cfg.get("base_url", "")
            if not isinstance(base_url, str) or not base_url.startswith("https://"):
                raise ValueError(f"partner {pid}: base_url must be https://")
            token_env = cfg.get("auth_token_env", "")
            if token_env and (not isinstance(token_env, str) or not re.fullmatch(r"[A-Z0-9_]{1,128}", token_env)):
                raise ValueError(f"partner {pid}: auth_token_env must name an env var")
            token = os.environ.get(token_env, "") if token_env else ""
            out[pid] = {
                "base_url": base_url.rstrip("/"),
                "auth_token_env": token_env,
                "token_present": bool(token),
                "_token": token,
            }
        return out


@lru_cache
def get_settings() -> Settings:
    return Settings()
