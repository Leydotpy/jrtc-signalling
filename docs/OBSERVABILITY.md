# Operating the signalling metrics

The gateway and Django expose Prometheus text on separate HTTPS endpoints. Each process samples its own activity. A scrape bearer opens only the metrics endpoint. The gateway's admission, moderation and readiness settings have separate controls.

| Process | Path | Scrape credential file | Optional trace key file |
| --- | --- | --- | --- |
| Gateway | `/metrics` | `JRTC_SIGNALLING_METRICS_TOKEN_FILE` | `JRTC_SIGNALLING_TRACE_KEY_FILE` |
| Django ASGI | `/api/v1/meetings/internal/gateway/metrics/` | `GATEWAY_METRICS_TOKEN_FILE` | `GATEWAY_TRACE_KEY_FILE` |

Mount distinct random secrets for scraping and tracing. Each file accepts 32–256 ASCII letters, digits or the characters `._~-`, with an optional final newline. An empty configuration keeps its function off. Restart the processes to load rotated keys. Configure TLS on the direct worker endpoint or use the application's existing trusted HTTPS forwarding configuration.

Give every gateway worker and Django ASGI process a stable scrape target. A load-balanced URL that selects a different process each time makes per-process counters unusable. Sum histogram buckets and counter rates across targets. Use `max` for shared Django database gauges so replicas do not multiply an outstanding command count.

This Prometheus example scrapes one gateway. Add an equivalent job for Django with its metrics path, addresses and bearer file. Keep target addresses and credentials in the monitoring deployment.

```yaml
scrape_configs:
  - job_name: jrtc-gateway
    scheme: https
    metrics_path: /metrics
    scrape_interval: 15s
    authorization:
      type: Bearer
      credentials_file: /run/secrets/gateway-scrape
    tls_config:
      ca_file: /run/secrets/internal-ca.pem
    static_configs:
      - targets: ["gateway-a.internal:8765", "gateway-b.internal:8765"]
```

### Dashboard panels

Use rates over five minutes. For percentile panels, change the first argument below to `0.50`, `0.95` or `0.99`. Prometheus estimates quantiles from the cumulative buckets. The benchmark report uses sorted per-request timings.

```promql
histogram_quantile(0.95, sum by (le) (rate(jrtc_ice_ack_seconds_bucket[5m])))
```

| Panel | Signals and interpretation |
| --- | --- |
| Admission and resume | Graph `sum by (outcome) (rate(jrtc_handshakes_total[5m]))` and `jrtc_resumes_total` the same way. An owner succeeds when it queues the validated admission reply. Early denials before the hello frame count only towards handshakes. Relay attempts and tunnel errors have separate counters. |
| Authorization | Graph `sum by (reason) (rate(jrtc_authorization_errors_total[5m]))`. Reasons use a closed vocabulary. Inspect expired tickets and stale authorizations separately from authority unavailability. |
| ICE and scheduler | Plot the ICE ACK histogram alongside `jrtc_ice_queue_seconds` and `jrtc_event_loop_lag_seconds` using the same percentile query. ACK time runs from validated queue admission to successful reply enqueue. Queue time ends at dispatch-slot acquisition. Also graph `rate(jrtc_ice_dispatch_errors_total[5m])`, active operations and overloads. The loop sampler wakes every 250 ms. |
| Janus capacity | Graph `jrtc_janus_sessions`, `jrtc_janus_handles`, `jrtc_janus_uncertain_handles` and `jrtc_owned_meetings` by instance. Handles include VideoRoom management plugins. A standby also has a Janus session for each meeting in its configured election pool. |
| Broker and dead letters | Graph `jrtc_command_group_lag` (undelivered entries), `jrtc_command_group_pending` (delivered, awaiting ACK), `jrtc_command_reader_up`, `jrtc_command_delivery_age_seconds`, `jrtc_command_quarantine_entries` and `jrtc_command_outcomes_dead`. Redis group gauges become NaN if unreadable. The delivery histogram uses the signed issue time of newly processed commands. The SQLite gauges retain visibility during a broker outage. |
| Permission reconciliation | Graph `sum by (outcome) (rate(jrtc_permission_reconciliation_total[5m]))`, `jrtc_permission_rooms_pending`, `max(synq_gateway_commands_outstanding)` and `max(synq_gateway_oldest_command_seconds)`. The last two read the durable PostgreSQL outbox across all command workers. |
| State delivery | Plot `synq_gateway_sse_live_lag_seconds` and `synq_gateway_sse_replay_lag_seconds` separately. Both measure projection row creation to frame yield in Django. Snapshot repairs and keepalives contribute no observations. Browser receipt and rendering require client measurements. Graph `max by (kind) (synq_gateway_dlq_entries)` for quarantined command outcomes and lifecycle observations. |

