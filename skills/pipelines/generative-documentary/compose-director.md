# Compose Director — Generative Documentary

Render the approved edit with the locked runtime and produce a schema-valid
`render_report` that factually describes the output. Before compose can pass,
run fail-closed Technical Conformance against the exact rendered bytes: file
integrity, duration, resolution, required asset identities and hashes, broken
and frozen frames, unintended silence, unsafe peaks, and narration timeline
alignment. Persist the schema-valid `technical_conformance` beside the render
report and bind it to the compose attempt, render hash, policy hash, and any
Manual Rescue evidence. Deterministic fixtures may exercise the same analyzers
but are never Production Proof.

Route composition by the proposal's exact `render_runtime`. If Remotion or
HyperFrames was selected but is unavailable, stop and surface the blocker;
never substitute another runtime silently.
