# Architecture hardening backlog

Created: 2026-07-24  
Source: `docs/ARCHITECTURE_AUDIT_2026-07-24.md` and `docs/RECOMMENDATIONS.md`  
Working branch: `architecture-audit-2026-07-24`

## Execution policy

Tasks are taken in order. Each task must have an implementation, focused regression tests, a verification result, and a short completion note. Existing user changes, including `docs/.knowledges` and `docs/RECOMMENDATIONS.md`, are out of scope and must remain untouched.

## Backlog

| ID | Priority | Task | Done when | Status |
|---|---|---|---|---|
| ARCH-001 | P0 | Bound skill-review prompt assembly | Review prompt has a deterministic input budget, omission notes, no oversized model call, and executable review fails closed on overflow | Done — tests added; pytest blocked by environment |
| ARCH-002 | P0 | Complete A2A ACK contract | `ack` tool is registered, ACK is idempotent, processing-before-ACK is covered by tests, and delivery state transitions are documented | Done — tests added; pytest blocked by environment |
| ARCH-003 | P0 | Cover A2A rejection paths with DPA events | Every rejected API path emits one safe, correlated DPA event | Done — tests added; pytest blocked by environment |
| ARCH-004 | P1 | Make deployment reproducible | One key/config source, startup validation, health-aware compose dependencies, and `.env.example` | Done — static Compose validation passes; closed-server E2E remains |
| ARCH-005 | P1 | Close transport security gaps | Non-loopback control plane fails closed by default; explicit insecure override is audited; A2A transport has TLS/rotation plan | In progress — control-plane bind and stale warning fixed |
| ARCH-006 | P1 | Make A2A delivery semantics explicit and safe | At-least-once/claim semantics, consumer dedup, retry/dead-letter policy, and concurrency tests | Planned |
| ARCH-007 | P1 | Separate transport acceptance from crypto validation | Gateway state distinguishes accepted, validated, quarantined and rejected envelopes | Planned |
| ARCH-008 | P1 | Bound A2A storage and strengthen audit trail | Retention, size metrics, bounded cleanup and tamper-evident DPA chain | Planned |
| ARCH-009 | P1 | Remove unsafe unlocked writes from critical state | Terminal task, cancellation and budget updates never merge/write without a lock | Planned |
| ARCH-010 | P2 | Make deployment dependencies reproducible | One dependency SSOT, constraints/lock, package-wheel test and `pip check` | Planned |
| ARCH-011 | P2 | Protect development with PR CI | Default branch PRs run smoke, packaging and platform-appropriate gates | Planned |
| ARCH-012 | P2 | Pay down module-boundary debt | Supervisor events, extension loader and provider routing expose stable ports and stop importing live globals directly | Planned |

## Current sequence

1. `ARCH-001` — prompt budget and fail-closed overflow.
2. `ARCH-002` — ACK tool and client/server contract.
3. `ARCH-003` — DPA rejection coverage.
4. `ARCH-005` + `ARCH-004` — security and reproducible deployment.
5. `ARCH-006`–`ARCH-009` — delivery, crypto, storage and state reliability.
6. `ARCH-010`–`ARCH-012` — engineering system and boundary debt.

## Progress log

### 2026-07-24 — backlog created

- Created this backlog from the architecture audit and analyst recommendations.
- Preserved existing untracked user files.
- Started `ARCH-001`.

### 2026-07-24 — ARCH-001 completed

- Added deterministic `_MAX_REVIEW_PROMPT_CHARS` gate (`135,000` chars).
- Replaced full governance-doc injection with source-preserving relevant-section selection.
- Added explicit pending/fail-closed result when prompt assembly exceeds the budget; no LLM call is made.
- Preserved full executable skill payload semantics: oversized payloads are rejected, not silently truncated.
- Added `test_skill_review_prompt_has_deterministic_hard_budget`.
- Manual verification passed: prompt for the bundled small skill is `104,443` chars; module compiles; `git diff --check` passes.
- Targeted pytest could not run because the available Python environment has no `pytest` module.

### 2026-07-24 — ARCH-002 completed

- Exported the existing client ACK operation as the extension tool `ack`.
- The tool accepts only `message_id`, returns a safe public envelope, and documents at-least-once delivery until ACK succeeds.
- Kept `list_inbox` non-acknowledging so the caller can process durably before acknowledging.
- Added plugin registration and invalid-input regression tests.
- Module compilation and static diff checks pass; pytest remains blocked by the missing environment dependency.

### 2026-07-24 — ARCH-003 completed

- Added DPA rejection events for invalid reply relationships, ACK of a missing message, and ACK by the wrong recipient.
- Added an end-to-end contract test asserting the three reason codes are durable.
- Added a global FastAPI validation handler for authenticated malformed envelopes; it records safe path/correlation metadata and leaves invalid-credential auditing to the auth gate without duplication.
- Added a contract assertion for `malformed_envelope` DPA records.
- Database/audit-storage failure remains fail-closed at the HTTP boundary and intentionally does not expose storage details; durable retry for audit outages is a future hardening item.
- Python compilation and `git diff --check` pass; FastAPI/pytest execution is blocked because the current environment lacks both packages.

### 2026-07-24 — ARCH-005 progress

- `validate_network_auth_configuration()` now fails startup for non-loopback binds without a network password.
- The existing `OUROBOROS_TRUST_NONLOCAL_BIND_WITHOUT_PASSWORD=1` is now the only explicit escape hatch and is documented as trusted-ingress/VPN/private-network-only.
- Updated architecture documentation and regression tests.
- Remaining scope: A2A TLS/mTLS, token rotation/revocation, rate limits and transport threat-model hardening.
- Added A2A startup validation for missing-all and duplicate `AGENTn_A2A_TOKEN` configuration; duplicate credentials now fail before the gateway serves traffic.
- Python compilation and `git diff --check` pass; runtime test import is blocked because Starlette is not installed in the current environment.

### 2026-07-24 — tester report remediation

- Moved the A2A environment, key mounts, skill mount and gateway healthcheck into the active `compose.yaml`; the previously separate override is now consistent with it.
- Added `.env.example` with placeholders only, parameterized key suffixes and `A2A_KEYS_DIR`; no credentials are stored in the repository.
- Agents now wait for a healthy gateway before starting. Startup still does not bypass the owner-controlled review/enable lifecycle.
- Documented the runtime namespaced A2A tool surface, including the separate `ack` operation, and added a regression test for canonical names.
- Corrected the stale non-loopback startup warning so it names the actual password/override policy.
- Static checks pass: both Compose configurations render successfully, Python modules compile, and `git diff --check` is clean.
- Real E2E remains intentionally pending for the closed server because this workspace has neither the project virtual environment nor credentials.
