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
      "pydantic>=2.7,<3" "pydantic-settings>=2.3,<3" "cryptography>=42,<46" \
      "aiokafka>=0.10,<1" "httpx>=0.27,<1"

FROM python:3.12-slim AS runtime
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
RUN useradd --system --uid 10001 insurance
WORKDIR /app
COPY --from=build /wheels /wheels
RUN pip install --no-cache-dir --no-index --find-links=/wheels \
      blueeconomy-insurance \
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
