# Dependency-ordered incremental execution

| ID | Independent task | Dependencies | Repos |
|---|---|---|---|
| SG-00 | Audit, SHA, topology and baseline | — | all |
| SG-01 | Freeze contract and compatibility map | SG-00 | all |
| SG-02 | Standalone gateway scaffold/CI | SG-01 | gateway |
| SG-03 | Django scoped ticket issuer | SG-01 | synq |
| SG-04 | Gateway auth verifier and WS hardening | SG-02, SG-03 | gateway |
| SG-05 | Django admission request/decision protocol | SG-03 | synq |
| SG-06 | Broka durable command/outbox | SG-01, SG-05 | synq, gateway |
| SG-07 | JRTC runtime & Janus ownership extraction | SG-02, SG-06 | gateway, synq, JRTC as needed |
| SG-08 | Gateway SDP/JSEP WebSocket operation layer | SG-04, SG-07 | gateway |
| SG-09 | ICE batching and negotiation correctness | SG-08 | gateway, SDK |
| SG-10 | Gateway lifecycle event publisher/consumer | SG-06, SG-07 | gateway, synq |
| SG-11 | Django event projection & SSE | SG-10 | synq |
| SG-12 | synq.js dual-transport adapter + SSE | SG-08, SG-09, SG-11 | synq.js |
| SG-13 | Moderation, removal, terminate & revoke | SG-06, SG-08, SG-11 | synq, gateway |
| SG-14 | Reconnect/resume & stale-generation safety | SG-09, SG-12, SG-13 | gateway, synq.js, synq |
| SG-15 | Multiworker routing, lease/fencing & drain | SG-07, SG-10, SG-14 | gateway, synq |
| SG-16 | Observability, load & resilience | SG-12, SG-15 | all applicable |
| SG-17 | Feature-flagged dual-path staged cutover + rollback | SG-13, SG-15, SG-16 | synq, synq.js, gateway |
| SG-18 | End-to-end audit and release readiness | SG-17 | all |

**Default**: run one task to completion, commit/report, then next task if dependencies satisfied; do not skip blockers. A multi-repo task must record separate commit SHAs. Task summaries list required tests and acceptance evidence. `DONE` means implemented locally and tested, **not** deployed. SG-17 may stage routing and a local/canary proof; releasing production or deleting legacy paths is outside permission granted by this pack.
