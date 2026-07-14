from __future__ import annotations

import hashlib
import json
from pathlib import Path

from tools.graphics.verified_asset_reuse import VerifiedAssetReuse
from tools.tool_registry import ToolRegistry


def _canonical_hash(value: object) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _write_source_evidence(
    project: Path,
    asset: dict,
    *,
    provenance_aware: bool = True,
) -> str:
    attempt_id = "trusted-assets-attempt"
    manifest = {"version": "1.0", "assets": [asset]}
    if provenance_aware:
        manifest.update(
            {
                "profile": "provenance-aware-documentary-v1",
                "approval_scope": {
                    "package_id": "approved-source-package",
                    "package_version": 1,
                    "content_hash": "sha256:" + "a" * 64,
                },
            }
        )
    artifacts = {"asset_manifest": manifest}
    output_fingerprint = _canonical_hash(artifacts)
    file_receipt = {"path": asset["path"], "sha256": asset["sha256"]}
    checkpoint = {
        "version": "1.0",
        "project_id": project.name,
        "pipeline_type": "generative-documentary",
        "stage": "assets",
        "status": "completed",
        "artifacts": artifacts,
        "output_fingerprint": output_fingerprint,
        "attempt_id": attempt_id,
    }
    checkpoint_path = project / "checkpoint_assets.json"
    checkpoint_path.write_text(json.dumps(checkpoint, sort_keys=True))
    attempt_dir = project / "attempts/assets"
    attempt_dir.mkdir(parents=True)
    (attempt_dir / f"{attempt_id}.json").write_text(
        json.dumps(
            {
                "version": "1.0",
                "stage": "assets",
                "attempt_id": attempt_id,
                "status": "completed",
                "artifacts": artifacts,
                "output_fingerprint": output_fingerprint,
                "cost_entry_ids": ["source-assets-cost"],
                "receipts": {
                    "artifacts": {
                        "asset_manifest": _canonical_hash(artifacts["asset_manifest"])
                    },
                    "files": [file_receipt],
                },
            },
            sort_keys=True,
        )
    )
    (project / "cost_log.json").write_text(
        json.dumps(
            {
                "version": "1.0",
                "entries": [
                    {
                        "id": "source-assets-cost",
                        "tool": "source-provider",
                        "operation": "assets",
                        "status": "completed",
                        "timestamp": "2026-07-13T00:00:00+00:00",
                        "attempt_id": attempt_id,
                        "output_receipts": [file_receipt],
                    }
                ],
            },
            sort_keys=True,
        )
    )
    return hashlib.sha256(checkpoint_path.read_bytes()).hexdigest()


def _licensed_nonvisual_asset(
    *, asset_id: str, media_type: str, path: str, digest: str
) -> dict:
    return {
        "id": asset_id,
        "type": media_type,
        "path": path,
        "sha256": digest,
        "source_tool": "source-provider",
        "scene_id": "scene-1",
        "provider": "source-provider",
        "model": "source-model-v1",
        "visual_type": "non_visual",
        "reconstruction_status": "not_applicable",
        "provenance": {
            "origin": "licensed_source",
            "provider": "source-provider",
            "model": "source-model-v1",
            "represented_as_archival": False,
            "rights": {"license": {"name": "Test fixture license"}},
        },
    }


def test_verified_asset_reuse_is_registered_and_hash_bound(tmp_path: Path) -> None:
    projects = tmp_path / "projects"
    source = projects / "sample--source/assets/audio/narration.wav"
    target = projects / "sample--target"
    source.parent.mkdir(parents=True)
    target.mkdir(parents=True)
    source.write_bytes(b"native-reuse-evidence")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    manifest_digest = _write_source_evidence(
        projects / "sample--source",
        _licensed_nonvisual_asset(
            asset_id="narration",
            media_type="narration",
            path="assets/audio/narration.wav",
            digest=digest,
        ),
    )

    registry = ToolRegistry()
    registry.discover()
    tool = registry.get("verified_asset_reuse")
    assert tool is not None
    result = tool.execute(
        {
            "project_dir": str(target),
            "source_project_id": "sample--source",
            "source_manifest_path": "checkpoint_assets.json",
            "source_manifest_sha256": manifest_digest,
            "source_asset_id": "narration",
            "source_path": "assets/audio/narration.wav",
            "source_sha256": digest,
            "output_path": "assets/reused/narration.wav",
            "media_type": "narration",
        }
    )

    assert result.success is True
    assert result.data["source_sha256"] == result.data["sha256"] == digest
    assert (target / result.data["path"]).read_bytes() == source.read_bytes()


