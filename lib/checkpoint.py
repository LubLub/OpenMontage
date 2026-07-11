"""Checkpoint writer/reader for pipeline state persistence.

Each stage writes a checkpoint after completion. The orchestrator uses
checkpoints to resume pipelines and to present state at human checkpoints.
"""

from __future__ import annotations

import json
import os
import stat
import uuid
from functools import lru_cache
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import jsonschema

from schemas.artifacts import ARTIFACT_NAMES, validate_artifact

# All known stages across all pipelines (used only for artifact name lookup).
ALL_KNOWN_STAGES = frozenset([
    "research", "proposal", "idea", "script", "scene_plan",
    "assets", "edit", "compose", "publish",
])

# Backward-compatible alias — existing code / tests that import STAGES still work.
# New code should use get_pipeline_stages(pipeline_type) instead.
STAGES = ["research", "proposal", "idea", "script", "scene_plan",
          "assets", "edit", "compose", "publish"]

CANONICAL_STAGE_ARTIFACTS = {
    "research": "research_brief",
    "proposal": "proposal_packet",
    "idea": "brief",
    "script": "script",
    "scene_plan": "scene_plan",
    "assets": "asset_manifest",
    "edit": "edit_decisions",
    "compose": "render_report",
    "publish": "publish_log",
}

# Additional artifacts that may be produced alongside canonical ones.
# These are not stage-defining but are required by governance contracts.
SUPPLEMENTARY_ARTIFACTS = {
    "claim_ledger",         # Claim-to-evidence record carried from research through approval
    "editorial_package",    # Versioned pre-production scope presented at Editorial Approval
    "source_media_review",  # Required before first planning stage when user media exists
    "final_review",         # Required by compose stage before presenting to user
    "video_analysis_brief", # Reference-video grounding artifact carried alongside stages
    "release_package",      # Deterministic offline package presented at the publish gate
    "technical_conformance", # Fail-closed automated review of one exact render/policy pair
}


def get_pipeline_stages(pipeline_type: str | None) -> list[str]:
    """Return the ordered stage list for a specific pipeline.

    Falls back to STAGES (deterministic canonical order) when pipeline_type
    is not provided or the manifest cannot be loaded.

    Previous versions used a set intersection here, which produced
    nondeterministic ordering. The fallback now uses a stable list.
    """
    if pipeline_type is None:
        # Deterministic canonical fallback — sorted to ensure stable ordering
        import logging
        logging.getLogger(__name__).warning(
            "get_pipeline_stages called without pipeline_type — "
            "using canonical fallback order. Pass pipeline_type for correctness."
        )
        return list(STAGES)

    try:
        from lib.pipeline_loader import load_pipeline_readonly, get_stage_order
        manifest = load_pipeline_readonly(pipeline_type)
        return get_stage_order(manifest)
    except (FileNotFoundError, Exception):
        # Graceful fallback: return all known stages in canonical order
        return list(STAGES)

CHECKPOINT_SCHEMA_PATH = (
    Path(__file__).resolve().parent.parent
    / "schemas"
    / "checkpoints"
    / "checkpoint.schema.json"
)

# Canonical project root. Checkpoints, artifacts, and the project marker all
# live under PROJECTS_DIR/<project_id>/ — this is the location the Backlot
# board watches. Callers may still pass a different pipeline_dir (tests do),
# but production runs should use the default.
from lib.paths import PROJECTS_DIR  # noqa: E402  (single source of truth)

PROJECT_MARKER_FILENAME = "project.json"
HISTORY_DIRNAME = "history"


class CheckpointValidationError(ValueError):
    """Raised when a checkpoint or its canonical artifacts are invalid."""


@lru_cache(maxsize=1)
def _load_checkpoint_schema() -> dict[str, Any]:
    with open(CHECKPOINT_SCHEMA_PATH) as f:
        return json.load(f)


def _validate_artifacts_for_stage(
    stage: str,
    status: str,
    artifacts: dict[str, Any],
) -> None:
    required_artifact = CANONICAL_STAGE_ARTIFACTS[stage]
    if status in {"completed", "awaiting_human"} and required_artifact not in artifacts:
        raise CheckpointValidationError(
            f"Stage {stage!r} with status {status!r} must include "
            f"canonical artifact {required_artifact!r}"
        )

    for artifact_name, artifact_data in artifacts.items():
        if artifact_name not in ARTIFACT_NAMES:
            continue
        if not isinstance(artifact_data, dict):
            raise CheckpointValidationError(
                f"Artifact {artifact_name!r} must be a JSON object matching its schema"
            )
        try:
            validate_artifact(artifact_name, artifact_data)
        except Exception as exc:
            raise CheckpointValidationError(
                f"Artifact {artifact_name!r} failed schema validation: {exc}"
            ) from exc


