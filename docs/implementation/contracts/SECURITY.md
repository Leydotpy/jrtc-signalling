# Security and admission protocol

## Authentication
- Django authenticates registered users or establishes a permitted guest identity, checks meeting existence, meeting status, bans, waiting-room policy, limits and rate policy.
- Django creates cryptographically signed short-TTL **audience-limited gateway ticket** with issuer, key id, jti, nbf, exp, meeting/user/guest ID, participant/session ID, allowed scopes, authorization_epoch and optional gateway route. No reusable arbitrary claims from the browser.
- Gateway verifies signature with trusted key rotation policy, audience, issuer, expiry/nbf, jti replay policy, session binding, allowed origin, connection limits and current revocation/authorization epoch. Gateway must reject token scope `lobby` for media operations.
- Treat rejoin/resume as a new bounded connection session/generation, not unconditional inheritance of an old socket's handles. Define token refresh process over authenticated Django HTTPS.

## Admission
- Django may mint a **lobby-only** ticket before approval. Lobby channel is isolated from media subscriptions and private state.
- Gateway emits observed lobby arrival / join request only for a Django-created authenticated join intent. Host's admit button invokes Django API. Django transactionally updates approval, authorization epoch, outbox; gateway receives command or fetches changed grant via trusted service channel.
- For no-waiting-room meetings Django explicitly authorizes immediate entry. The gateway never auto-admits based merely on its own `join_requested` event.
- Prevent approve/revoke race: latest monotonic authorization epoch wins; a revoked/ended meeting always blocks publishing/subscribing regardless of old ticket age.

## Moderation
- Host/moderator authorization is checked in Django for every sensitive operation. Django transactionally revokes or changes role first, then sends command to owner. Gateway checks epoch and enforces kick/stop-publishing/detach via JRTC; confirms outcome separately.
- An unconfirmed disconnect is `revoked, enforcement_pending`, not `removed_from_transport`. Authorization remains revoked if gateway is down. On recovery, reconcile before accepting media work.
- Limit blast radius: gateway service token/command channel mTLS or equivalent authenticated identity, broker ACLs, minimal Janus credential scopes, no unauthenticated admin endpoints.

## Security tests
Forged/expired/reused/wrong-audience tickets; websocket origin bypass; guessed meeting/session; lobby media attempt; revoked-before-command race; duplicate command; unknown worker; stale epoch; resume after ban; cross-tenant/meeting event injection; replayed authorization; broker spoofing; overlarge SDP/candidate batches; timing/resource exhaustion; failure of authority/Redis/Broka; secrets leakage in logs/traces.
