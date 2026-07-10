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


def _claim_ledger() -> dict:
    return {
        "version": "1.0",
        "ledger_id": "dry-run-claims",
        "ledger_version": 1,
        "project_id": "dry-run",
        "evidence_policy": {
            "routine_authoritative_sources": 1,
            "high_risk_authoritative_sources": 2,
        },
        "claims": [
            {
                "claim_id": "claim-1",
                "claim_text": "The city opened its first public library in 1901.",
                "classification": "routine",
                "risk_reasons": [],
                "evidence_requirement": "one_authoritative",
                "evidence_status": "sufficient",
                "sources": [
                    {
                        "source_id": "source-1",
                        "title": "Municipal annual report",
                        "url": "https://example.gov/archive/report",
                        "publisher": "City Archive",
                        "source_class": "official",
                        "authority_status": "authoritative",
                        "independence_group": "city-archive",
                    }
                ],
                "confidence": "high",
                "disposition": "supported",
                "narration_refs": [
                    {
                        "section_id": "opening",
                        "narration_excerpt": "The first public library opened in 1901.",
                        "start_char": 0,
                        "end_char": 40,
                    }
                ],
            }
        ],
    }


def _editorial_package() -> dict:
    digest = "b" * 64

    def component(name: str) -> dict:
        return {
            "artifact_name": name,
            "artifact_version": "1.0",
            "content_hash": f"sha256:{digest}",
        }

    return {
        "version": "1.0",
        "package_id": "dry-run-editorial",
        "package_version": 1,
        "content_hash": f"sha256:{digest}",
        "episode_thesis": "A city's quiet institutions reveal how public life changed.",
        "fact_checked_script": component("script"),
        "claim_ledger": {
            **component("claim_ledger"),
            "ledger_version": 1,
        },
        "shotlist": component("scene_plan"),
        "provider_plan": component("proposal_packet"),
        "expected_cost": {"currency": "USD", "amount": 0},
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


def test_manifest_carries_editorial_artifacts_to_the_existing_gate() -> None:
    manifest = load_pipeline("generative-documentary")
    stages = {stage["name"]: stage for stage in manifest["stages"]}

    assert stages["research"]["produces"] == ["research_brief", "claim_ledger"]
    assert "claim_ledger" in stages["script"]["required_artifacts_in"]
    assert stages["script"]["produces"] == ["script", "claim_ledger"]
    assert "claim_ledger" in stages["scene_plan"]["required_artifacts_in"]
    assert stages["scene_plan"]["produces"] == ["scene_plan", "editorial_package"]
    assert "editorial_package" in stages["assets"]["required_artifacts_in"]


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


def test_claim_ledger_schema_is_strict_and_registered() -> None:
    ledger = _claim_ledger()
    validate_artifact("claim_ledger", ledger)

    ledger["unexpected"] = True
    with pytest.raises(jsonschema.ValidationError):
        validate_artifact("claim_ledger", ledger)


def test_high_risk_supported_claim_requires_two_source_records() -> None:
    ledger = _claim_ledger()
    claim = ledger["claims"][0]
    claim.update(
        classification="high_risk",
        risk_reasons=["numerical"],
        evidence_requirement="two_independent_authoritative",
    )
    with pytest.raises(jsonschema.ValidationError):
        validate_artifact("claim_ledger", ledger)

    claim["sources"].append(
        {
            **claim["sources"][0],
            "source_id": "source-2",
            "url": "https://example.edu/archive/report",
        }
    )
    validate_artifact("claim_ledger", ledger)


def test_director_owns_source_independence_review_policy() -> None:
    root = Path(__file__).resolve().parents[2]
    script_director = (
        root / "skills/pipelines/generative-documentary/script-director.md"
    ).read_text()

    assert "independent groups and distinct publishers" in script_director


def test_claim_ids_must_be_unique() -> None:
    ledger = _claim_ledger()
    ledger["claims"].append({**ledger["claims"][0]})

    with pytest.raises(jsonschema.ValidationError, match="claim_id"):
        validate_artifact("claim_ledger", ledger)


def test_reused_source_id_requires_globally_consistent_metadata() -> None:
    ledger = _claim_ledger()
    second_claim = {
        **ledger["claims"][0],
        "claim_id": "claim-2",
        "claim_text": "The archive remained open through the winter.",
        "narration_refs": [
            {
                "section_id": "middle",
                "narration_excerpt": "The archive remained open through the winter.",
                "start_char": 0,
                "end_char": 45,
            }
        ],
        "sources": [{**ledger["claims"][0]["sources"][0]}],
    }
    ledger["claims"].append(second_claim)
    validate_artifact("claim_ledger", ledger)

    second_claim["sources"][0]["publisher"] = "Different Archive"
    with pytest.raises(jsonschema.ValidationError, match="inconsistent metadata"):
        validate_artifact("claim_ledger", ledger)


@pytest.mark.parametrize(
    "url",
    [
        "archive/report",
        "/archive/report",
        "ftp://example.gov/archive/report",
        "http:///archive/report",
    ],
)
def test_claim_ledger_source_urls_must_be_absolute_http_urls(url: str) -> None:
    ledger = _claim_ledger()
    ledger["claims"][0]["sources"][0]["url"] = url

    with pytest.raises(jsonschema.ValidationError, match="absolute http"):
        validate_artifact("claim_ledger", ledger)


@pytest.mark.parametrize(
    "url",
    [
        "http://example.gov/archive/report",
        "https://example.gov/archive/report?volume=1#page-2",
    ],
)
def test_claim_ledger_accepts_absolute_http_urls(url: str) -> None:
    ledger = _claim_ledger()
    ledger["claims"][0]["sources"][0]["url"] = url

    validate_artifact("claim_ledger", ledger)


@pytest.mark.parametrize("field", ["start_char", "end_char"])
def test_narration_references_require_character_spans(field: str) -> None:
    ledger = _claim_ledger()
    del ledger["claims"][0]["narration_refs"][0][field]

    with pytest.raises(jsonschema.ValidationError):
        validate_artifact("claim_ledger", ledger)


@pytest.mark.parametrize(
    ("field", "value"),
    [("start_char", -1), ("start_char", 0.5), ("end_char", 0), ("end_char", 1.5)],
)
def test_narration_reference_character_spans_have_integer_bounds(
    field: str,
    value: float,
) -> None:
    ledger = _claim_ledger()
    ledger["claims"][0]["narration_refs"][0][field] = value

    with pytest.raises(jsonschema.ValidationError):
        validate_artifact("claim_ledger", ledger)


def test_uncertain_claim_requires_explicit_narration_language() -> None:
    ledger = _claim_ledger()
    claim = ledger["claims"][0]
    claim.update(
        evidence_status="insufficient",
        sources=[],
        confidence="low",
        disposition="framed_uncertain",
    )

    with pytest.raises(jsonschema.ValidationError):
        validate_artifact("claim_ledger", ledger)

    claim["uncertainty_language"] = "The surviving evidence suggests"
    validate_artifact("claim_ledger", ledger)


def test_removed_claim_cannot_remain_in_narration() -> None:
    ledger = _claim_ledger()
    claim = ledger["claims"][0]
    claim.update(
        evidence_status="insufficient",
        sources=[],
        confidence="low",
        disposition="removed",
        removal_reason="No authoritative source was found.",
    )

    with pytest.raises(jsonschema.ValidationError):
        validate_artifact("claim_ledger", ledger)

    claim["narration_refs"] = []
    validate_artifact("claim_ledger", ledger)


def test_editorial_package_schema_is_strict_and_versioned() -> None:
    package = _editorial_package()
    validate_artifact("editorial_package", package)

    package["unexpected"] = True
    with pytest.raises(jsonschema.ValidationError):
        validate_artifact("editorial_package", package)


@pytest.mark.parametrize(
    ("component", "artifact_name"),
    [
        ("fact_checked_script", "scene_plan"),
        ("claim_ledger", "script"),
        ("shotlist", "proposal_packet"),
        ("provider_plan", "claim_ledger"),
    ],
)
def test_editorial_package_components_have_fixed_roles(
    component: str,
    artifact_name: str,
) -> None:
    package = _editorial_package()
    package[component]["artifact_name"] = artifact_name

    with pytest.raises(jsonschema.ValidationError):
        validate_artifact("editorial_package", package)


def test_scene_checkpoint_validates_editorial_artifacts(tmp_path: Path) -> None:
    scene_plan = {
        "version": "1.0",
        "scenes": [
            {
                "id": "scene-1",
                "type": "generated",
                "description": "A quiet reconstruction",
                "start_seconds": 0,
                "end_seconds": 10,
            }
        ],
    }
    path = write_checkpoint(
        tmp_path,
        "dry-run",
        "scene_plan",
        "awaiting_human",
        {
            "scene_plan": scene_plan,
            "claim_ledger": _claim_ledger(),
            "editorial_package": _editorial_package(),
        },
        pipeline_type="generative-documentary",
    )
    assert path.is_file()

    invalid = _claim_ledger()
    invalid["claims"][0]["sources"] = []
    with pytest.raises(CheckpointValidationError, match="claim_ledger"):
        write_checkpoint(
            tmp_path,
            "invalid",
            "scene_plan",
            "awaiting_human",
            {
                "scene_plan": scene_plan,
                "claim_ledger": invalid,
                "editorial_package": _editorial_package(),
            },
            pipeline_type="generative-documentary",
        )


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
