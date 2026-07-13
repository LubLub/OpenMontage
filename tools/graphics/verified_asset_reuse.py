"""Hash-bound reuse of an asset from a sibling OpenMontage project."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import uuid
from pathlib import Path
from typing import Any

from jsonschema import ValidationError
from schemas.artifacts import validate_artifact
from tools.base_tool import (
    BaseTool,
    Determinism,
    ExecutionMode,
    ResourceProfile,
    ToolResult,
    ToolStability,
    ToolTier,
)


PROJECT_ID = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*--[a-z0-9]+(?:-[a-z0-9]+)*$")
MEDIA_TYPES = {"image", "video", "narration", "music"}


def _canonical_hash(value: Any) -> str:
    payload = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _relative(root: Path, value: str, *, existing: bool) -> Path:
    relative = Path(value)
    if relative.is_absolute() or not value or ".." in relative.parts:
        raise ValueError("reuse path must be project-relative")
    unresolved = root / relative
    if unresolved.is_symlink():
        raise ValueError("reuse path cannot be a symlink")
    if existing:
        resolved = unresolved.resolve(strict=True)
        if not resolved.is_file():
            raise ValueError("reuse source must be a regular file")
    else:
        unresolved.parent.mkdir(parents=True, exist_ok=True)
        parent = unresolved.parent.resolve(strict=True)
        resolved = parent / unresolved.name
    if not resolved.is_relative_to(root):
        raise ValueError("reuse path escapes its project")
    return resolved


def _trusted_source_asset(
    source_project: Path,
    source_project_id: str,
    checkpoint: dict[str, Any],
    source_asset_id: str,
) -> dict[str, Any]:
    artifacts = checkpoint.get("artifacts")
    manifest = artifacts.get("asset_manifest") if isinstance(artifacts, dict) else None
    if (
        checkpoint.get("project_id") != source_project_id
        or checkpoint.get("pipeline_type") != "generative-documentary"
        or checkpoint.get("stage") != "assets"
        or checkpoint.get("status") != "completed"
        or not isinstance(manifest, dict)
        or checkpoint.get("output_fingerprint") != _canonical_hash(artifacts)
    ):
        raise ValueError("reuse source checkpoint is not authoritative")
    try:
        validate_artifact("asset_manifest", manifest)
    except (FileNotFoundError, ValidationError) as exc:
        raise ValueError("reuse source manifest violates its contract") from exc
    attempt_id = checkpoint.get("attempt_id")
    if not isinstance(attempt_id, str) or not attempt_id:
        raise ValueError("reuse source checkpoint has no completed attempt")
    attempt_path = _relative(
        source_project,
        f"attempts/assets/{attempt_id}.json",
        existing=True,
    )
    try:
        attempt = json.loads(attempt_path.read_text(encoding="utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("reuse source attempt is invalid") from exc
    manifest_receipt = _canonical_hash(manifest)
    if (
        not isinstance(attempt, dict)
        or attempt.get("attempt_id") != attempt_id
        or attempt.get("stage") != "assets"
        or attempt.get("status") != "completed"
        or attempt.get("artifacts") != artifacts
        or attempt.get("output_fingerprint") != checkpoint["output_fingerprint"]
        or attempt.get("receipts", {}).get("artifacts", {}).get("asset_manifest")
        != manifest_receipt
    ):
        raise ValueError("reuse source attempt does not bind its checkpoint")
    source_assets = manifest.get("assets")
    matching_assets = [
        asset
        for asset in source_assets
        if isinstance(asset, dict) and asset.get("id") == source_asset_id
    ]
    if len(matching_assets) != 1:
        raise ValueError("reuse source asset identity is invalid")
    source_asset = matching_assets[0]
    file_receipt = {
        "path": source_asset.get("path"),
        "sha256": source_asset.get("sha256"),
    }
    if file_receipt not in attempt.get("receipts", {}).get("files", []):
        raise ValueError("reuse source attempt lacks the asset receipt")
    cost_ids = attempt.get("cost_entry_ids")
    if not isinstance(cost_ids, list) or not cost_ids:
        raise ValueError("reuse source attempt lacks cost evidence")
    cost_path = _relative(source_project, "cost_log.json", existing=True)
    try:
        cost_log = json.loads(cost_path.read_text(encoding="utf-8"))
        validate_artifact("cost_log", cost_log)
    except (UnicodeError, json.JSONDecodeError, FileNotFoundError, ValidationError) as exc:
        raise ValueError("reuse source cost evidence is invalid") from exc
    entries = [
        entry
        for entry in cost_log.get("entries", [])
        if entry.get("id") in cost_ids
    ]
    if (
        len(entries) != len(cost_ids)
        or {entry.get("id") for entry in entries} != set(cost_ids)
        or any(
            entry.get("status") != "completed"
            or entry.get("operation") != "assets"
            or entry.get("attempt_id") != attempt_id
            or file_receipt not in entry.get("output_receipts", [])
            for entry in entries
        )
    ):
        raise ValueError("reuse source cost evidence does not bind the asset")
    return source_asset


class VerifiedAssetReuse(BaseTool):
    """Copy a hash-verified sibling-project asset with a native receipt."""

    name = "verified_asset_reuse"
    version = "1.0.0"
    tier = ToolTier.CORE
    capability = "asset_reuse"
    provider = "local"
    stability = ToolStability.EXPERIMENTAL
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.DETERMINISTIC
    capabilities = ["hash_bound_sibling_project_reuse"]
    resource_profile = ResourceProfile(
        cpu_cores=1,
        ram_mb=128,
        vram_mb=0,
        disk_mb=1024,
        network_required=False,
    )
    side_effects = ["writes one hash-verified project-local asset copy"]
    input_schema = {
        "type": "object",
        "required": [
            "project_dir",
            "source_project_id",
            "source_manifest_path",
            "source_manifest_sha256",
            "source_asset_id",
            "source_path",
            "source_sha256",
            "output_path",
            "media_type",
        ],
        "properties": {
            "project_dir": {"type": "string"},
            "source_project_id": {"type": "string"},
            "source_manifest_path": {"type": "string"},
            "source_manifest_sha256": {
                "type": "string",
                "pattern": "^[0-9a-f]{64}$",
            },
            "source_asset_id": {"type": "string"},
            "source_path": {"type": "string"},
            "source_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
            "output_path": {"type": "string"},
            "media_type": {"type": "string", "enum": sorted(MEDIA_TYPES)},
        },
    }

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        try:
            project = Path(inputs["project_dir"]).resolve(strict=True)
            projects_root = project.parent
            source_project_id = inputs["source_project_id"]
            if not isinstance(source_project_id, str) or not PROJECT_ID.fullmatch(
                source_project_id
            ):
                raise ValueError("source project identity is invalid")
            unresolved_source_project = projects_root / source_project_id
            if unresolved_source_project.is_symlink():
                raise ValueError("source project cannot be a symlink")
            source_project = unresolved_source_project.resolve(strict=True)
            if source_project.parent != projects_root or not source_project.is_dir():
                raise ValueError("source project is not a safe sibling")
            if inputs["source_manifest_path"] != "checkpoint_assets.json":
                raise ValueError("reuse requires the canonical assets checkpoint")
            source_manifest = _relative(source_project, "checkpoint_assets.json", existing=True)
            source_manifest_sha256 = inputs["source_manifest_sha256"]
            if _sha256(source_manifest) != source_manifest_sha256:
                raise ValueError("reuse source manifest hash changed")
            try:
                manifest_document = json.loads(source_manifest.read_text(encoding="utf-8"))
            except (UnicodeError, json.JSONDecodeError) as exc:
                raise ValueError("reuse source manifest is invalid") from exc
            if not isinstance(manifest_document, dict):
                raise ValueError("reuse source manifest is invalid")
            source_asset_id = inputs["source_asset_id"]
            source_asset = _trusted_source_asset(
                source_project,
                source_project_id,
                manifest_document,
                source_asset_id,
            )
            if (
                source_asset.get("path") != inputs["source_path"]
                or source_asset.get("sha256") != inputs["source_sha256"]
            ):
                raise ValueError("reuse source differs from its manifest entry")
            source = _relative(source_project, inputs["source_path"], existing=True)
            expected_hash = inputs["source_sha256"]
            if _sha256(source) != expected_hash:
                raise ValueError("reuse source hash changed")
            media_type = inputs["media_type"]
            if media_type not in MEDIA_TYPES:
                raise ValueError("reuse media type is unsupported")
            output = _relative(project, inputs["output_path"], existing=False)
            temporary = output.with_name(f".{output.name}.{uuid.uuid4().hex}.tmp")
            try:
                shutil.copyfile(source, temporary)
                os.replace(temporary, output)
            finally:
                try:
                    temporary.unlink()
                except FileNotFoundError:
                    pass
            return ToolResult(
                success=True,
                data={
                    "path": inputs["output_path"],
                    "sha256": _sha256(output),
                    "size_bytes": output.stat().st_size,
                    "media_type": media_type,
                    "provider": self.provider,
                    "model": self.version,
                    "source_project_id": source_project_id,
                    "source_manifest_path": inputs["source_manifest_path"],
                    "source_manifest_sha256": source_manifest_sha256,
                    "source_asset_id": source_asset_id,
                    "source_path": inputs["source_path"],
                    "source_sha256": expected_hash,
                    "source_asset": source_asset,
                },
                artifacts=[str(output)],
                cost_usd=0.0,
                model=self.version,
            )
        except (KeyError, OSError, TypeError, ValueError) as exc:
            return ToolResult(success=False, error=str(exc), cost_usd=0.0)
