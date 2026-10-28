# JRTC signalling

Dedicated Python ASGI signalling service for Synq. Django remains the authority
for identity, admission, roles and revocation. JRTC and VideoRoom will manage
Janus sessions in this separately deployed process; browser media goes directly
to Janus.

Implementation follows the [task pack](docs/implementation/README.md) in dependency
order. See [progress](docs/implementation/PROGRESS.md) and the
[baseline report](docs/implementation/tracking/reports/SG-00.md).

This is an implementation in progress, not a deployed media service. Existing
Django and browser signalling remain unchanged.
