"""Contracts for the reusable generative-documentary pipeline."""

from __future__ import annotations

import json
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

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
    package = {
        "version": "1.0",
        "package_id": "dry-run-release",
        "package_version": 1,
        "content_hash": f"sha256:{digest}",
        "technical_conformance": {
            "artifact_name": "technical_conformance",
            "conformance_id": "dry-run-conformance",
            "conformance_version": 1,
            "content_hash": f"sha256:{digest}",
            "project_id": "dry-run",
            "compose_attempt_id": "compose-attempt-1",
            "compose_output_fingerprint": f"sha256:{digest}",
            "render_sha256": digest,
            "policy_content_hash": f"sha256:{digest}",
            "execution_mode": "deterministic_fixture",
            "status": "pass",
            "manual_rescue_used": False,
            "production_proof_eligible": False,
        },
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
    scope = dict(package)
    scope.pop("content_hash")
    encoded = json.dumps(
        scope,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    package["content_hash"] = "sha256:" + hashlib.sha256(encoded).hexdigest()
    return package


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
    assert stages["compose"]["produces"] == [
        "render_report",
        "technical_conformance",
    ]
    assert "technical_conformance" in stages["publish"]["required_artifacts_in"]


def test_compose_exposes_the_versioned_remotion_bundle_path() -> None:
    manifest = load_pipeline("generative-documentary")
    compose = next(stage for stage in manifest["stages"] if stage["name"] == "compose")

    assert set(compose["required_artifacts_in"]) >= {
        "proposal_packet",
        "scene_plan",
        "asset_manifest",
        "edit_decisions",
    }
    assert "remotion_bundle" in compose["tools_available"]
    assert any(
        "remotion_bundle" in criterion
        for criterion in compose["success_criteria"]
    )


def _premium_scene_plan() -> dict:
    return {
        "version": "1.0",
        "metadata": {"visual_tier": "premium"},
        "scenes": [
            {
                "id": "scene-1",
                "type": "generated",
                "description": "A sourced street photograph receives a slow local push.",
                "start_seconds": 0,
                "end_seconds": 10,
                "hero_moment": False,
                "visual_type": "historical_anchor",
                "source_plan": {"mode": "source", "source_id": "archive-plate-1"},
                "provenance_plan": {
                    "origin": "deterministic_fixture",
                    "source_title": "Archive plate 1",
                    "source_url": "https://example.gov/archive/plate-1",
                    "fixture_proxy": True,
                    "represented_as_archival": False,
                    "rights": {
                        "public_domain": {
                            "basis": "Published by the issuing government archive",
                            "source_url": "https://example.gov/archive/rights",
                        }
                    },
                },
                "reconstruction_status": "not_reconstruction",
                "motion_treatment": {
                    "mode": "local_motion",
                    "instructions": "Apply a slow 4 percent push over ten seconds.",
                },
            }
        ],
    }


def _premium_asset_manifest() -> dict:
    digest = "c" * 64
    common = {
        "source_tool": "dry_run_fixture",
        "scene_id": "scene-1",
        "provider": "dry_run",
        "model": "deterministic-fixture-v1",
        "cost_usd": 0,
        "sha256": digest,
    }
    return {
        "version": "1.0",
        "profile": "provenance-aware-documentary-v1",
        "approval_scope": {
            "package_id": "dry-run-editorial",
            "package_version": 1,
            "content_hash": f"sha256:{digest}",
        },
        "assets": [
            {
                **common,
                "id": "anchor-1",
                "type": "image",
                "path": "assets/images/anchor.png",
                "visual_type": "historical_anchor",
                "reconstruction_status": "not_reconstruction",
                "provenance": {
                    "origin": "deterministic_fixture",
                    "source_id": "archive-plate-1",
                    "source_title": "Archive plate 1",
                    "source_url": "https://example.gov/archive/plate-1",
                    "fixture_proxy": True,
                    "rights": {
                        "public_domain": {
                            "basis": "Published by the issuing government archive",
                            "source_url": "https://example.gov/archive/rights",
                        }
                    },
                    "represented_as_archival": False,
                },
                "motion": {
                    "mode": "local_motion",
                    "instructions": "Apply a slow push.",
                    "source_asset_id": "anchor-1",
                },
            },
            {
                **common,
                "id": "narration",
                "type": "narration",
                "path": "assets/audio/narration.wav",
                "visual_type": "non_visual",
                "reconstruction_status": "not_applicable",
                "provenance": {
                    "origin": "deterministic_fixture",
                    "provider": "dry_run",
                    "model": "deterministic-fixture-v1",
                    "represented_as_archival": False,
                },
                "motion": {
                    "mode": "not_applicable",
                    "instructions": "Narration has no visual motion treatment.",
                },
            },
        ],
        "total_cost_usd": 0,
    }


def test_premium_documentary_vocabulary_is_schema_valid_and_strict() -> None:
    scene_plan = _premium_scene_plan()
    asset_manifest = _premium_asset_manifest()
    validate_artifact("scene_plan", scene_plan)
    validate_artifact("asset_manifest", asset_manifest)

    del scene_plan["scenes"][0]["motion_treatment"]
    with pytest.raises(jsonschema.ValidationError):
        validate_artifact("scene_plan", scene_plan)

    del asset_manifest["assets"][0]["provenance"]
    with pytest.raises(jsonschema.ValidationError):
        validate_artifact("asset_manifest", asset_manifest)


def test_premium_scene_requires_visual_type_when_other_premium_fields_remain() -> None:
    scene_plan = _premium_scene_plan()
    del scene_plan["scenes"][0]["visual_type"]

    with pytest.raises(jsonschema.ValidationError):
        validate_artifact("scene_plan", scene_plan)


def test_generated_reconstruction_cannot_be_structurally_archival() -> None:
    scene_plan = _premium_scene_plan()
    scene = scene_plan["scenes"][0]
    scene.update(
        hero_moment=True,
        visual_type="generated_reconstruction",
        source_plan={
            "mode": "generate",
            "provider": "dry_run",
            "model": "deterministic-fixture-v1",
        },
        reconstruction_status="generated_reconstruction",
        motion_treatment={
            "mode": "generated_video",
            "instructions": "Create the selected hero movement.",
            "provider": "dry_run",
            "model": "deterministic-fixture-v1",
        },
    )
    scene["provenance_plan"] = {
        "origin": "deterministic_fixture",
        "represented_as_archival": True,
    }

    with pytest.raises(jsonschema.ValidationError):
        validate_artifact("scene_plan", scene_plan)

    scene["provenance_plan"]["represented_as_archival"] = False
    validate_artifact("scene_plan", scene_plan)


def test_generated_video_motion_requires_provider_and_model() -> None:
    scene_plan = _premium_scene_plan()
    scene_plan["scenes"][0]["motion_treatment"] = {
        "mode": "generated_video",
        "instructions": "Create the selected hero movement.",
    }

    with pytest.raises(jsonschema.ValidationError):
        validate_artifact("scene_plan", scene_plan)


def test_generated_video_requires_a_generated_reconstruction_hero() -> None:
    scene_plan = _premium_scene_plan()
    scene = scene_plan["scenes"][0]
    scene["hero_moment"] = False
    scene["motion_treatment"] = {
        "mode": "generated_video",
        "instructions": "Create the selected hero movement.",
        "provider": "dry_run",
        "model": "deterministic-fixture-v1",
    }

    with pytest.raises(jsonschema.ValidationError):
        validate_artifact("scene_plan", scene_plan)


def test_source_and_generate_plans_are_mode_exclusive() -> None:
    scene_plan = _premium_scene_plan()
    scene_plan["scenes"][0]["source_plan"].update(
        provider="dry_run",
        model="deterministic-fixture-v1",
    )

    with pytest.raises(jsonschema.ValidationError):
        validate_artifact("scene_plan", scene_plan)


def test_premium_video_role_cannot_be_relabelled() -> None:
    manifest = _premium_asset_manifest()
    video = {
        **manifest["assets"][0],
        "id": "hero-video",
        "type": "video",
        "path": "assets/video/hero.mp4",
        "visual_type": "non_visual",
        "reconstruction_status": "not_applicable",
        "motion": {
            "mode": "generated_video",
            "instructions": "Create the selected hero movement.",
            "source_asset_id": "anchor-1",
        },
    }
    manifest["assets"].append(video)

    with pytest.raises(jsonschema.ValidationError):
        validate_artifact("asset_manifest", manifest)


@pytest.mark.parametrize(
    ("artifact_name", "mutate"),
    [
        (
            "scene_plan",
            lambda value: value["scenes"][0]["provenance_plan"].update(
                source_url="../archive-plate"
            ),
        ),
        (
            "scene_plan",
            lambda value: value["scenes"][0]["provenance_plan"]["rights"][
                "public_domain"
            ].update(source_url="file:///archive/rights"),
        ),
        (
            "asset_manifest",
            lambda value: value["assets"][0]["provenance"].update(
                source_url="archive.example/plate"
            ),
        ),
        (
            "asset_manifest",
            lambda value: value["assets"][0]["provenance"].update(
                source_url="https://?missing-host"
            ),
        ),
    ],
)
def test_premium_provenance_urls_are_absolute_http_urls(
    artifact_name: str,
    mutate: Callable[[dict], None],
) -> None:
    artifact = (
        _premium_scene_plan()
        if artifact_name == "scene_plan"
        else _premium_asset_manifest()
    )
    mutate(artifact)

    with pytest.raises(jsonschema.ValidationError):
        validate_artifact(artifact_name, artifact)


def test_premium_asset_manifest_requires_approval_scope() -> None:
    manifest = _premium_asset_manifest()
    del manifest["approval_scope"]

    with pytest.raises(jsonschema.ValidationError):
        validate_artifact("asset_manifest", manifest)


def test_premium_policy_is_owned_by_director_skills() -> None:
    root = Path(__file__).resolve().parents[2]
    scene_director = (
        root / "skills/pipelines/generative-documentary/scene-director.md"
    ).read_text()
    asset_director = (
        root / "skills/pipelines/generative-documentary/asset-director.md"
    ).read_text()

    assert "never describe or label it as\narchival evidence" in scene_director
    assert "license record or public-domain basis" in asset_director
    assert "marks it as a hero moment" in asset_director
    assert "Refuse an absent or\nstale approval" in asset_director


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
    assert "technical_conformance" in schema["required"]

    package = _release_package()
    package["technical_conformance"]["status"] = "fail"
    with pytest.raises(jsonschema.ValidationError):
        validate_artifact("release_package", package)


@pytest.mark.parametrize(
    "mismatch",
    ["conformance", "video_file", "thumbnail_file", "content_hash"],
)
def test_release_package_semantically_binds_approved_media(mismatch: str) -> None:
    package = _release_package()
    if mismatch == "conformance":
        package["technical_conformance"]["render_sha256"] = "b" * 64
    elif mismatch == "video_file":
        package["files"][0]["sha256"] = "b" * 64
    elif mismatch == "thumbnail_file":
        package["files"][1]["path"] = "assets/images/other.png"
    else:
        package["title"] = "Changed after package hashing"

    with pytest.raises(jsonschema.ValidationError):
        validate_artifact("release_package", package)


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
