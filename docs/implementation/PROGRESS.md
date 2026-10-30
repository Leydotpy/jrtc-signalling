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
| SG-06–SG-18 | NOT_STARTED | Retain the [original dependency order](EXECUTION_ORDER.md) |

## Verification

[Gateway CI 41014052042](https://github.com/Leydotpy/jrtc-signalling/actions/runs/41014052042)
passed all five jobs: 273 gateway tests on Python 3.12, 3.13 and 3.14; lint,
formatting, types, schema export and package build; four real Django authority
handoff/command-data checks; and Docker build/config/startup/shutdown checks.

[Synq CI 41013971484](https://github.com/Leydotpy/synq/actions/runs/41013971484)
passed all 149 tests on PostgreSQL 16, including concurrent replay consumption,
permission changes, admission retries and approval/removal races. No skips.
Migration drift checks also passed. These results cover authority and protocol
compatibility; live Janus/Broka and browser media remain later gates.

## What works and what remains

The standalone service runs, validates wire contracts, exposes health probes and
owns its lifecycle. Synq issues signed scoped tickets with shared replay state and
permission epochs. The gateway authenticates WSS sessions and enforces current
scopes. Django now decides direct/waiting-room admission and records permission
changes atomically in its outbox. Admission and pending enforcement are distinct
from connected media. Media stays disabled; readiness is 503. Outbox delivery,
exclusive JRTC ownership, live media, event projection and browser routing remain
later tasks.

This implementation batch (SG-03–SG-05) is complete and committed on feature branches.
Next: SG-06 durable authenticated Broka command/event delivery, followed by SG-07
exclusive JRTC ownership and fencing.
No production routing or deployment has changed.
