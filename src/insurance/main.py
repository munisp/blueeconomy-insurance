"""FastAPI application assembly.

Boot is fail-closed:
- the signing key MUST load and MUST NOT be placeholder material;
- the PBAC policy directory MUST parse with at least one valid rule;
- when OIDC is configured the JWKS MUST load;
- when the inbound key directory is configured it MUST load;
- without a database URL the service refuses to boot.

Optional integrations (Kafka, partner adapters, ISR evidence) do not block
boot but are reported unavailable in GET /v1/capabilities and their
dependent routes return 503.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from insurance.api.auth import JwksKeyring
from insurance.api.pbac import PolicyEngine
from insurance.api.routes_claims import router as claims_router
from insurance.api.routes_integrations import router as integrations_router
from insurance.api.routes_ops import router as ops_router
from insurance.api.routes_policies import router as policies_router
from insurance.api.routes_products import router as products_router
from insurance.api.routes_quotes import router as quotes_router
from insurance.config import get_settings
from insurance.crypto.eddsa import KeyDirectory, load_signing_key
from insurance.db import dispose_engine, init_engine

log = logging.getLogger("insurance")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    if not settings.database_url:
        raise RuntimeError("INSURANCE_DATABASE_URL is required")
    if not settings.signing_key_path:
        raise RuntimeError("INSURANCE_SIGNING_KEY_PATH is required")
    if not settings.issuer_did:
        raise RuntimeError("INSURANCE_ISSUER_DID is required")
    if not settings.policy_dir:
        raise RuntimeError("INSURANCE_POLICY_DIR is required")
    app.state.settings = settings
    # Boot-fatal: placeholder/dummy key material refuses to boot here.
    app.state.signing_key = load_signing_key(settings.signing_key_path, settings.kid)
    # Boot-fatal: policy directory must be valid and non-empty.
    app.state.policy_engine = PolicyEngine.load(settings.policy_dir)
    # OIDC optional; when configured the JWKS must load or boot fails.
    app.state.keyring = JwksKeyring.load(settings) if settings.oidc_configured else None
    # Inbound envelope verification key directory; when configured it must load.
    app.state.key_directory = (
        KeyDirectory.load(settings.key_directory_path) if settings.key_directory_configured else None
    )
    # Partner adapter registry is validated at boot (malformed = boot-fatal).
    app.state.partner_adapters = settings.partner_adapters()
    init_engine(settings.database_url)
    app.state.kafka_available = settings.kafka_configured
    log.info("blueeconomy-insurance booted (kid=%s)", settings.kid)
    yield
    await dispose_engine()


app = FastAPI(
    title="blueeconomy-insurance",
    version="0.1.0",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


@app.exception_handler(RequestValidationError)
async def validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        media_type="application/problem+json",
        content={
            "type": "about:blank",
            "title": "Unprocessable Entity",
            "status": 422,
            "detail": "request validation failed; client-supplied totals are rejected — "
                      "pricing is computed server-side",
            "errors": exc.errors()[:10],
        },
    )


@app.exception_handler(Exception)
async def unhandled_handler(request: Request, exc: Exception) -> JSONResponse:
    log.exception("unhandled error on %s", request.url.path)
    return JSONResponse(
        status_code=500,
        media_type="application/problem+json",
        content={"type": "about:blank", "title": "Internal Server Error", "status": 500},
    )


app.include_router(ops_router)
app.include_router(products_router)
app.include_router(quotes_router)
app.include_router(policies_router)
app.include_router(claims_router)
app.include_router(integrations_router)


def run_api() -> None:
    settings = get_settings()
    uvicorn.run(
        "insurance.main:app",
        host=settings.http_host,
        port=settings.http_port,
        log_level="info",
    )


if __name__ == "__main__":
    run_api()
