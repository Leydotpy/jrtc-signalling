# Ticket and authority contract

These are SG-01 integration requirements, not an implemented verifier. The ASGI
scaffold rejects every WebSocket. `validate_message(..., schema="ticket-claims")`
checks shape only; a passing result must never be treated as authentication.

Django mints RS256 JWTs with an explicit kid and typ=JWT. Pin the issuer and
audience (`jrtc-signalling`), allow only RS256, and trust only configured RSA
public keys (at least 2048 bits). Never resolve URLs from a JWT header or infer
an allowed algorithm from the token. Rotate keys with a bounded overlap;
unknown kid fails closed. Public-key distribution is an authenticated deployment
configuration/authority responsibility to be implemented in SG-03/04.

Required claims are in `ticket-claims.schema.json`: sub, jti, iat, nbf, exp,
meeting/participant/session/grant IDs, scopes, authorization_epoch and generation,
alongside iss/aud. TTL at most 120 s. SG-04 must check current time, with at most
5 s clock skew, and atomically consume each connection ticket's jti in shared
replay state through expiration. Replay-store failures deny new access. Refresh
requires a newly issued ticket and another current-grant check. Tickets are
credentials and never appear in URLs, access logs, traces or exception messages.

Only `lobby`, `media:publish`, `media:subscribe`, `session:resume` scopes exist in
v1. Lobby cannot coexist with media scopes. Publishing additionally checks
current audio/video/screen grants; scope alone is insufficient. Guest identities
must have a Django-created join intent and participant/session record before a
lobby ticket can be issued. Admit/revoke/meeting-end increment the authoritative
epoch transactionally. A newer revoke wins regardless of command arrival order.

Exact origin allowlisting is mandatory for browser connections. Missing Origin
is denied unless a separately authenticated non-browser policy is explicitly
implemented. Bind every message to the connection's verified identity; do not
trust mutable envelope IDs. Check live grant/owner state for access-changing
operations, and use a bounded cache with invalidation for ICE. Unknown/stale
revocation state denies publish/subscribe/rejoin. SG-06 needs an authenticated
service channel and broker ACLs; possession of a command JSON object is not proof
of the sender's authority.

SG-04/06/13/14 acceptance must cover signature, expiry, replay races, origin
bypass, cross-meeting identity substitution, lobby media attempts, admission vs
revoke races, unknown owners, stale lease/generation, broker spoofing and restart.
The existing contract tests cover malformed shapes, bounded messages and safe
errors only. No cryptographic or live authority result is claimed yet.
