# Asset Director — Generative Documentary

Generate narration, Historical Anchors, Generated Reconstructions, local-motion
instruction files, music, and only the hero video selected in the approved
shotlist. Use explicit project-local output paths and the cost ledger. A Dry Run
uses only the fixed deterministic fake-provider set and records zero cost.

Before calling any provider, verify that the current Editorial Approval receipt
matches the Editorial Package ID, version, and content hash. Refuse an absent or
stale approval. Write that exact identity to `asset_manifest.approval_scope` and
set `profile=provenance-aware-documentary-v1`.

For every asset, record its SHA-256, scene linkage, visual type, source tool,
provider/model where applicable, provenance, and reconstruction status. Record
motion treatment for visual assets; do not invent one for narration, music, or
thumbnails. Historical Anchors must retain their source title/URL and either a
license record or public-domain basis, plus the `source_id` from the approved
shot plan. A Dry Run Historical Anchor is marked as a deterministic
`fixture_proxy`, never as the archival source itself. Generated Reconstructions must record
their generation provenance and `represented_as_archival=false`. Local-motion
outputs must identify the still they derive from. Do not generate video for a
scene unless the approved shotlist marks it as a hero moment, and record the
approved provider/model on its `generated_video` motion treatment. A provider/model
substitution changes the approved production scope: stop and return to
Editorial Approval rather than silently falling back.

The JSON schemas validate each artifact's structural vocabulary. They do not
authorize production on their own: the host must also enforce cross-artifact
approval equality, scene/derivative linkage, provider identity, role coherence,
and file integrity before generation and again before edit.