def validate_checkpoint(checkpoint: dict[str, Any]) -> None:
    """Validate checkpoint structure and canonical artifact payloads.

    Uses pipeline_type (if present) to resolve the valid stage list.
    Falls back to ALL_KNOWN_STAGES when pipeline_type is absent.
    """
    stage = checkpoint.get("stage")
    status = checkpoint.get("status")
    artifacts = checkpoint.get("artifacts")
    pipeline_type = checkpoint.get("pipeline_type")

    valid_stages = (
        set(get_pipeline_stages(pipeline_type)) if pipeline_type
        else ALL_KNOWN_STAGES
    )

    if not isinstance(stage, str) or stage not in valid_stages:
        raise CheckpointValidationError(
            f"Invalid stage: {stage!r} for pipeline {pipeline_type!r}. "
            f"Valid stages: {sorted(valid_stages)}"
        )
    if not isinstance(status, str):
        raise CheckpointValidationError(f"Invalid status: {status!r}")
    if not isinstance(artifacts, dict):
        raise CheckpointValidationError("Checkpoint artifacts must be a dictionary")

    _validate_artifacts_for_stage(stage, status, artifacts)

    try:
        jsonschema.validate(instance=checkpoint, schema=_load_checkpoint_schema())
    except jsonschema.ValidationError as exc:
        raise CheckpointValidationError(f"Checkpoint failed schema validation: {exc.message}") from exc


def _validate_path_component(value: str, *, label: str) -> None:
    if (
        not isinstance(value, str)
        or not value
        or value in {".", ".."}
        or "/" in value
        or "\\" in value
        or "\x00" in value
        or Path(value).is_absolute()
    ):
        raise CheckpointValidationError(f"Unsafe checkpoint {label}: {value!r}")


def _reject_symlink_components(path: Path, *, label: str) -> Path:
    """Reject symlinks in every existing component without resolving through them."""
    absolute = Path(os.path.abspath(path))
    current = Path(absolute.anchor)
    for component in absolute.parts[1:]:
        current /= component
        try:
            component_stat = current.lstat()
        except FileNotFoundError:
            break
        except OSError as exc:
            raise CheckpointValidationError(
                f"Unsafe checkpoint {label}: {path}"
            ) from exc
        if stat.S_ISLNK(component_stat.st_mode):
            raise CheckpointValidationError(
                f"Unsafe checkpoint {label} contains symlink traversal: {path}"
            )
    return absolute


def _validated_project_directory(pipeline_dir: Path, project_id: str) -> Path:
    _validate_path_component(project_id, label="project_id")
    root = _reject_symlink_components(Path(pipeline_dir), label="pipeline root")
    project_dir = _reject_symlink_components(
        root / project_id,
        label="project directory",
    )
    if project_dir.parent != root:
        raise CheckpointValidationError(
            f"Unsafe checkpoint project_id escapes pipeline root: {project_id!r}"
        )
    return project_dir


def _checkpoint_path(pipeline_dir: Path, project_id: str, stage: str) -> Path:
    _validate_path_component(stage, label="stage")
    return _validated_project_directory(pipeline_dir, project_id) / f"checkpoint_{stage}.json"


def init_project(
    project_id: str,
    *,
    title: str,
    pipeline_type: str,
    pipeline_dir: Optional[Path] = None,
    style_playbook: Optional[str] = None,
) -> Path:
    """Initialize a project workspace with the canonical layout + marker file.

    Creates projects/<project_id>/ with the standard subdirectories and writes
    project.json — the marker the Backlot board uses to render a project's
    identity and stage rail before the first checkpoint exists.

    Idempotent: re-running preserves the original created_at and merges fields.
    Returns the project directory.
    """
    base = pipeline_dir or PROJECTS_DIR
    project_dir = base / project_id
    for sub in (
        "artifacts",
        "assets/images",
        "assets/video",
        "assets/audio",
        "assets/music",
        "renders",
    ):
        (project_dir / sub).mkdir(parents=True, exist_ok=True)

    marker_path = project_dir / PROJECT_MARKER_FILENAME
    marker: dict[str, Any] = {}
    if marker_path.exists():
        try:
            with open(marker_path) as f:
                marker = json.load(f)
        except (json.JSONDecodeError, OSError):
            marker = {}

    marker.setdefault("version", "1.0")
    marker.setdefault("created_at", datetime.now(timezone.utc).isoformat())
    marker["project_id"] = project_id
    marker["title"] = title
    marker["pipeline_type"] = pipeline_type
    if style_playbook is not None:
        marker["style_playbook"] = style_playbook

    with open(marker_path, "w") as f:
        json.dump(marker, f, indent=2)

    return project_dir


