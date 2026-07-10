"""Contracts for the reusable generative-documentary pipeline."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import jsonschema
import pytest

from lib.checkpoint import CheckpointValidationError, write_checkpoint
from lib.pipeline_loader import get_stage_order, load_pipeline
from schemas.artifacts import load_schema, validate_artifact


EXPECTED_STAGES = [
    "research",
    "proposal",
    "script",
    "scene_plan",
    "assets",
    "edit",
    "compose",
    "publish",
]


def _release_package() -> dict:
    digest = "a" * 64
    return {
        "version": "1.0",
        "package_id": "dry-run-release",
        "package_version": 1,
        "content_hash": f"sha256:{digest}",
        "video": {
            "path": "renders/final.mp4",
            "sha256": digest,
            "format": "mp4",
            "resolution": "1920x1080",
            "duration_seconds": 30,
        },
        "title": "A Deterministic Documentary",
        "thumbnail": {
            "path": "assets/images/thumbnail.png",
            "sha256": digest,
        },
        "files": [
            {"role": role, "path": path, "sha256": digest}
            for role, path in (
                ("video", "renders/final.mp4"),
                ("thumbnail", "assets/images/thumbnail.png"),
                ("metadata", "release-package/metadata/metadata.json"),
                ("description", "release-package/metadata/description.txt"),
            )
        ],
        "description": "Offline Dry Run release package.",
        "disclosures": ["Contains generated reconstruction imagery."],
        "destination": {
            "platform": "youtube",
            "channel": "dry-run",
            "visibility": "private",
        },
        "publication_status": "not_published",
    }


def _publish_log() -> dict:
    return {
        "version": "1.0",
        "entries": [
            {
                "platform": "local",
                "status": "exported",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "export_path": "exports/release",
            }
        ],
    }


def test_manifest_has_exact_canonical_order_and_two_named_gates() -> None:
    manifest = load_pipeline("generative-documentary")

    assert manifest["category"] == "generated"
    assert get_stage_order(manifest) == EXPECTED_STAGES
    assert [
        stage["name"]
        for stage in manifest["stages"]
        if stage.get("human_approval_default")
    ] == ["scene_plan", "publish"]
    assert manifest["metadata"]["approval_gates"] == {
        "scene_plan": "Editorial Approval",
        "publish": "Release Approval",
    }
    assert manifest["metadata"]["publishing_mode"] == "offline_package_only"


def test_every_stage_director_is_present() -> None:
    manifest = load_pipeline("generative-documentary")
    root = Path(__file__).resolve().parents[2]

    assert (root / "skills/pipelines/generative-documentary/executive-producer.md").is_file()
    for stage in manifest["stages"]:
        skill = stage["skill"]
        assert (root / "skills" / f"{skill}.md").is_file(), skill


def test_release_package_schema_is_strict_and_offline() -> None:
    package = _release_package()
    validate_artifact("release_package", package)

    package["unexpected"] = True
    with pytest.raises(jsonschema.ValidationError):
        validate_artifact("release_package", package)

    schema = load_schema("release_package")
    assert "technical_conformance" not in schema["required"]
    assert "technical_conformance" not in schema["properties"]


@pytest.mark.parametrize(
    "unsafe",
    [
        "/tmp/final.mp4",
        "../final.mp4",
        "C:\\final.mp4",
        "\\tmp\\final.mp4",
        "\\\\server\\share\\final.mp4",
    ],
)
def test_release_package_paths_are_project_relative(unsafe: str) -> None:
    package = _release_package()
    package["video"]["path"] = unsafe

    with pytest.raises(jsonschema.ValidationError):
        validate_artifact("release_package", package)


def test_publish_checkpoint_validates_release_package_supplement(tmp_path: Path) -> None:
    path = write_checkpoint(
        tmp_path,
        "dry-run",
        "publish",
        "awaiting_human",
        {
            "publish_log": _publish_log(),
            "release_package": _release_package(),
        },
        pipeline_type="generative-documentary",
    )
    assert path.is_file()

    invalid = _release_package()
    invalid["publication_status"] = "published"
    with pytest.raises(CheckpointValidationError, match="release_package"):
        write_checkpoint(
            tmp_path,
            "invalid",
            "publish",
            "awaiting_human",
            {"publish_log": _publish_log(), "release_package": invalid},
            pipeline_type="generative-documentary",
        )


def test_decision_log_reference_is_project_relative(tmp_path: Path) -> None:
    proposal = {
        "version": "1.0",
        "concept_options": [
            {
                "id": f"c{index}",
                "title": f"Concept {index}",
                "hook": "A short hook",
                "narrative_structure": "journey",
                "visual_approach": "Generated reconstruction",
                "target_duration_seconds": 30,
                "why_this_works": "Deterministic contract coverage",
            }
            for index in range(1, 4)
        ],
        "selected_concept": {"concept_id": "c1", "rationale": "Best fit"},
        "production_plan": {
            "pipeline": "generative-documentary",
            "stages": [],
            "render_runtime": "ffmpeg",
        },
        "cost_estimate": {
            "total_estimated_usd": 0,
            "line_items": [
                {"tool": "deterministic-fake", "operation": "test", "estimated_usd": 0}
            ],
            "budget_verdict": "within_budget",
        },
        "approval": {"status": "pending"},
    }
    decision_log = {
        "version": "1.0",
        "project_id": "dry-run",
        "decisions": [],
    }
    path = write_checkpoint(
        tmp_path,
        "dry-run",
        "proposal",
        "completed",
        {"proposal_packet": proposal, "decision_log": decision_log},
        pipeline_type="generative-documentary",
    )
    checkpoint = json.loads(path.read_text())
    assert checkpoint["artifacts"]["proposal_packet"]["production_plan"][
        "decision_log_ref"
    ] == "decision_log.json"
