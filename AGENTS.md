# JRTC signalling server

This repository implements the dedicated Python ASGI gateway from the
[27 October instruction pack](docs/implementation/AGENTS.md). Read that pack,
its execution order, contracts, task reports and progress tracker before work.
Run tasks sequentially; keep each concern in its own commit. Record verification
and external prerequisites honestly. The original pack is retained under
`docs/implementation`; its MANIFEST describes the original imported files.

Scope is this repository. Inspect other repositories for compatibility, but do
not change their code without an expanded user instruction. Django owns admission
and durable permissions. This service must not import Django settings/models.
Keep SDP/ICE off Broka, credentials out of logs, and unimplemented access paths
closed. No production rollout or removal of the legacy path is authorized.

Implementation contracts live in `src/jrtc_signalling/schemas/v1` and `docs/`;
the imported draft contracts remain historical source material. Do not mark an
implementation task done using documentation or mock integration alone.