def _stage_requires_approval(pipeline_type: Optional[str], stage: str) -> Optional[bool]:
    """Read human_approval_default for a stage from its pipeline manifest.

    Returns None when the stage isn't declared in the manifest or no
    pipeline_type was given — the caller then falls back to the value the
    agent passed in.

    A *provided but unknown* pipeline_type raises: a typo must not silently
    disable gate enforcement (fail-closed, not fail-open). Other manifest
    load failures are logged and fall back — a corrupt manifest shouldn't
    strand an otherwise-valid run, but the degradation must be visible.
    """
    if not pipeline_type or pipeline_type == "unknown":
        return None
    from lib.pipeline_loader import get_stage_human_approval_default, load_pipeline_readonly
    try:
        manifest = load_pipeline_readonly(pipeline_type)
    except FileNotFoundError:
        raise CheckpointValidationError(
            f"Unknown pipeline_type {pipeline_type!r} — cannot resolve gate "
            f"policy for stage {stage!r}. Check the spelling against "
            f"pipeline_defs/*.yaml."
        )
    except Exception as exc:
        import logging
        logging.getLogger(__name__).warning(
            "Gate policy unavailable for pipeline %r (%s) — falling back to "
            "the caller's human_approval_required flag.", pipeline_type, exc,
        )
        return None
    return get_stage_human_approval_default(manifest, stage)


def _archive_superseded_checkpoint(path: Path, stage: str) -> None:
    """Copy an existing checkpoint into history/ before it is overwritten.

    Preserves the full run record: stage re-runs (script v1 → v2) and gate
    transitions (awaiting_human → completed) remain reconstructable. Repeated
    in_progress refreshes are NOT archived — they are partial-progress
    heartbeats, not versions.

    Archiving is best-effort and must never crash a checkpoint write: the
    Backlot watcher may hold the file open (Windows denies renames of open
    files), so we copy rather than move, and swallow archival I/O failures.
    """
    if not path.exists():
        return
    existing = _read_json_regular_file(path)
    if existing.get("status") == "in_progress":
        return

    try:
        stamp = str(existing.get("timestamp", ""))
        safe_stamp = "".join(c for c in stamp if c.isalnum()) or f"{path.stat().st_mtime_ns}"
        history_dir = path.parent / HISTORY_DIRNAME
        history_dir.mkdir(parents=True, exist_ok=True)
        target = history_dir / f"checkpoint_{stage}_{safe_stamp}.json"
        if target.exists():
            target = history_dir / f"checkpoint_{stage}_{safe_stamp}_{path.stat().st_mtime_ns}.json"
        _write_durable_json(target, existing)
    except CheckpointValidationError:
        raise
    except OSError:
        import logging
        logging.getLogger(__name__).warning(
            "Could not archive superseded checkpoint %s to history/", path
        )


def _decision_log_path(pipeline_dir: Path, project_id: str) -> Path:
    return pipeline_dir / project_id / "decision_log.json"


def _merge_decision_log(
    pipeline_dir: Path, project_id: str, new_log: dict[str, Any]
) -> None:
    """Append new decisions to the project-level decision log.

    Each stage may produce decisions. This function merges them into a
    single cumulative file so reviewers and the bench can inspect the
    full audit trail.
    """
    path = _decision_log_path(pipeline_dir, project_id)
    if path.exists():
        with open(path) as f:
            existing = json.load(f)
    else:
        existing = {
            "version": "1.0",
            "project_id": project_id,
            "decisions": [],
        }

    existing_ids = {d["decision_id"] for d in existing.get("decisions", [])}
    for decision in new_log.get("decisions", []):
        if decision.get("decision_id") not in existing_ids:
            existing["decisions"].append(decision)

    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(existing, f, indent=2)


