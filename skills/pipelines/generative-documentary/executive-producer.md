# Executive Producer — Generative Documentary

Drive the manifest's eight stages in order and treat its two approval flags as
binding. `scene_plan` presents the current pre-production artifacts at the
Editorial Approval gate as one versioned Editorial Package; `publish` presents
the offline Release Package. The `assets` stage must not begin until the
Editorial Approval receipt matches the current Editorial Package version and
content hash. No other stage invents a human gate. Resume with `get_next_stage`,
keep every
artifact in the project workspace, record provider costs, and never replace an approved provider,
model, or render path silently.

Immediately before asset generation, verify that the Editorial Approval receipt
is present and matches the current Editorial Package ID, version, and content
hash. Refuse the stage if the receipt is absent, stale, or scoped to different
content. Copy that exact scope into the Premium `asset_manifest.approval_scope`.

This is a research-and-generation pipeline, not a stock montage. Historical
source material may be used as an authored Historical Anchor, but retrieval,
clip ranking, and corpus size are not pipeline assumptions. Channel tone,
subject, pacing, voice identity, and evidence thresholds come from the Channel
Snapshot and playbook.