### Starting alert thresholds for a rehearsal

These are proposed thresholds. Tune them with cohort traffic and deployment-specific deadlines. Use a volume floor of 100 observations per five minutes for the percentage and latency rules. Treat missing scrapes with a separate `up == 0` alert.

| Signal | Initial threshold | First check |
| --- | --- | --- |
| Admission or resume failures | More than 5% for five minutes | Compare reason counters, authority availability and relay failures. |
| ICE lag | p95 over 100 ms or p99 over 250 ms for five minutes | Compare queue lag, dispatch errors and loop lag. Review fresh grant calls and Janus response times before changing concurrency. |
| Event-loop or queue lag | p99 above 50 ms for five minutes, or repeated dispatch overloads | Check worker CPU, active dispatches and transport backpressure. |
| Janus cleanup | Any uncertain handles for 30 seconds after Janus recovers, or missing expected ready sessions for 15 seconds | Check the owning worker and lease. Confirm disposal of old handles before transferring ownership. |
| Broker delivery | Reader down for 15 seconds, group lag above 100 for a minute, or pending entries persist with rising delivery age | Inspect the consumer and its Redis ACLs. Compare PostgreSQL outbox age with the gateway's group lag. |
| Dead letters or permission cleanup | Retained dead outcomes above zero, a rising quarantine count, or oldest outstanding command above 10 seconds | Inspect authenticated command status and retry disposition. A storage or delivery failure must preserve the permission fence. |
| Live SSE | p95 above two seconds for five minutes with active changes | Compare projection and outbox status, Django polling, client backpressure and proxy buffering. |

### Queue and memory limits

| Boundary | Limit and exhaustion behaviour |
| --- | --- |
| Gateway ingress | Up to 256 connections by default. Auth configuration accepts 1–4,096. A WebSocket frame is at most 128 KiB. The transport queue defaults to 16 frames and accepts at most 64. |
| Per authenticated socket | Eight queued requests, 16 replies and 64 replay entries. Each of the request queue, reply queue and replay cache has a 256 KiB byte budget. Dispatch uses 16 slots per worker and a six-second request budget. Overflow is observable and closes or rejects the affected connection. |
| Janus and ICE | Up to 256 live plus uncertain participant handle bindings per worker, 64 plugin events per participant handle, 128 diagnostic publisher events and 256 candidates or 256 KiB per ICE generation. A multiworker gateway can configure up to 16 meetings. |
| Broker | Each signalling stream accepts at most 4,096 new-or-retained packets at steady state, each at most 32 KiB. At capacity, an atomic Redis operation reclaims only the acknowledged prefix of the expected consumer group. Unread or pending work, a missing group, or an extra group can block append. The durable producer then follows its existing retry policy. An older stream already over the cap stays intact until a safe prefix can be reclaimed. |
| Durable spools | Per gateway, the SQLite command inbox caps at 10,000 commands, quarantine retains up to 1,000 digests, and the lifecycle spool defaults to 1,024 streams. Ten failed outcome publishes retain a dead outcome for reconciliation. |
| Django SSE | 32 streams per process, four per user per process, a 60-second stream lifetime and 128 KiB per frame. Each consumer pulls its next update after the previous yield. |
| Telemetry | Each histogram has 15 finite buckets plus overflow. Counters have fixed dimensions. Sampling uses one loop timer and one serial broker/store reader per configured process. Scrapes render the latest values. No sample queue, meeting label or participant label is allocated. |

The stream cap is per route. Size the dedicated Redis instance for the number of active and retired routes, set a memory limit with `noeviction`, and retain disk headroom for the gateway spools. Redis ACLs for the control producer must permit its existing stream writes and the bounded append script's `EVAL`, `XLEN`, `XINFO GROUPS`, `XPENDING` and `XTRIM` on its signalling routes. Consumers retain their existing read-group and ACK permissions.

### Following a session through logs

Mount the same dedicated trace key on the gateway and Django event consumer. Search gateway JSON for `gateway.admitted` or `gateway.resumed`, then search the existing Django LogVista output for `gateway.session_observed` with the same `meeting_ref` and `session_ref`. Django emits the observation after the transaction commits. The references are domain-separated HMAC-SHA256 values truncated to 128 bits. Rotation starts a new correlation window. The formatter discards arbitrary messages and tracebacks. Tickets, raw identifiers, SDP and candidates have no fields in the trace or metrics.

The [SG-16 verification report](implementation/tracking/reports/SG-16.md) records the measured workload, fault timings and reproduction commands.
