# Performance Notes (Phase 11 audit)

Scope: index coverage vs. actual query code, unbounded queries, N+1 patterns,
connection-pool sizing, Kafka producer batching. No behavior changes; all
fail-closed invariants preserved.

## Indexes added (migrations/versions/0002_perf_indexes.py)

| Index | Justifying query |
|---|---|
| `ix_status_list_snapshots_purpose_time` `(purpose, created_at DESC)` | `statuslists._current_list` / `current_credential`: `WHERE purpose = ? ORDER BY created_at DESC LIMIT 1`, hit on every issuance/verification |

Already covered and intentionally not duplicated: `ix_outbox_unpublished`
partial index (outbox drain), `policies.policy_number` / `claims.claim_ref`
(unique), all FK `index=True` columns, `products` PK lookups.

## Query caps / pagination

- `GET /products` (catalog list) was unbounded; it now takes a `limit` query
  parameter (default 500, hard ceiling 5000) over the deterministic
  `(code, version)` ordering.
- All other routes are single-resource or `LIMIT 1` probes; the outbox drain
  is already batched.

## Connection pool sizing (env, opt-in)

`insurance.db.init_engine` now reads (defaults = previous hard-coded values):

- `INSURANCE_DB_POOL_SIZE` (default 10)
- `INSURANCE_DB_MAX_OVERFLOW` (default 5)
- `INSURANCE_DB_POOL_TIMEOUT` (default 30s)
- `INSURANCE_DB_POOL_RECYCLE` (default 0 = disabled)

Invalid values fail closed at startup. `pool_pre_ping` remains on.

## Kafka producer batching (env, opt-in)

`INSURANCE_KAFKA_LINGER_MS` (default 0 = unchanged) and
`INSURANCE_KAFKA_MAX_BATCH_SIZE` (default 16384) tune the outbox producer.
`enable_idempotence=True` is untouched.

## Remaining recommendations (not implemented)

- `routes_integrations` summary endpoints do full-table `GROUP BY status`
  counts; fine at current volumes, consider a materialized rollup if policies
  grow past ~1M rows.
- Endorsement listing per policy is indexed on `policy_id`; add
  `(policy_id, endorsement_no)` if policies accumulate many endorsements.
