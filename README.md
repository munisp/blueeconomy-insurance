# blueeconomy-insurance

Marine insurance engine for the BlueEconomy PPP platform: product catalogue,
deterministic server-side premium rating, quote → bind (maker-checker) →
policy issuance as W3C VC 2.0 credentials, endorsements, lapse/cancel with
bitstring status-list revocation, and claims with double-entry settlement.

## Design doctrine

- **Fail-closed everywhere.** Boot refuses placeholder/dummy signing keys,
  world-readable key files, malformed PBAC policies and malformed partner
  adapter config. Unconfigured integrations report unavailable via
  `GET /v1/capabilities` and return 503 from dependent routes — success is
  never fabricated. Partner-insurer calls to an unconfigured adapter fail
  with `ADAPTER_UNCONFIGURED` **before any network I/O**.
- **Integer money only.** All amounts are integer kobo (1 NGN = 100 kobo);
  all rates and risk factors are basis points. No floats anywhere;
  client-supplied premium totals are rejected on mismatch (`client-total-rejected`).
- **DB-enforced invariants.** CHECK constraints (status enums, dual control:
  checker ≠ maker on binds, adjuster assignment and settlement), unique
  constraints, append-only hash-chained audit (trigger-rejected UPDATE/DELETE),
  immutable bind decisions/endorsements/evidence/receipts, and a deferred
  constraint trigger that rejects COMMIT of any unbalanced double-entry journal.
- **Envelope v1.0 events.** FHIR R4 message Bundle + JWS compact EdDSA over
  RFC 8785 JCS, `kid = "insurance-<epoch>"`. Published via the transactional
  outbox: `insurance.policy.v1`, `insurance.policy-endorsed.v1`,
  `insurance.policy-cancelled.v1`, `insurance.claim.v1`,
  `insurance.claim-paid.v1`.
- **VC 2.0 policies.** `eddsa-jcs-2022` Data Integrity proofs, bitstring
  status list (revocation/suspension), Luhn mod-34 check-digit policy numbers
  (`NG-<KIND3>-<YYYY>-<SEQ10>-<CHECK>`) gated before any database lookup.
  Policy credentials are minimal-disclosure: no premiums, values or assured
  identifiers inside the credential.

## Surface

| Area | Routes |
| --- | --- |
| Products | `POST /v1/products`, `POST /v1/products/{code}/versions/{v}/activate`, `POST .../rate-tables`, `GET /v1/products`, `GET .../rate-tables` |
| Quotes | `POST /v1/quotes`, `POST /v1/quotes:fromDeclaration` (NTP VAS attach), `GET /v1/quotes/{ref}`, `POST ...:bind`, `POST ...:bind-decision`, `POST ...:issue`, `GET .../policy` |
| Policies | `GET /v1/policies/{number}` (+ `/credential`, `/endorsements`), `POST .../endorsements`, `POST ...:cancel`, `GET /v1/status-list/{purpose}` (public) |
| Claims | `POST /v1/claims`, `GET /v1/claims/{ref}`, `POST .../documents`, `...:propose-adjuster`, `...:confirm-adjuster`, `...:propose-settlement`, `...:approve-settlement`, `...:reject`, `POST /v1/claims:payout-receipt` |
| Integrations | `POST /v1/integrations/isr/evidence` (digest-pinned ISR outcome-ledger evidence), `GET /v1/partners`, `POST /v1/partners/{id}:call`, `GET /v1/aggregates/portfolio` (regulator, `insurer-aggregator` role) |
| Ops | `GET /healthz`, `GET /v1/capabilities`, `GET /v1/audit/verify` |

## ISR outcome-ledger integration

The engine consumes premium-delta evidence from blueeconomy-maritime-intelligence's
outcome ledger (`maritime.outcome.v1` envelopes): signature-verified against
the mounted key directory, **digest-pinned** to
`INSURANCE_ISR_EVIDENCE_SOURCE_DIGESTS`, replay-killed, immutable once stored.
Corridor route risk = Σ confirmed premium-delta basis points, fed into the
rating engine (capped per rate table) with the full evidence list persisted
in every quote's rating trace.

## Reinsurance/ceding

**Omitted, not stubbed.** There is no ceding ledger in this service; the
capabilities registry reports `reinsurance.ceding` unavailable with an
explicit "not implemented" reason. Partner gateways exist for evidence
exchange only and are fail-closed when unconfigured.

## Development

```bash
pip install -e '.[dev]' pgserver   # pgserver bundles a REAL PostgreSQL
pytest tests/unit -q               # pure unit tests
pytest tests/integration -q        # real PostgreSQL (pgserver or INSURANCE_TEST_DATABASE_URL)
ruff check src tests && mypy src
```

Configuration is 12-factor env-only (`INSURANCE_*`); see `src/insurance/config.py`.
Secrets never have defaults and are never committed.
