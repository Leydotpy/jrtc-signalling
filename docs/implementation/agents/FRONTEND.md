# synq.js agent — signaling transport + UI state
- Discover existing socket/SDK/event name contracts first and preserve them through adapter; avoid UI restyle and unrelated P0 changes.
- Connect Django HTTPS for auth/admin and SSE for committed state, gateway WebSocket for SDP/ICE, Janus for media.
- Maintain ICE batch/final-completion ordering; exact JSEP correlation and loss recovery; browser Janus IDs are decimal strings.
- Treat admission approval separate from connected; bind frontend participant store updates to event/version and reconcile gap via fresh Django snapshot.
- Expose feature flag to select legacy path per meeting and prevent dual-active signaling for same participant.
- Test lobby, direct join, rejoin, host removal, meeting termination, SSE reconnect, TURN-only and publisher/subscriber flows.