def test_verified_asset_reuse_accepts_canonical_kebab_case_project_id(
    tmp_path: Path,
) -> None:
    projects = tmp_path / "projects"
    source = projects / "sample-source/assets/audio/narration.wav"
    target = projects / "sample-target"
    source.parent.mkdir(parents=True)
    target.mkdir(parents=True)
    source.write_bytes(b"canonical-project-source")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    manifest_digest = _write_source_evidence(
        projects / "sample-source",
        {
            "id": "narration",
            "type": "narration",
            "path": "assets/audio/narration.wav",
            "sha256": digest,
            "source_tool": "source-provider",
            "scene_id": "scene-1",
            "provider": "source-provider",
            "model": "source-model-v1",
            "visual_type": "non_visual",
            "reconstruction_status": "not_applicable",
            "provenance": {
                "origin": "licensed_source",
                "provider": "source-provider",
                "model": "source-model-v1",
                "represented_as_archival": False,
                "rights": {"license": {"name": "Test fixture license"}},
            },
        },
    )

    result = VerifiedAssetReuse().execute(
        {
            "project_dir": str(target),
            "source_project_id": "sample-source",
            "source_manifest_path": "checkpoint_assets.json",
            "source_manifest_sha256": manifest_digest,
            "source_asset_id": "narration",
            "source_path": "assets/audio/narration.wav",
            "source_sha256": digest,
            "output_path": "assets/reused/narration.wav",
            "media_type": "narration",
        }
    )

    assert result.success is True
    assert result.data["source_project_id"] == "sample-source"
    assert (target / "assets/reused/narration.wav").read_bytes() == source.read_bytes()


def test_verified_asset_reuse_rejects_media_type_mismatch_before_writing(
    tmp_path: Path,
) -> None:
    projects = tmp_path / "projects"
    source = projects / "sample--source/assets/audio/narration.wav"
    target = projects / "sample--target"
    source.parent.mkdir(parents=True)
    target.mkdir(parents=True)
    source.write_bytes(b"narration-must-stay-narration")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    manifest_digest = _write_source_evidence(
        projects / "sample--source",
        _licensed_nonvisual_asset(
            asset_id="narration",
            media_type="narration",
            path="assets/audio/narration.wav",
            digest=digest,
        ),
    )

    result = VerifiedAssetReuse().execute(
        {
            "project_dir": str(target),
            "source_project_id": "sample--source",
            "source_manifest_path": "checkpoint_assets.json",
            "source_manifest_sha256": manifest_digest,
            "source_asset_id": "narration",
            "source_path": "assets/audio/narration.wav",
            "source_sha256": digest,
            "output_path": "assets/reused/narration.wav",
            "media_type": "image",
        }
    )

    assert result.success is False
    assert "does not match source manifest asset type" in result.error
    assert not (target / "assets").exists()


def test_verified_asset_reuse_rejects_noncanonical_source_type_before_writing(
    tmp_path: Path,
) -> None:
    projects = tmp_path / "projects"
    source = projects / "sample--source/assets/audio/source.wav"
    target = projects / "sample--target"
    source.parent.mkdir(parents=True)
    target.mkdir(parents=True)
    source.write_bytes(b"generic-audio-is-not-narration")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    manifest_digest = _write_source_evidence(
        projects / "sample--source",
        _licensed_nonvisual_asset(
            asset_id="generic-audio",
            media_type="audio",
            path="assets/audio/source.wav",
            digest=digest,
        ),
    )

    result = VerifiedAssetReuse().execute(
        {
            "project_dir": str(target),
            "source_project_id": "sample--source",
            "source_manifest_path": "checkpoint_assets.json",
            "source_manifest_sha256": manifest_digest,
            "source_asset_id": "generic-audio",
            "source_path": "assets/audio/source.wav",
            "source_sha256": digest,
            "output_path": "assets/reused/source.wav",
            "media_type": "narration",
        }
    )

    assert result.success is False
    assert result.error == "reuse media type is unsupported"
    assert not (target / "assets").exists()


