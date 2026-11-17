# Implementation progress

Updated 2026-11-17. DONE means implemented and verified for the stated task scope,
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
| SG-08 Media signalling | DONE | [Report](tracking/reports/SG-08.md), implementation `fb0d048932be369fb50c7305b0f35ad6df409f9d`, fixes through `f24631887cf06b73a219e17119f793be37d68dc5` |
| SG-09 Ordered ICE/restart | DONE | [Report](tracking/reports/SG-09.md), implementation `ee8999b0f987f9cdfe4513adb419291970a246bd`, CI portability fix `1baf2fc221e7b84e50c7b861738964d08de74e91` |
| SG-10 Lifecycle events | DONE | [Report](tracking/reports/SG-10.md), gateway `16a3272ca4625b133f24dbbf0e8fdd1e036c8694`; Synq `a4ea69571bd9f21e8301f1b656ebce626b9c392f` |
| SG-11 State projection/SSE | DONE | [Report](tracking/reports/SG-11.md), Synq `2cd29394081243cc0be9feb62d7359f5a3dcf041` |
| SG-12 Client transport/SSE | DONE | [Report](tracking/reports/SG-12.md); browser CI 43563938796, runtime `a623d2f`, client `f2c080d`, Synq `d8f47f3`, plugin `eed4309` |
| SG-13 Moderation | DONE | [Report](tracking/reports/SG-13.md); implementation `e05d0bd`, acceptance runner `82f34a0`; all seven jobs passed in [CI 43715489748](https://github.com/Leydotpy/jrtc-signalling/actions/runs/43715489748) |
| SG-14 Reconnect/resume | DONE | [Report](tracking/reports/SG-14.md); gateway `4676d03`, Synq `68b0db2`, client `f43abaf`; [browser CI 44133663381](https://github.com/Leydotpy/synq.js/actions/runs/44133663381) |
| SG-15–SG-18 | NOT_STARTED | Retain the [original dependency order](EXECUTION_ORDER.md) |

## Verification

SG-14 implements fresh-ticket resume, bounded retry, confirmed old-handle cleanup,
stale-generation protection and automatic media rebuilding without another
waiting-room approval. [Gateway CI 43844350412](https://github.com/Leydotpy/jrtc-signalling/actions/runs/43844350412)
passed all seven jobs, covering **522 tests** across regular and dedicated live
suites. [Synq CI 43985742300](https://github.com/Leydotpy/synq/actions/runs/43985742300)
passed **245 PostgreSQL/Redis tests**, both actual Janus integrations, style and
migration checks. [Client/browser CI 44133663381](https://github.com/Leydotpy/synq.js/actions/runs/44133663381)
passed **78 client tests**, five publishing checks, 18 app tests, builds/types and
the complete Chromium/React/HTTPS/WSS/Janus/TURN sequence. Socket loss,
offline/online, ticket expiry and Janus restart all restore fresh media. The browser
also verifies quiet state repair, removal denial and meeting end when gateway
revocation precedes its HTTP acknowledgement. Local TypeScript compilation and all
78 client tests passed. The [report](tracking/reports/SG-14.md) records exact pins,
failure corrections and limits. SG-00–SG-14 are complete for their stated scope;
SG-15 is next. No merge, cohort enablement or deployment occurred.

SG-13 implements physical revocation, denial, kick, permission updates and meeting
termination, with monotonic epoch barriers and cleanup reconciliation. **513
gateway tests** passed with real Janus/Redis/TURN and zero skips, plus **242 Synq
tests**, three new actual Django/Broka/Janus moderation tests, both existing live
integrations and six authority compatibility tests. Lint, format, types, schema
and package builds passed. [The report](tracking/reports/SG-13.md) records exact
commands and limits. Implementation is published as `e05d0bd`; the acceptance
runner correction is `82f34a0`. [Hosted CI 43715489748](https://github.com/Leydotpy/jrtc-signalling/actions/runs/43715489748)
passed all seven jobs: Python 3.12/3.13/3.14 verification, seven live Janus/TURN
tests, three live moderation scenarios, six Django compatibility checks and the
container job. That run completed SG-00–SG-13; subsequent SG-14 evidence is recorded above.

SG-12 passed the complete Chromium/React integration in
[browser CI 43563938796](https://github.com/Leydotpy/synq.js/actions/runs/43563938796):
direct/lobby admission, four audio peers exchanging RTP over TURN, ICE completion,
chat/reaction, ticket renewal, forced SSE drop/resume, silent repair, explicit
rejoin, removal/reconnect denial and meeting end. **490 gateway tests** passed
locally with real Janus/Redis/TURN and zero skips; final gateway CI passed all six
jobs. **242 Synq tests**, both live integrations, **49 client tests**, five
publishing checks, 18 app tests, builds/types/lint/schema/migration checks passed.
The [report](tracking/reports/SG-12.md) records exact pins, fixes and scope limits.
The current SG-14 implementation and publication status are recorded above.
No merge, cutover or deployment occurred.

SG-11 passed **228 Synq tests** locally against PostgreSQL 16.2/Redis, zero skips,
and both actual Janus integrations: the existing lifecycle path and the new
Janus -> Broka -> Django projection -> authenticated HTTP snapshot/SSE path.
[Synq CI 42308160215](https://github.com/Leydotpy/synq/actions/runs/42308160215)
passed the same 228 tests on PostgreSQL 16/Redis 7.4, both live integrations,
targeted Ruff checks and migration drift. [The report](tracking/reports/SG-11.md)
records exact commands, timing, initial harness corrections and limits. The gateway
package pin is unchanged; no frontend cutover or deployment occurred.


SG-10 passed **486 gateway tests** locally with real Janus 1.1.2, Redis 7.0.15
and coturn 4.6.1, with zero skips. **198 Synq tests** passed against PostgreSQL
16.2/Redis, also with zero skips, using the installed immutable gateway artifact.
A separate live Janus -> Broka -> Django/PostgreSQL check passed signed ticket and
owner HTTP, lobby isolation, publishing observations and broker-stalled disconnect
replay. Lint, formatting, 21-file typecheck, ten schema exports, migration drift
and package builds passed. [The report](tracking/reports/SG-10.md) records commands,
initial failures, scope limits and rollback.

[Gateway CI 42263270744](https://github.com/Leydotpy/jrtc-signalling/actions/runs/42263270744)
passed all six jobs: 480 tests plus six dedicated-live-test skips on each supported
Python version, six actual Janus/TURN tests, six authority compatibility checks,
container and packaging checks.
[Synq CI 42263492020](https://github.com/Leydotpy/synq/actions/runs/42263492020)
passed all 198 PostgreSQL 16/Redis 7.4 tests and the live lifecycle integration.
No required test is skipped overall. Earlier task evidence remains in its reports.

## Current implementation boundary

The standalone gateway authenticates sessions, exchanges signed owner-targeted
commands/outcomes, owns Janus resources under Django-issued leases, and supports
bounded media/ordered ICE rehearsal. Optional lifecycle publication now persists
complete observations, retries/reconciles broker failures and records authenticated
snapshots in Django. SDP/ICE never enters Broka or Django; observation events never
change admission, roles, permissions or joined_at.

Optional Django projection and authenticated SSE/snapshots now provide monotonic,
permission-filtered state, replay/snapshot repair and disposable Redis presence.
Admission remains independent of observed connection. Lifecycle and state flags
remain off by default and gateway readiness stays 503. Frontend integration is
implemented behind an explicit cohort and passed SG-12 browser verification.
SG-13 adds physical moderation, current-grant reconciliation during broker outages,
and confirmed handle/room cleanup. Uncertain media cleanup retains its conservative
lease fence. SG-14 adds bounded automatic resumption, fresh authority, quiet state
repair and rebuilt media. Multiworker routing and reconciled failover remain SG-15.

After SG-14, the remaining dependency order is:

| Task | Remaining work |
| --- | --- |
| SG-15 Multiworker ownership | Route to the current worker, reconcile lease loss, prevent duplicate owners, and drain safely during restart |
| SG-16 Performance and observability | Measure latency/load, exercise outages, and add bounded metrics, logging and alert guidance |
| SG-17 Staged cutover and rollback | Enable selected local/staging cohorts, prove no dual ownership, and rehearse safe rollback |
| SG-18 Final acceptance | Verify the complete cross-repository flow, failure matrix, reproducible setup and readiness evidence |

These four tasks have not started. Production rollout remains a separate decision.