def write_checkpoint(
    pipeline_dir: Path,
    project_id: str,
    stage: str,
    status: str,
    artifacts: dict[str, Any],
    *,
    pipeline_type: Optional[str] = None,
    style_playbook: Optional[str] = None,
    checkpoint_policy: str = "guided",
    human_approval_required: bool = False,
    human_approved: bool = False,
    review: Optional[dict] = None,
    cost_snapshot: Optional[dict] = None,
    error: Optional[str] = None,
    metadata: Optional[dict] = None,
    input_fingerprint: Optional[str] = None,
    input_scope: Optional[dict[str, Any]] = None,
    output_fingerprint: Optional[str] = None,
    attempt_id: Optional[str] = None,
) -> Path:
    """Write a checkpoint file for a pipeline stage."""
    # Backfill a missing pipeline_type from the project marker so that
    # omitting the kwarg doesn't quietly bypass gate enforcement.
    if not pipeline_type:
        marker = None
        marker_path = pipeline_dir / project_id / PROJECT_MARKER_FILENAME
        if marker_path.exists():
            try:
                with open(marker_path) as f:
                    marker = json.load(f)
            except (json.JSONDecodeError, OSError):
                marker = None
        if isinstance(marker, dict) and marker.get("pipeline_type"):
            pipeline_type = marker["pipeline_type"]

    valid_stages = (
        set(get_pipeline_stages(pipeline_type)) if pipeline_type
        else ALL_KNOWN_STAGES
    )
    if stage not in valid_stages:
        raise ValueError(
            f"Invalid stage: {stage!r} for pipeline {pipeline_type!r}. "
            f"Valid stages: {sorted(valid_stages)}"
        )

    # --- Gate enforcement (GI-4) ---
    # The pipeline manifest is the binding source of truth for whether a stage
    # gates on human approval; a caller may gate MORE strictly (e.g. a
    # manual_all checkpoint policy) but never less. A gated stage can only be
    # written "completed" with explicit evidence of approval
    # (human_approved=True). Skipping a gate is a hard error.
    #
    # Enforcement happens at write time only: pre-existing checkpoints written
    # before gating (or by hand) still read as completed — deliberate
    # back-compat so in-flight and legacy projects keep resuming.
    manifest_gate = _stage_requires_approval(pipeline_type, stage)
    gated = bool(manifest_gate) or human_approval_required
    if gated:
        human_approval_required = True
        if status == "completed" and not human_approved:
            gate_source = (
                f"human_approval_default: true in the {pipeline_type!r} manifest"
                if manifest_gate
                else "human_approval_required=True was passed by the caller"
            )
            raise CheckpointValidationError(
                f"GATE VIOLATION: stage {stage!r} requires human approval "
                f"({gate_source}) but status='completed' was written without "
                f"human_approved=True. Correct protocol: write "
                f"status='awaiting_human', present the artifact summary to the "
                f"user, END YOUR TURN, and only after the user approves "
                f"re-write with status='completed', human_approved=True."
            )

    checkpoint = {
        "version": "1.0",
        "project_id": project_id,
        "pipeline_type": pipeline_type or "unknown",
        "stage": stage,
        "status": status,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "checkpoint_policy": checkpoint_policy,
        "human_approval_required": human_approval_required,
        "human_approved": human_approved,
        "artifacts": artifacts,
    }
    if style_playbook is not None:
        checkpoint["style_playbook"] = style_playbook
    if review is not None:
        checkpoint["review"] = review
    if cost_snapshot is not None:
        checkpoint["cost_snapshot"] = cost_snapshot
    if error is not None:
        checkpoint["error"] = error
    if metadata is not None:
        checkpoint["metadata"] = metadata
    if input_fingerprint is not None:
        checkpoint["input_fingerprint"] = input_fingerprint
    if input_scope is not None:
        checkpoint["input_scope"] = input_scope
    if output_fingerprint is not None:
        checkpoint["output_fingerprint"] = output_fingerprint
    if attempt_id is not None:
        checkpoint["attempt_id"] = attempt_id

    # Merge decision_log: if this checkpoint carries new decisions,
    # append them to the project-level decision log file, then write the
    # reference back into relevant artifacts so downstream consumers can find it.
    if "decision_log" in artifacts and isinstance(artifacts["decision_log"], dict):
        _merge_decision_log(pipeline_dir, project_id, artifacts["decision_log"])
        # Artifact references are project-relative so checkpoints remain portable
        # across worktrees, project roots, and deterministic replay runs.
        log_ref = "decision_log.json"

        # Write decision_log_ref into proposal_packet and render_report
        # artifacts if they are present in this checkpoint.
        for artifact_key in ("proposal_packet", "render_report"):
            if artifact_key in artifacts and isinstance(artifacts[artifact_key], dict):
                plan_or_top = artifacts[artifact_key]
                # proposal_packet stores it under production_plan
                if artifact_key == "proposal_packet":
                    plan = plan_or_top.get("production_plan")
                    if isinstance(plan, dict):
                        plan["decision_log_ref"] = log_ref
                else:
                    plan_or_top["decision_log_ref"] = log_ref

    validate_checkpoint(checkpoint)

    path = _checkpoint_path(pipeline_dir, project_id, stage)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Serialize to a temp file first so a mid-write failure (disk full,
    # unserializable metadata) can never leave the stage with a truncated
    # current checkpoint; then archive the superseded file and swap in the
    # new one atomically.
    # Preserve run history: a superseded completed/awaiting_human checkpoint
    # is copied to history/ (stage versioning, gate audit trail, replay).
    _archive_superseded_checkpoint(path, stage)
    _write_durable_json(path, checkpoint)

    return path


def _open_safe_directory(path: Path) -> int:
    """Open a real directory without following a final symlink component."""
    path.mkdir(parents=True, exist_ok=True)
    try:
        directory_stat = path.lstat()
    except OSError as exc:
        raise CheckpointValidationError(f"Unsafe checkpoint directory: {path}") from exc
    if not stat.S_ISDIR(directory_stat.st_mode) or path.is_symlink():
        raise CheckpointValidationError(f"Unsafe checkpoint directory: {path}")
    directory_flag = getattr(os, "O_DIRECTORY", 0)
    if not directory_flag:
        # Windows does not expose durable directory descriptors. The caller
        # still uses exclusive random temp files and rechecks this path.
        return -1
    flags = os.O_RDONLY | directory_flag | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise CheckpointValidationError(f"Unsafe checkpoint directory: {path}") from exc
    file_stat = os.fstat(descriptor)
    if not stat.S_ISDIR(file_stat.st_mode):
        os.close(descriptor)
        raise CheckpointValidationError(f"Unsafe checkpoint directory: {path}")
    return descriptor


def _fsync_directory_descriptor(descriptor: int) -> None:
    """Flush directory metadata where the platform exposes that operation."""
    if descriptor < 0 or not getattr(os, "O_DIRECTORY", 0):
        return
    os.fsync(descriptor)


def _read_json_regular_file(path: Path) -> dict[str, Any]:
    """Read JSON from one non-symlink, single-link regular file."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = -1
    try:
        descriptor = os.open(path, flags)
        file_stat = os.fstat(descriptor)
        if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_nlink != 1:
            raise CheckpointValidationError(
                f"Checkpoint is not a safe single-link regular file: {path}"
            )
        with os.fdopen(descriptor, encoding="utf-8") as handle:
            descriptor = -1
            document = json.load(handle)
    except CheckpointValidationError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CheckpointValidationError(f"Checkpoint is unavailable or unsafe: {path}") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if not isinstance(document, dict):
        raise CheckpointValidationError(f"Checkpoint must be a JSON object: {path}")
    return document


def _write_durable_json(path: Path, document: dict[str, Any]) -> None:
    """Atomically persist and flush a JSON document before returning."""
    directory = _open_safe_directory(path.parent)
    temporary = f".{path.name}.{uuid.uuid4().hex}.tmp"
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
    )
    file_descriptor = -1
    temporary_created = False
    try:
        if directory >= 0:
            file_descriptor = os.open(temporary, flags, 0o600, dir_fd=directory)
        else:
            file_descriptor = os.open(path.parent / temporary, flags, 0o600)
        temporary_created = True
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as handle:
            file_descriptor = -1
            json.dump(document, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        if directory >= 0 and os.replace in os.supports_dir_fd:
            os.replace(
                temporary,
                path.name,
                src_dir_fd=directory,
                dst_dir_fd=directory,
            )
        else:
            # The temp was created exclusively in the already-validated target
            # directory, so pathname replacement cannot follow a hostile temp.
            os.replace(path.parent / temporary, path)
        _fsync_directory_descriptor(directory)
    finally:
        if file_descriptor >= 0:
            os.close(file_descriptor)
        if temporary_created:
            try:
                if directory >= 0:
                    os.unlink(temporary, dir_fd=directory)
                else:
                    os.unlink(path.parent / temporary)
            except FileNotFoundError:
                pass
        if directory >= 0:
            os.close(directory)


def supersede_checkpoint(
    pipeline_dir: Path,
    project_id: str,
    stage: str,
    *,
    reason: str,
    replacement_input_fingerprint: str | None,
) -> Path:
    """Durably archive an invalidated checkpoint before removing the current file.

    The caller decides that a checkpoint is stale. This helper only performs the
    structural transition and fails closed: an archive or validation failure
    leaves the current checkpoint in place.
    """
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("Supersession reason must be a non-empty string")

    current_path = _checkpoint_path(pipeline_dir, project_id, stage)
    checkpoint = read_checkpoint(pipeline_dir, project_id, stage)
    if checkpoint is None:
        raise FileNotFoundError(f"Checkpoint not found: {current_path}")

    superseded_at = datetime.now(timezone.utc).isoformat()
    archived = dict(checkpoint)
    archived["status"] = "superseded"
    archived["supersession"] = {
        "reason": reason.strip(),
        "previous_status": checkpoint["status"],
        "replacement_input_fingerprint": replacement_input_fingerprint,
        "pending_recalculation": replacement_input_fingerprint is None,
        "superseded_at": superseded_at,
    }
    validate_checkpoint(archived)

    safe_stamp = "".join(character for character in superseded_at if character.isalnum())
    history_path = (
        current_path.parent
        / HISTORY_DIRNAME
        / f"checkpoint_{stage}_{safe_stamp}_{uuid.uuid4().hex[:12]}_superseded.json"
    )
    _write_durable_json(history_path, archived)

    # Do not remove a checkpoint another writer replaced while the archive was
    # being flushed. The durable audit record remains useful; current state wins.
    current = read_checkpoint(pipeline_dir, project_id, stage)
    if current != checkpoint:
        raise RuntimeError("Current checkpoint changed while it was being superseded")
    current_path.unlink()
    directory = _open_safe_directory(current_path.parent)
    try:
        _fsync_directory_descriptor(directory)
    finally:
        if directory >= 0:
            os.close(directory)
    return history_path


def read_checkpoint(
    pipeline_dir: Path, project_id: str, stage: str
) -> Optional[dict[str, Any]]:
    """Read a checkpoint file. Returns None if not found."""
    path = _checkpoint_path(pipeline_dir, project_id, stage)
    if not path.exists():
        return None
    checkpoint = _read_json_regular_file(path)
    validate_checkpoint(checkpoint)
    return checkpoint


def get_latest_checkpoint(
    pipeline_dir: Path, project_id: str
) -> Optional[dict[str, Any]]:
    """Find the most recent checkpoint for a project (by file mtime)."""
    project_dir = _validated_project_directory(pipeline_dir, project_id)
    if not project_dir.exists():
        return None

    checkpoints_with_times: list[tuple[Path, float]] = []
    for path in project_dir.glob("checkpoint_*.json"):
        file_stat = path.lstat()
        if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_nlink != 1:
            raise CheckpointValidationError(
                f"Checkpoint is not a safe single-link regular file: {path}"
            )
        checkpoints_with_times.append((path, file_stat.st_mtime))
    checkpoints = [
        path
        for path, _ in sorted(
            checkpoints_with_times,
            key=lambda item: item[1],
            reverse=True,
        )
    ]
    if not checkpoints:
        return None

    stage = checkpoints[0].name.removeprefix("checkpoint_").removesuffix(".json")
    return read_checkpoint(pipeline_dir, project_id, stage)


def get_completed_stages(
    pipeline_dir: Path, project_id: str, pipeline_type: str | None = None
) -> list[str]:
    """Return list of stages that have a completed checkpoint.

    When pipeline_type is provided, only checks stages defined in that
    pipeline's manifest — preventing false positives from leftover
    checkpoints of a different pipeline type.
    """
    stages_to_check = get_pipeline_stages(pipeline_type)
    completed = []
    for stage in stages_to_check:
        cp = read_checkpoint(pipeline_dir, project_id, stage)
        if cp and cp.get("status") == "completed":
            completed.append(stage)
    return completed


def get_next_stage(
    pipeline_dir: Path, project_id: str, pipeline_type: str | None = None
) -> Optional[str]:
    """Determine the next stage to run based on completed checkpoints.

    Uses pipeline-specific stage order so that pipelines with different
    stage sequences (e.g. cinematic vs explainer) progress correctly.
    """
    stages = get_pipeline_stages(pipeline_type) if pipeline_type else STAGES
    completed = set(get_completed_stages(pipeline_dir, project_id, pipeline_type))
    for stage in stages:
        if stage not in completed:
            return stage
    return None