def test_verified_asset_reuse_rejects_source_without_provenance_profile(
    tmp_path: Path,
) -> None:
    projects = tmp_path / "projects"
    source = projects / "sample--source/assets/audio/narration.wav"
    target = projects / "sample--target"
    source.parent.mkdir(parents=True)
    target.mkdir(parents=True)
    source.write_bytes(b"unapproved-source")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    manifest_digest = _write_source_evidence(
        projects / "sample--source",
        {
            "id": "narration",
            "type": "narration",
            "path": "assets/audio/narration.wav",
            "sha256": digest,
            "source_tool": "source-provider",
            "scene_id": "scene-1",
        },
        provenance_aware=False,
    )

    result = VerifiedAssetReuse().execute(
        {
            "project_dir": str(target),
            "source_project_id": "sample--source",
            "source_manifest_path": "checkpoint_assets.json",
            "source_manifest_sha256": manifest_digest,
            "source_asset_id": "narration",
            "source_path": "assets/audio/narration.wav",
            "source_sha256": digest,
            "output_path": "assets/reused/narration.wav",
            "media_type": "narration",
        }
    )

    assert result.success is False
    assert "provenance-aware" in result.error
    assert not (target / "assets/reused/narration.wav").exists()


def test_verified_asset_reuse_rejects_changed_source(tmp_path: Path) -> None:
    projects = tmp_path / "projects"
    source = projects / "sample--source/source.bin"
    target = projects / "sample--target"
    source.parent.mkdir(parents=True)
    target.mkdir(parents=True)
    source.write_bytes(b"changed")
    manifest_digest = _write_source_evidence(
        projects / "sample--source",
        {
            "id": "video",
            "type": "video",
            "path": "source.bin",
            "sha256": "0" * 64,
            "source_tool": "source-provider",
            "scene_id": "scene-1",
            "provider": "source-provider",
            "model": "source-model-v1",
            "visual_type": "generated_reconstruction",
            "reconstruction_status": "generated_reconstruction",
            "provenance": {
                "origin": "ai_generated",
                "provider": "source-provider",
                "model": "source-model-v1",
                "prompt": "test fixture",
                "represented_as_archival": False,
            },
            "motion": {
                "mode": "generated_video",
                "instructions": "Test fixture motion.",
            },
        },
    )

    result = VerifiedAssetReuse().execute(
        {
            "project_dir": str(target),
            "source_project_id": "sample--source",
            "source_manifest_path": "checkpoint_assets.json",
            "source_manifest_sha256": manifest_digest,
            "source_asset_id": "video",
            "source_path": "source.bin",
            "source_sha256": "0" * 64,
            "output_path": "asset.bin",
            "media_type": "video",
        }
    )

    assert result.success is False
    assert not (target / "asset.bin").exists()


def test_verified_asset_reuse_rejects_intermediate_output_symlink_without_writing(
    tmp_path: Path,
) -> None:
    projects = tmp_path / "projects"
    source = projects / "sample--source/assets/audio/narration.wav"
    target = projects / "sample--target"
    outside = tmp_path / "outside"
    source.parent.mkdir(parents=True)
    target.mkdir(parents=True)
    outside.mkdir()
    (target / "assets").symlink_to(outside, target_is_directory=True)
    source.write_bytes(b"safe-source")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    manifest_digest = _write_source_evidence(
        projects / "sample--source",
        {
            "id": "narration",
            "type": "narration",
            "path": "assets/audio/narration.wav",
            "sha256": digest,
            "source_tool": "source-provider",
            "scene_id": "scene-1",
            "provider": "source-provider",
            "model": "source-model-v1",
            "visual_type": "non_visual",
            "reconstruction_status": "not_applicable",
            "provenance": {
                "origin": "licensed_source",
                "provider": "source-provider",
                "model": "source-model-v1",
                "represented_as_archival": False,
                "rights": {"license": {"name": "Test fixture license"}},
            },
        },
    )

    result = VerifiedAssetReuse().execute(
        {
            "project_dir": str(target),
            "source_project_id": "sample--source",
            "source_manifest_path": "checkpoint_assets.json",
            "source_manifest_sha256": manifest_digest,
            "source_asset_id": "narration",
            "source_path": "assets/audio/narration.wav",
            "source_sha256": digest,
            "output_path": "assets/reused/narration.wav",
            "media_type": "narration",
        }
    )

    assert result.success is False
    assert "escapes its project" in result.error
    assert not (outside / "reused").exists()
