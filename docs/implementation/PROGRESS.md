# Implementation progress

Updated 2026-11-04. DONE means implemented and verified for the stated task scope,
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
| SG-07 Runtime ownership | DONE | [Report](tracking/reports/SG-07.md), gateway `162d78778a8fc23457e34a0792417d9e69b323ed` / integration fix `76109404888f5846be4dac24d608dbf49aba55f5`; Synq `ea002b1bc29b69681cdea1cd7364d7fa1c9613bf` |
| SG-08–SG-18 | NOT_STARTED | Retain the [original dependency order](EXECUTION_ORDER.md) |

## Verification

The final local gateway run passed all **343 tests with zero skips** on Python
3.12.14 using real isolated Janus 1.1.2 and Redis 7.0.15. This includes ASGI
startup/health/shutdown, reusable management handles, concurrent attachment,
disposal and fresh handles after server recovery. Lint, formatting, types and
all nine schema exports passed.

[Gateway CI 41191969654](https://github.com/Leydotpy/jrtc-signalling/actions/runs/41191969654)
passed all six jobs: 341 tests on each supported Python version, two additional
live Janus tests, existing Django compatibility checks and container/package
checks. After the signed ownership HTTP check was added, all five cross-repository
checks passed locally against the pinned SG-07 Synq implementation.

[Synq CI 41876688346](https://github.com/Leydotpy/synq/actions/runs/41876688346)
passed all **182 tests on PostgreSQL 16 and Redis 7.4**, with zero skips, and the
migration drift check. This includes competing-owner claims, signed ownership
HTTP round trips, legacy cohort exclusion, outbox races and broker retry.
The later CI reruns were still queued when these completion records were prepared;
they are not represented as passes. See the SG-07 report for exact evidence.

## What works and what remains

The standalone gateway authenticates control sessions, validates wire contracts,
and exchanges durable signed owner-targeted commands/outcomes with Django.
An optional ASGI-owned JRTC runtime now creates real Janus sessions and opaque
handles under Django-issued exclusive leases and generation checks. Persisted
meeting cohorts prevent legacy Django/Celery Janus work on gateway sessions.
Loss recovery invalidates old handles, and clean disposal precedes lease release.

Admission, accepted commands and applied enforcement remain distinct from media
presence. A correctly routed command reports failed/unavailable until SG-13 adds
physical enforcement; an absent/stale owner reports failed/owner_lost. Browser
media dispatch remains disabled and overall readiness stays 503. Reconciled crash
takeover and multiworker routing remain SG-15.

SG-00–SG-07 are complete for their stated scopes and committed on feature branches.
Next: SG-08 browser-to-gateway media signalling operations.
No production routing or deployment has changed.
