# Implementation progress

Updated 2026-10-29. DONE means implemented and verified for the stated task scope,
not deployed or approved for production.

Work is on `codex/gateway-foundation`; it has not been merged into `main`.

| Task | State | Evidence / implementation commit |
| --- | --- | --- |
| SG-00 Baseline | DONE | [Report](tracking/reports/SG-00.md), `5cde8793030630795e83f39bf3b84c4635678120` |
| SG-01 Contracts | DONE | [Report](tracking/reports/SG-01.md), `362e7ff3521023409f9ec29e4de38ab55aa74f95` |
| SG-02 ASGI scaffold | DONE | [Report](tracking/reports/SG-02.md), `b0aae66c50c7fa3becf954ab875f842b1000d886` |
| SG-03 Django ticket issuer | BLOCKED | [External prerequisite](tracking/reports/SG-03.md); implementation belongs to `Leydotpy/synq` |
| SG-04–SG-18 | NOT_STARTED | Retain the [original dependency order](EXECUTION_ORDER.md) |

## Verification

164 tests passed locally, together with lint, formatting, types, schema export
checks, package build and a clean wheel installation. The real loopback server
test verifies liveness, denied WebSocket upgrades, log redaction and SIGTERM cleanup.

[GitHub Actions run 40832176854](https://github.com/Leydotpy/jrtc-signalling/actions/runs/40832176854)
passed for implementation commit `b0aae66c50c7fa3becf954ab875f842b1000d886`:
Python 3.12, 3.13 and 3.14 verification jobs plus Docker build/config/startup/shutdown
smoke checks. This is scaffold verification, not live Janus/Broka or browser media.

## What works and what remains

The standalone service runs, validates configuration and wire contracts, exposes
health probes and owns its startup/shutdown lifecycle. Live signalling remains
disabled and readiness returns 503. Ticket signature verification, live grants,
durable command delivery, exclusive JRTC ownership and browser media operations
are not implemented. No production routing or deployment has changed.

Next: implement SG-03 in Synq using the agreed ticket/grant contract, then SG-04
in this repository. Do not replace authoritative grants with gateway-created
credentials to bypass the dependency.

Machine-readable state: [progress.json](tracking/progress.json).
