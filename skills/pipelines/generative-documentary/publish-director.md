# Publish Director — Generative Documentary

Use `export_bundle` to prepare an offline handoff only. Build a strict
`release_package` containing the exact packaged video,
title, thumbnail, description, disclosures, and publishing destination, plus a
schema-valid `publish_log` with status `exported`. No upload or network publish
belongs in this stage.

Write the publish checkpoint as `awaiting_human` and stop. After Release
Approval, rewrite it `completed` with
`human_approved=True`. Approval grants permission for a separate explicit
publication action; it does not perform that action.
