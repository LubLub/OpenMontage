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

When `render_runtime="remotion"` and `composition_mode="atelier"`, call
`remotion_bundle` before `video_compose`. Pass the project directory plus the
exact `proposal_packet`, `scene_plan`, `asset_manifest`, and `edit_decisions`
artifacts. The bundle must succeed and validate before any proxy or full render.
It freezes the Editorial Approval scope, canonical input hashes, project-local
source and props, provenance-matched public assets, Remotion package lock, and
render settings in `artifacts/remotion_bundle.json`.

Treat a bundle failure as a compose blocker. Do not copy an unmanifested file
into the Remotion public directory, relax a hash, or substitute an asset to make
the render proceed. Correct the owning upstream artifact; if that is a material
Editorial Package change, invalidate Editorial Approval through the normal
version-bound workflow.

For Remotion atelier work, use this automated QA ladder in order:

1. Build and validate `remotion_bundle`.
2. Typecheck the project-local composition.
3. Run composition validation.
4. Render one representative still per scene.
5. Render a short proxy using the locked bundle.
6. Render the full output locally.
7. Run Technical Conformance on the exact final bytes.

Technical retries may reuse the same bundle. Any changed source, props, public
asset, runtime lock, or render setting produces a new bundle hash and compose
attempt. Creative or provenance failures return to the owning stage rather
than being repaired through a silent render-time substitution.
