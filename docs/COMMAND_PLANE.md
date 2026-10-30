# Durable command plane (SG-06)

The optional `bus` extra uses the same Broka 0.0.2 Redis Streams backend as Synq.
Run `uv sync --locked --group dev --extra bus`. Frozen uv builds correct Broka's
erroneous mandatory Django/other-engine wheel dependencies using the reviewed
metadata in pyproject.toml; no Django package is imported or needed by the gateway.
Redis 7.4 is exercised in CI. Broka's public transport SPI carries bounded signed
bytes without generic object, pickle or broker-header deserialization.

Commands go to `synq.signalling.commands.v1.<owner-id>`; outcomes go to
`synq.signalling.events.v1`. This isolates owner queues from the existing
`janus.events` consumer. Durable consumer groups start from history and reclaim
unacknowledged entries. Broka 0.0.2 needs group/consumer IDs on its engine config;
the adapter also detects its startup-task failure case and reconnects. Queue
consumption is serial with an eight-entry reclaim batch and no task-per-message
fanout. Each command has a three-second processing budget and explicit ACK after
the inbox/outcome transaction. Storage/ACK failures leave Redis entries pending.

Configure `JRTC_SIGNALLING_BUS_CONFIG_FILE` with an absolute JSON file path:

```json
{
  "redis_url_file": "/run/secrets/gateway-redis-url",
  "producer_id": "gateway-a",
  "signing_key_id": "gateway-a-2026-10",
  "private_key_file": "/run/secrets/gateway-signing.pem",
  "trusted_keys": {
    "django-2026-10": {
      "producer_id": "django-control",
      "public_key_file": "/run/secrets/django-signing-public.pem"
    }
  },
  "inbox_path": "/var/lib/jrtc-signalling/commands.sqlite3",
  "authority_url": "https://synq.example/api/v1/meetings/internal/gateway/commands/authorize/",
  "authority_token_file": "/run/secrets/gateway-authority-token"
}
```

Keys are Ed25519 PEM keys dedicated to this bus, separate from RS256 tickets.
Each key ID pins its trusted producer identity. Keep old verification keys during
a bounded rotation overlap. Each canonical JSON packet signs version, producer,
key ID, kind and the entire typed body. A packet is at most 32 KiB. Duplicate JSON
fields, unknown fields, invalid schema/signature and wrong owner are rejected.
Only command and command-outcome bodies are supported in SG-06; lifecycle event
production/projection is SG-10. No SDP, ICE or arbitrary metadata is permitted.

Use `rediss://` outside loopback, with separate Redis ACL identities for Django
and gateways. Django may publish command queues and consume outcomes; each gateway
may consume only its own command queue and publish outcomes. Public keys do not
replace broker authentication. Configure Redis persistence/replication and
`maxmemory-policy noeviction`; no approximate MAXLEN trim is used because it can
delete unconsumed work. Retain commands/outcomes until the sole designated group
acknowledges them, then compact acknowledged history operationally. Broker storage
exhaustion must fail publication so the database outbox retains the command.

The gateway inbox must live on a persistent local volume owned by the process.
SQLite transactions use synchronous FULL. Multiple processes may share an inbox
on the same host; do not use a network filesystem or give two replicas separate
inboxes under the same owner identity. SG-07 provides the live owner lease/fence;
a configured producer ID alone is never a lease. The default enforcer returns
failed/owner_lost and never claims physical enforcement. Readiness remains 503.

The inbox stores a canonical command digest and durable accepted/terminal outcomes.
A matching duplicate replays the existing outcome; an ID with different content
is quarantined. A reserved command interrupted by crash/cancellation becomes
timed_out/owner_lost after its five-second reservation budget. It is never executed
again: reconciliation must establish what actually happened. Applied means a real
enforcer confirmed its side effect; publication or accepted does not mean applied.

The inbox has 10,000 slots, two outcome slots per command and 1,000 digest-only
quarantine entries. Capacity pressure leaves messages pending. Expired terminal
records with delivered outcomes are pruned only after an additional 600 seconds;
pending/dead outcomes remain. Outcome publication retries with backoff capped at
30 seconds; ten failures retain a dead letter. Redelivery of the original command
reopens its persisted outcomes. Operators should inspect dead letters and reconcile
uncertain enforcement before replay. Never resubmit an uncertain media mutation
under a new ID simply to bypass the inbox.

Command processing, SQLite work and broker publication run in owned background
tasks; SQLite calls run off the event loop. No Janus reader callback calls these
workers. Shutdown stops/cancels the worker, leaving uncertain commands recoverable.
The health response reports cached broker status; overall media readiness stays
closed until the later owner/media gates pass.

Disable the bus configuration to stop this component. Preserve both sides' inbox,
outbox and keys while pending commands drain or expire; disabling it does not
restore revoked Django permission. No deployment or legacy runtime cutover is
part of SG-06.
