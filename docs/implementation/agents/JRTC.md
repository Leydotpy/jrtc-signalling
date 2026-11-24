# JRTC/JRTC-video agents — library boundaries
- Investigate first; change generic packages **only** when strictly needed for async gateway support. Prefer existing session manager, runtime, plugin, trickle and event APIs.
- JRTC owns generic transport sessions, transactions, plugin handles and nonblocking global-event hooks, not Django/meeting auth/Broka domain schemas.
- Preserve callback/event delivery and transaction resolution ordering; no DB/broker await in Janus receive loop. Bounded queues and cancellation required.
- Janus IDs remain `int` in Python, decimal strings in browser wire. ICE batch final candidate correctness and unsolicited JSEP preserved.
- For `jrtc-video`, test VideoRoom join/publish/subscribe, reusable management handles, detach, recovery and session loss; avoid broad refactor.
