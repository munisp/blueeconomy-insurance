# blueeconomy-insurance — marine insurance engine
# Multi-stage: wheels built once, runtime carries no compiler.

FROM python:3.12-slim AS build
WORKDIR /src
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip wheel --no-cache-dir --no-deps -w /wheels . \
 && pip wheel --no-cache-dir -w /wheels \
      "fastapi>=0.115,<1" "uvicorn[standard]>=0.30,<1" \
      "sqlalchemy[asyncio]>=2.0,<3" "alembic>=1.13,<2" "asyncpg>=0.29,<1" \
      "redis>=5,<7" \
      "pydantic>=2.7,<3" "pydantic-settings>=2.3,<3" "cryptography>=42,<46" \
      "aiokafka>=0.10,<1" "httpx>=0.27,<1" \
      "opentelemetry-api>=1.27,<2" "opentelemetry-sdk>=1.27,<2" \
      "opentelemetry-exporter-otlp-proto-grpc>=1.27,<2" \
      "opentelemetry-instrumentation-fastapi==0.48b0" \
      "setuptools<81"
# setuptools/pkg_resources: python:3.12-slim doesn't ship it, but
# opentelemetry-instrumentation's dependency-conflict checker still imports
# pkg_resources at runtime. Confirmed live: ModuleNotFoundError on boot even
# with a bare "setuptools" installed (84.0.0 by default) - setuptools
# itself dropped pkg_resources; upstream's own deprecation warning says to
# pin <81 (https://setuptools.pypa.io/en/latest/pkg_resources.html).

FROM python:3.12-slim AS runtime
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
RUN useradd --system --uid 10001 insurance
WORKDIR /app
COPY --from=build /wheels /wheels
RUN pip install --no-cache-dir --no-index --find-links=/wheels \
      blueeconomy-insurance "setuptools<81" \
 && rm -rf /wheels
COPY migrations ./migrations
COPY alembic.ini ./alembic.ini
COPY policies ./policies
USER 10001
EXPOSE 8080
# Secrets are env-only: INSURANCE_DATABASE_URL, INSURANCE_SIGNING_KEY_PATH,
# INSURANCE_OIDC_*, INSURANCE_PARTNER_ADAPTERS_JSON (+ referenced token envs).
# The service refuses to boot on placeholder/missing key material.
CMD ["insurance-api"]
