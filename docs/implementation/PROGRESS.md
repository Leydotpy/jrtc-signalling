# Implementation progress

Updated 2026-10-30. DONE means implemented and verified for the stated task scope,
not deployed or approved for production.

Work is on `codex/gateway-foundation`; it has not been merged into `main`.

| Task | State | Evidence / implementation commit |
| --- | --- | --- |
| SG-00 Baseline | DONE | [Report](tracking/reports/SG-00.md), `5cde8793030630795e83f39bf3b84c4635678120` |
| SG-01 Contracts | DONE | [Report](tracking/reports/SG-01.md), `362e7ff3521023409f9ec29e4de38ab55aa74f95` |
| SG-02 ASGI scaffold | DONE | [Report](tracking/reports/SG-02.md), `b0aae66c50c7fa3becf954ab875f842b1000d886` |
| SG-03 Django ticket issuer | DONE | [Report](tracking/reports/SG-03.md), Synq `0426eee3497f35cdd37f9725a352c9fef11014f2` |
| SG-04 Gateway authentication | DONE | [Report](tracking/reports/SG-04.md), `a1b5de07ed7e552f1202114c185e239dd9387e41` |
| SG-05 Admission/outbox | DONE | [Report](tracking/reports/SG-05.md), Synq `8000bc062579b760fb373387d5945243a568ea59`; gateway compatibility `c2815c95eddaeef6594de6ffa0fe452107fdae36` |
| SG-06 Command delivery | DONE | [Report](tracking/reports/SG-06.md), gateway `f8242ac5369ea4c92157e38482f81c5ed8a43a56` / test fix `8feab34f5d6d113888833afe157e32363ebd773d`; Synq `088fd3adee2a20e9a0b3cbeae0477930f4f35939` |
| SG-07–SG-18 | NOT_STARTED | Retain the [original dependency order](EXECUTION_ORDER.md) |

## Verification

[Gateway CI 41072340216](https://github.com/Leydotpy/jrtc-signalling/actions/runs/41072340216)
passed all five jobs: 313 gateway tests on each of Python 3.12, 3.13 and 3.14; lint,
formatting, types, schema export and package build; four real Django authority
handoff/command-data checks; and Docker build/config/startup/shutdown checks.
Real Redis tests cover signed delivery, pending reclaim, slow consumers,
duplicates and restart. The four handoff checks also pass locally against the
SG-06 Synq commit, including the new meeting.terminate payload.

[Synq CI 41074432251](https://github.com/Leydotpy/synq/actions/runs/41074432251)
passed all 169 tests on PostgreSQL 16 and Redis 7.4, including concurrent replay,
permission/admission races, dispatcher claims and a broker outage/retry through
the gateway and actual Django ASGI authority endpoint. No skips. Migration drift
checks also passed. Live Janus and browser media remain later gates.

## What works and what remains

The standalone service runs, validates wire contracts, exposes health probes and
owns its lifecycle. Synq issues signed scoped tickets with shared replay state and
permission epochs. The gateway authenticates WSS sessions and enforces current
scopes. Django now decides direct/waiting-room admission and records permission
changes atomically in its outbox. Signed owner-targeted commands now retry
durably, deduplicate at the gateway and report authenticated outcomes to Django.
Admission, accepted commands and applied enforcement remain distinct from
connected media. The default enforcer reports failed/owner_lost until actual
runtime ownership exists. Media stays disabled; readiness is 503. Exclusive JRTC
ownership, live media, event projection and browser routing remain later tasks.

SG-00–SG-06 are complete for their stated scopes and committed on feature branches.
Next: SG-07 exclusive JRTC ownership and generation fencing.
No production routing or deployment has changed.
