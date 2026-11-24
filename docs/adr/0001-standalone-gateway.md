# ADR 0001 — Standalone gateway and additive v1 boundary

Date: 2026-10-28. Status: selected for repository implementation; production
cutover remains unapproved and depends on SG-03 through SG-18.

The user supplied an empty dedicated `Leydotpy/jrtc-signalling` repository. Use
the distribution `jrtc-signalling` and import `jrtc_signalling`, not a second
local gateway directory. Python 3.12–3.14 supports the inspected JRTC 3.3 and
VideoRoom 3.0.4 sources. Starlette plus Uvicorn provides a small standalone ASGI
process. Django project imports and settings are forbidden here.

Retain the existing libraries' async session/plugin APIs; do not copy the Django
runtime or implement a second generic Janus transport. Install/wire JRTC and
Broka when SG-06/07 have authoritative grants and exclusive ownership. Until
then all media access stays closed and readiness reports missing dependencies.
Broka's current distribution has a transitive Django dependency; that does not
authorize Django model/settings imports in the gateway. Reassess dependency
metadata when adding runtime packages.

Use JSON Schema 2020-12 for portable wire contracts and a bounded Python parser.
Keep current SDK methods and MediaSignalAck shapes through the compatibility
map. Add explicit client generation, request sequence and negotiation IDs to
the new transport without pretending legacy diagnostics were an authority fence.

Use Redis Streams through Broka for at-least-once durable domain delivery,
separate from legacy raw Janus events. Require outbox/inbox, owner fencing,
bounded queues and authenticated producers. Memory/pubsub may be test doubles,
never a production durability substitute. Exact delivery/lease integration is
SG-06/15 and cannot be proven by this scaffold.

The eventual default remains the existing Django Socket.IO route. Gateway
traffic is an opt-in cohort selected by Django; switching only the browser URL
is insufficient. Rollback stops new gateway routing, drains/releases ownership,
then creates clean legacy resources. Do not attach the same Janus handle from
both runtimes. Feature flags and canary routing are SG-17; none are active here.

Evidence: [SG-00](../implementation/tracking/reports/SG-00.md),
[v1 protocol](../PROTOCOL.md), [security contract](../SECURITY.md).
