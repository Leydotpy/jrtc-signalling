# Ordered ICE and restart rehearsal (SG-09)

This extends the explicitly gated [media rehearsal](MEDIA_SIGNALLING.md).
The default flag, production readiness and legacy client routing are unchanged.

## Browser boundary

Canonical `media.trickle` payload:

```json
{
  "handle_type": "publisher",
  "negotiation_id": "the-accepted-offer-id",
  "candidates": [{
    "candidate": "candidate:1 1 udp 1 192.0.2.1 5000 typ host",
    "sdpMid": "0",
    "sdpMLineIndex": 0,
    "usernameFragment": "theSDPfragment"
  }],
  "completed": false
}
```

Wait for the publisher offer result or subscriber `start` result before trickling.
The bounded client batcher may collect candidates while that negotiation is in
flight. The gateway adds no queue for candidates awaiting unknown SDP.

The decoder temporarily accepts singular `candidate`, converts it into a one-item
array and defaults its omitted `completed` to false. Mixed singular/plural input
rejects. Exported schemas and internal validation stay strictly plural. This is
the only legacy shim; it does not make the existing Socket.IO client a gateway
client. The SG-12 adapter must supply authenticated envelope and negotiation IDs.

An empty array requires boolean `completed=true`. A request permits at most 32
candidates and 64 KiB of compact UTF-8 JSON, within the 128 KiB frame limit.
Whole-batch syntax, exact integer indices, candidate grammar and size are checked
before authority/SDK work. Every candidate must name a negotiated active MID or
index; if both are supplied they must agree. Optional `usernameFragment` and
candidate-line `ufrag` must agree with each other and the accepted browser SDP.
The SDK does not accept `usernameFragment`, so it is removed only after checking.
Legacy candidates without either fragment rely on their authenticated connection,
current negotiation ID and MID/index; a batcher generation counter is not an
authorization or ICE-credential fence.

## Ordering and failures

One request worker and the peer/owner locks serialize both roles. One nonempty
browser batch becomes **one plural JRTC `trickle` operation**, including a single
candidate. For `completed=true`, its ACK must arrive before `complete_trickle`.
An empty completion invokes only `complete_trickle`. The combined operation has
one three-second SDK deadline, further limited by lease expiry. Authority revision,
ownership and live session/handle checks also run between the two sends.

Cancellation, timeout, error ACK or lost ownership cannot trigger completion or
automatic replay. An uncertain outcome disposes the handle and closes the socket;
the client must obtain a fresh authorized connection. Exact request replay uses
the existing bounded response cache. Repeated empty completion after a confirmed
completion is a no-op; later candidates reject. Candidates embedded in SDP with
`end-of-candidates` also close that generation's trickle window.

Forwarded trickle is limited to 256 candidates and 256 KiB per ICE generation.
Ordinary SDP renegotiation with unchanged credentials preserves completion and
budgets. Only fresh ICE credentials reset them. Retired generation fingerprints
are bounded and cannot be revived by choosing another wire negotiation ID.

## Restart and JSEP

Send `media.ice_restart` with only `handle_type`:

| Role | Result and next step |
| --- | --- |
| Publisher | Returns `action=ice_restart`, a reserved `negotiation_id`, and no JSEP. Create a browser ICE-restart offer, then call publisher configure/publish with that ID and permitted tracks. |
| Subscriber | Calls Janus `configure_subscriber(restart=true)` and returns its offer with a new ID. Apply the offer, create an answer, and call subscriber start with that ID. |

Both new browser ICE username fragment and password must differ from the previous
generation. Publisher offer-driven restart without the explicit reservation also
works with a fresh negotiation ID and fresh credentials. Trickle is blocked while
the restart offer/answer is pending; old IDs and credentials reject. Subscriber
restart offers must also have fresh Janus credentials. No role accepts arbitrary
client-selected Janus session, handle, feed or room IDs.

Correlated JSEP is returned once. Its exact transaction/type/SDP hash suppresses
the SDK callback, including delayed callbacks after restart. Conflicting content
under a known transaction fails closed. Valid unsolicited subscriber offers are
processed, allocated new negotiation IDs and bound to their stream layout; an
unsolicited restart requires fresh browser credentials in the answer. Retired
server credentials cannot become a new subscriber offer. Unsolicited publisher
answers must match the currently accepted server ICE generation. Replaced handles
or SDK sessions cannot deliver into a new handle. Browser Janus IDs remain decimal
strings, including values above JavaScript's safe integer range.

## Compatibility evidence

`scripts/check_client_ice.cjs` executes the actual batcher at synq.js commit
`6521187857da9f8d9223e67b445d71114e935ab0`, preserves the 25 ms / 16-candidate
defaults, and runs its 16 upstream tests. Four generated fixture cases include
single, 16, 16-plus-final-three and empty completion. Gateway tests consume these
fixtures for both roles and compare the exact Janus wire batches. The Django
compatibility runner checks them against Synq's actual `normalize_ice_request`.
No Synq, synq.js or SDK source is modified.

```sh
node --experimental-transform-types scripts/check_client_ice.cjs /path/to/pinned/synq.js
```

Run with Node 24 in a workspace authorized to read the pinned private client
source. The gateway CI token cannot read that repository, so CI consumes the
generated fixtures across the real SDK in every Python matrix job. The generator's
`--write` mode is for deliberate baseline updates; normal validation also checks
the source SHA-256. Real service
coverage and its test-only aiortc adapters are described in
[media verification](MEDIA_SIGNALLING.md#verification-and-limits).

Primary protocol references: [Janus VideoRoom ICE restart](https://janus.conf.meetecho.com/docs/videoroom.html),
[Janus trickle API](https://janus.conf.meetecho.com/docs/rest.html),
[ICE SDP syntax](https://www.rfc-editor.org/rfc/rfc8839.html),
[coturn configuration](https://github.com/coturn/coturn/blob/master/examples/etc/turnserver.conf).
