# Authenticated control sessions (SG-04)

Authentication can be enabled for integration rehearsal. Media operations remain
unavailable until runtime ownership and dispatch are implemented in SG-07/08.
Readiness remains 503. A successful hello/refresh has an empty result: it does
not advertise a fabricated gateway owner or lease generation.

Set `JRTC_SIGNALLING_AUTH_CONFIG_FILE` to a mounted JSON file with exactly these
fields (the values below are placeholders, not deployable credentials):

```json
{
  "issuer": "https://synq.example",
  "public_keys": {"issuer-key-id": "-----BEGIN PUBLIC KEY-----\n...\n-----END PUBLIC KEY-----"},
  "origins": ["https://meet.example"],
  "authority_url": "https://synq.example/api/v1/meetings/internal/gateway/authority/",
  "authority_token_file": "/run/secrets/gateway-authority-token"
}
```

Use the issuer, public key and dedicated service credential configured for Synq
SG-03. Only RSA public keys of at least 2048 bits are accepted, with at most eight
key IDs. Deploy key rotation with bounded overlap as documented by Synq; token
headers cannot add keys, URLs or algorithms. Config/credential errors are generic.
Keep the configuration and TLS keys out of source control and restrict mounts.

Browser access requires WSS and one exact allowlisted HTTPS Origin. Missing/null
or duplicate Origin, arbitrary subprotocols, query strings and plain WS reject
before upgrade. The only subprotocol is `jrtc.signalling.v1`. The supported CLI
can serve TLS using `JRTC_SIGNALLING_TLS_CERT_FILE` and `JRTC_SIGNALLING_TLS_KEY_FILE`;
both are validated by `--check-config`. If TLS terminates elsewhere, the ASGI
deployment must establish a trusted secure scheme; this application never trusts
arbitrary forwarded headers. The default CLI disables proxy headers. No insecure
browser exception is implemented. A separate native-client policy is future work.

Send a v1 `session.hello` (or `session.resume`) as sequence 1 with the ticket in
the payload. Envelope meeting/session/generation must match the verified claims.
Every subsequent frame has the same binding and the next sequence. HTTPS ticket
refresh uses the existing participant_session_id and generation; send the new
ticket in `session.refresh` before expiration. Refresh cannot switch identity,
regress the epoch or replay an already consumed ticket. Resume's replay/recovery
semantics beyond authentication remain SG-14 work.

The verifier checks bounded/duplicate-free JWT JSON, pinned RS256 signature,
issuer, audience, all claims and times with zero skew (within v1's five-second
maximum). PyJWT performs signature validation; explicit clock checks enable
deterministic expiry testing. The same ticket must then pass Django's current
grant and atomic one-use receipt check. A gateway restart does not reset replay
state. A valid signature alone cannot grant access.

Each connection has one cached grant, expiring after 0.5 seconds measured from
the start of the RPC and never beyond ticket expiry. Ping/ICE may reuse it;
privileged media transitions always recheck Django. Invalidation racing an RPC
cannot repopulate the cache. Unavailable/expired/mismatched authority denies;
there is no stale fallback. The invalidation entry point is implemented/tested,
but its durable broker feed is SG-06 work. The cache window is not an atomic
remote authorization-to-Janus fence. Media remains disabled until ownership,
live revocation delivery and dispatch fences are complete.

Lobby cannot call media operations. Publisher/subscriber scopes and current
microphone/camera/screen grants are checked separately. SG-08 must also reconcile
declared tracks with actual SDP and apply grants to the Janus handle; client track
metadata alone is not proof of what media is published. This phase forwards no SDP.

## Resource and failure budgets

- At most 256 pending/active connections per worker. No unbounded IP/user map.
- Worker handshake token bucket: burst 20, refill 10/s; apply aggregate edge limits
  before scaling workers. Per authenticated connection: burst 60, refill 30 frames/s.
- First-frame authentication budget five seconds. An idle connection closes at
  ticket expiry. Sends/closes have two-second budgets. Frames use the configured
  maximum (ceiling 128 KiB); transport queue defaults to 16, compression off.
- Authority calls use TLS validation, the dedicated bearer credential, two-second
  total deadline, at most 16 pooled connections and a 16 KiB response ceiling.
  Redirects are forbidden. No JWT is sent to this RPC, only verified claims.
- Close 1008 for policy/authentication, 1013 for temporary pressure/authority
  unavailability, 1011 for unexpected failure, 1001 during shutdown. Reasons and
  logs never contain JWTs, raw frames, authority replies, SDP or ICE.

`scripts/check_django_authority.py` runs the actual Synq issuer, gateway verifier,
gateway authorization session and Django shared receipt/permission service in
one test environment. Its adapter crosses the Python async boundary; HTTP TLS is
tested separately with mock transport and real HTTPS/WSS process smoke checks.
It is not a live Janus/broker/browser end-to-end test. CI pins the Synq source SHA.
Rollback by unsetting AUTH_CONFIG_FILE and disabling Django's ticket feature;
the existing legacy meeting transport remains available throughout this phase.
