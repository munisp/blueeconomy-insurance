# Security Posture — blueeconomy-insurance

Phase 11 security audit (branch `phase11/security`).

## Controls verified
- **Secrets**: working-tree scan clean (only a `PLACEHOLDER CHANGE_ME` test fixture key, refused at boot by design).
- **AuthN/Z**: PBAC policy directory boot-validated; OIDC via JWKS; partner insurer adapters fail closed (`ADAPTER_UNCONFIGURED` before any network I/O); ISR evidence digest-pinned.
- **Key handling**: same Ed25519 file-key doctrine as tax-stamps; placeholder and permissive-mode refusals.

## Fixes this phase
- **CRITICAL**: `INSURANCE_ALLOW_PERMISSIVE_KEY_FILE` escape hatch now hard-refuses when `ENV=production` (`permissive-key-file-refused`). Regression test added.

## Residuals
- Run `pip-audit` in CI; integration tests (24 skipped locally) need the full stack.
