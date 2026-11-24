# SG-14: connection recovery

Django issues `purpose: resume` tickets only for the current participant session
and generation. It rechecks admission, membership and permissions under row locks,
increments the connection generation, and preserves an existing approval. The
browser consumes the ticket with `session.resume`, `sequence: 1` and
`last_sequence: 0`. This starts fresh command and notification sequences; no
media command or old notification is replayed. SSE separately repairs from a
current Django snapshot and its committed cursor.

The gateway installs a worker-local generation high-water mark before awaiting
handoff. It invalidates every unfinished predecessor, waits up to six seconds
for their cleanup, then checks current Django authority and moderation again.
Confirmed Janus detachment must precede replacement media. Unknown attachment or
detachment outcomes retain the existing registry fence and deny media admission.
New peers use fresh handles, SDP/ICE negotiations and observation streams. Late
disconnects cannot remove replacement peers or overwrite Django's newer presence.

Connection generation, authorization epoch and gateway owner generation are
distinct fences. Handoff never grants permissions or revokes a meeting globally.
The worker retains at most 10,000 participant high-water marks and fails closed
at capacity; it never evicts a mark to accept a delayed authority reply.
Multiworker routing, draining and takeover remain SG-15.

Close `4001` identifies ticket expiry so an authenticated browser can obtain a
fresh resume ticket. Close `4009` identifies supersession and is terminal for that
tab. Policy denial (`1008`) remains terminal for admitted media. An invalidated
lobby grant may reconcile only when fresh Django state confirms an admitted state,
the same current session/generation and a newer authorization epoch; the ticket
endpoint still rechecks current permissions. Waiting, denied, removed and
superseded sessions cannot use this path. Temporary failures use the existing
`1012`/`1013` codes. Close reasons contain no credentials or media payloads.

The optional gateway client retains local capture tracks and closes old peer
connections on transport loss. It requests at most six replacement generations,
with 0.5/1/2/4/8/8-second backoff and a 30-second deadline, quietly reconciles Django
state and rebuilds
publisher/subscriber media. Replacement Janus readiness and confirmed old-handle
cleanup may settle separately; the retry window covers that interval while retaining
both fences. Visibility/online wake checks detect expired or dead
sockets. Revocation, supersession and invalid responses stop recovery. If a ticket
POST response is lost, the client cannot know the committed generation and requires
explicit rejoin; it never guesses or silently creates a new admission.

Deployment remains disabled. Roll back the matching gateway, Django dependency
pin and frontend cohort together after draining existing sessions. The legacy
transport and JRTC/plugin dependencies are unchanged.

A meeting-end POST may revoke its own gateway before its HTTP acknowledgement.
The client completes that POST and final Django snapshot within one bounded HTTP
deadline (15 seconds by default), without replay. The snapshot must be ended;
responses and pending credential lookups cannot cross into a newly joined lifetime.
Ordinary requests still cancel with teardown.

Exact revisions and acceptance evidence are in the
[SG-14 report](implementation/tracking/reports/SG-14.md).
