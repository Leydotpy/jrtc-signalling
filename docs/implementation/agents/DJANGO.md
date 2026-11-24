# Django agent — authoritative control plane
- Retain authoritative ORM meeting, waiting room, guest identity and permission model. Add scoped ticket issuance, permission epochs, outbox, command outcome tracking and trusted gateway integration.
- Add Broka durable inbox/consumer state projector and SSE subscriber route (or reuse existing supported emitter; evaluate in SG-00). Mutation in DB transaction; fanout post-commit.
- Replace **for gateway-assigned meetings only** direct Janus calls from `src/conf/asgi.py`, `services/janus.py`, `services/signaling.py` and Celery hooks with gateway command routing; retain legacy code behind feature flag until safe cutover.
- Avoid database queries on high-frequency trickle/candidate path. Do not persist live plugin Python objects; historical migrations remain untouched.
- Permission revocation must be immediate even if enforcement temporarily fails; clearly report pending vs confirmed.
