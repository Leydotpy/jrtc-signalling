# Implementation progress

Updated 2026-10-29. DONE means implemented and verified for the stated task scope,
not deployed or approved for production.

Work is on `codex/gateway-foundation`; it has not been merged into `main`.

| Task | State | Evidence / implementation commit |
| --- | --- | --- |
| SG-00 Baseline | DONE | [Report](tracking/reports/SG-00.md), `5cde8793030630795e83f39bf3b84c4635678120` |
| SG-01 Contracts | DONE | [Report](tracking/reports/SG-01.md), `362e7ff3521023409f9ec29e4de38ab55aa74f95` |
| SG-02 ASGI scaffold | DONE | [Report](tracking/reports/SG-02.md), `b0aae66c50c7fa3becf954ab875f842b1000d886` |
| SG-03 Django ticket issuer | DONE | [Report](tracking/reports/SG-03.md), Synq `0426eee3497f35cdd37f9725a352c9fef11014f2` |
| SG-04 Gateway authentication | IN_PROGRESS | Code and local verification complete; commit/CI report follows |
| SG-05–SG-18 | NOT_STARTED | Retain the [original dependency order](EXECUTION_ORDER.md) |

## Verification

164 tests passed locally, together with lint, formatting, types, schema export
checks, package build and a clean wheel installation. The real loopback server
test verifies liveness, denied WebSocket upgrades, log redaction and SIGTERM cleanup.

[GitHub Actions run 40832176854](https://github.com/Leydotpy/jrtc-signalling/actions/runs/40832176854)
passed for implementation commit `b0aae66c50c7fa3becf954ab875f842b1000d886`:
Python 3.12, 3.13 and 3.14 verification jobs plus Docker build/config/startup/shutdown
smoke checks. This is scaffold verification, not live Janus/Broka or browser media.

## What works and what remains

The standalone service runs, validates wire contracts, exposes health probes and
owns its lifecycle. Synq issues signed scoped tickets with shared replay state and
permission epochs. Gateway authentication is implemented and being verified.
Media stays disabled; readiness is 503. Durable commands, exclusive JRTC ownership,
live media, event projection and browser routing remain later tasks.

SG-03 PostgreSQL CI: 129 passed, including replay/revocation races; see its report.
Next: complete SG-04 verification/report, then SG-05 Django admission and outbox.
No production routing or deployment has changed.
