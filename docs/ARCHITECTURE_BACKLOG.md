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
| ARCH-002 | P0 | Complete A2A ACK contract | `ack` tool is registered, ACK is idempotent, processing-before-ACK is covered by tests, and delivery state transitions are documented | Planned |
| ARCH-003 | P0 | Cover A2A rejection paths with DPA events | Every rejected API path emits one safe, correlated DPA event | Planned |
| ARCH-004 | P1 | Make deployment reproducible | One key/config source, startup validation, health-aware compose dependencies, and `.env.example` | Planned |
| ARCH-005 | P1 | Close transport security gaps | Non-loopback control plane fails closed by default; explicit insecure override is audited; A2A transport has TLS/rotation plan | Planned |
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
