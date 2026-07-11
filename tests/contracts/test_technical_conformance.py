from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import jsonschema
import pytest

from lib.checkpoint import SUPPLEMENTARY_ARTIFACTS
from lib.technical_conformance import (
    analyze_technical_conformance,
    build_technical_conformance,
)
from schemas.artifacts import ARTIFACT_NAMES, validate_artifact


DIGEST = "a" * 64


def _runner(
    *,
    freeze_seconds: float = 0,
    silence_seconds: float = 0,
    peak_dbfs: float = -1.5,
):
    def run(command: list[str], timeout: int) -> SimpleNamespace:
        assert timeout > 0
        if command[0] == "ffprobe":
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps(
                    {
                        "format": {"duration": "30.0"},
                        "streams": [
                            {"codec_type": "video", "width": 1920, "height": 1080},
                            {"codec_type": "audio"},
                        ],
                    }
                ),
                stderr="",
            )
        stderr = ""
        if "-vf" in command:
            expression = command[command.index("-vf") + 1]
            if expression.startswith("freezedetect") and freeze_seconds:
                stderr = f"freeze_duration: {freeze_seconds}"
        elif "-af" in command:
            expression = command[command.index("-af") + 1]
            if expression.startswith("ebur128"):
                stderr = f"Peak: {peak_dbfs} dBFS"
            elif expression.startswith("silencedetect") and silence_seconds:
                stderr = f"silence_duration: {silence_seconds}"
        return SimpleNamespace(returncode=0, stdout="", stderr=stderr)

    return run


def _artifact(tmp_path: Path, **overrides):
    render = tmp_path / "final.mp4"
    render.write_bytes(b"deterministic render fixture")
    arguments = {
        "project_path": "renders/final.mp4",
        "project_id": "channel--episode-1",
        "channel_snapshot_hash": f"sha256:{DIGEST}",
        "compose_attempt_id": "compose-attempt-1",
        "compose_output_fingerprint": f"sha256:{DIGEST}",
        "render_report_content_hash": f"sha256:{DIGEST}",
        "policy_id": "youtube-longform-v1",
        "policy_version": 1,
        "policy_content_hash": f"sha256:{DIGEST}",
        "conformance_id": "episode-1-conformance",
        "expected_duration_seconds": 30,
        "expected_resolution": "1920x1080",
        "expected_asset_count": 4,
        "found_asset_count": 4,
        "expected_narration_end_seconds": 29.5,
        "observed_narration_end_seconds": 29.5,
        "runner": _runner(),
    }
    arguments.update(overrides)
    return analyze_technical_conformance(render, **arguments)


def test_analyzer_returns_strict_passing_production_artifact(tmp_path: Path) -> None:
    artifact = _artifact(tmp_path)
    validate_artifact("technical_conformance", artifact)

    assert artifact["status"] == "pass"
    assert artifact["execution_mode"] == "production"
    assert artifact["production_proof"] == {
        "eligible": True,
        "ineligibility_reasons": [],
    }
    assert artifact["render"]["compose_attempt_id"] == "compose-attempt-1"
    assert set(artifact["checks"]) == {
        "file_integrity",
        "duration",
        "resolution",
        "asset_completeness",
        "broken_frames",
        "frozen_frames",
        "unintended_silence",
        "unsafe_peaks",
        "narration_timing",
    }


def test_analyzer_fails_closed_on_freeze_and_unsafe_peak(tmp_path: Path) -> None:
    artifact = _artifact(
        tmp_path,
        runner=_runner(freeze_seconds=4, silence_seconds=3, peak_dbfs=-0.2),
    )

    assert artifact["status"] == "fail"
    assert artifact["checks"]["frozen_frames"]["status"] == "fail"
    assert artifact["checks"]["unsafe_peaks"]["status"] == "fail"
    assert artifact["checks"]["unintended_silence"]["status"] == "fail"
    assert artifact["production_proof"]["eligible"] is False
    assert "technical_conformance_failed:frozen_frames" in artifact[
        "production_proof"
    ]["ineligibility_reasons"]


def test_fixture_and_manual_rescue_are_never_production_proof(tmp_path: Path) -> None:
    fixture = _artifact(tmp_path, execution_mode="deterministic_fixture")
    assert fixture["status"] == "pass"
    assert fixture["production_proof"] == {
        "eligible": False,
        "ineligibility_reasons": ["deterministic_fixture"],
    }

    rescued = _artifact(
        tmp_path,
        manual_rescue={
            "used": True,
            "rescue_id": "rescue-1",
            "reason": "operator replaced a damaged frame",
            "recorded_at": "2026-07-11T00:00:00Z",
        },
    )
    assert rescued["status"] == "pass"
    assert rescued["production_proof"]["eligible"] is False
    assert rescued["production_proof"]["ineligibility_reasons"] == ["manual_rescue_used"]


def test_public_builder_derives_fixture_status_and_rejects_incomplete_evidence(
    tmp_path: Path,
) -> None:
    checks = _artifact(tmp_path)["checks"]
    fixture = build_technical_conformance(
        conformance_id="fixture-conformance",
        conformance_version=1,
        project_id="channel--episode-1",
        channel_snapshot_hash=f"sha256:{DIGEST}",
        compose_attempt_id="compose-attempt-1",
        compose_output_fingerprint=f"sha256:{DIGEST}",
        project_path="renders/final.mp4",
        render_sha256=DIGEST,
        render_report_content_hash=f"sha256:{DIGEST}",
        policy_id="dry-run-v1",
        policy_version=1,
        policy_content_hash=f"sha256:{DIGEST}",
        execution_mode="deterministic_fixture",
        checks=checks,
    )
    assert fixture["status"] == "pass"
    assert fixture["production_proof"] == {
        "eligible": False,
        "ineligibility_reasons": ["deterministic_fixture"],
    }

    incomplete = dict(checks)
    incomplete.pop("duration")
    with pytest.raises(ValueError, match="exactly nine checks"):
        build_technical_conformance(
            conformance_id="fixture-conformance",
            conformance_version=1,
            project_id="channel--episode-1",
            channel_snapshot_hash=f"sha256:{DIGEST}",
            compose_attempt_id="compose-attempt-1",
            compose_output_fingerprint=f"sha256:{DIGEST}",
            project_path="renders/final.mp4",
            render_sha256=DIGEST,
            render_report_content_hash=f"sha256:{DIGEST}",
            policy_id="dry-run-v1",
            policy_version=1,
            policy_content_hash=f"sha256:{DIGEST}",
            execution_mode="deterministic_fixture",
            checks=incomplete,
        )


def test_schema_rejects_missing_checks_and_inconsistent_derived_proof(tmp_path: Path) -> None:
    artifact = _artifact(tmp_path)
    missing = copy.deepcopy(artifact)
    del missing["checks"]["duration"]
    with pytest.raises(jsonschema.ValidationError):
        validate_artifact("technical_conformance", missing)

    inconsistent = copy.deepcopy(artifact)
    inconsistent["execution_mode"] = "deterministic_fixture"
    with pytest.raises(jsonschema.ValidationError):
        validate_artifact("technical_conformance", inconsistent)

    false_pass = copy.deepcopy(artifact)
    false_pass["checks"]["duration"]["status"] = "fail"
    false_pass["checks"]["duration"]["issues"] = ["duration failed"]
    with pytest.raises(jsonschema.ValidationError):
        validate_artifact("technical_conformance", false_pass)

    tampered_binding = copy.deepcopy(artifact)
    tampered_binding["render"]["compose_attempt_id"] = "different-attempt"
    with pytest.raises(jsonschema.ValidationError, match="content_hash"):
        validate_artifact("technical_conformance", tampered_binding)


def test_builder_canonicalizes_multiple_failure_reason_order(tmp_path: Path) -> None:
    passing = _artifact(tmp_path)
    checks = {
        name: copy.deepcopy(passing["checks"][name])
        for name in reversed(list(passing["checks"]))
    }
    for name in ("unsafe_peaks", "duration"):
        checks[name]["status"] = "fail"
        checks[name]["issues"] = [f"{name} failed"]

    artifact = build_technical_conformance(
        conformance_id="ordered-failures",
        conformance_version=1,
        project_id="channel--episode-1",
        channel_snapshot_hash=f"sha256:{DIGEST}",
        compose_attempt_id="compose-attempt-1",
        compose_output_fingerprint=f"sha256:{DIGEST}",
        project_path="renders/final.mp4",
        render_sha256=DIGEST,
        render_report_content_hash=f"sha256:{DIGEST}",
        policy_id="dry-run-v1",
        policy_version=1,
        policy_content_hash=f"sha256:{DIGEST}",
        execution_mode="deterministic_fixture",
        checks=checks,
    )

    assert artifact["production_proof"]["ineligibility_reasons"] == [
        "technical_conformance_failed:duration",
        "technical_conformance_failed:unsafe_peaks",
        "deterministic_fixture",
    ]


def test_analyzer_rejects_equal_count_wrong_asset_set(tmp_path: Path) -> None:
    artifact = _artifact(
        tmp_path,
        expected_assets=[
            {"asset_id": "hero", "path": "assets/hero.png", "sha256": DIGEST}
        ],
        observed_assets=[
            {"asset_id": "other", "path": "assets/other.png", "sha256": DIGEST}
        ],
    )
    assert artifact["checks"]["asset_completeness"]["status"] == "fail"


def test_analyzer_rejects_narration_offset_with_same_end_time(tmp_path: Path) -> None:
    artifact = _artifact(
        tmp_path,
        expected_narration_segments=[
            {"asset_id": "narration-1", "start_seconds": 0, "end_seconds": 15},
            {"asset_id": "narration-2", "start_seconds": 15, "end_seconds": 30},
        ],
        observed_narration_segments=[
            {"asset_id": "narration-1", "start_seconds": 2, "end_seconds": 15},
            {"asset_id": "narration-2", "start_seconds": 17, "end_seconds": 30},
        ],
    )
    assert artifact["checks"]["narration_timing"]["status"] == "fail"


def test_analyzer_preserves_repeated_asset_narration_segments(tmp_path: Path) -> None:
    artifact = _artifact(
        tmp_path,
        expected_narration_segments=[
            {"asset_id": "narration", "start_seconds": 0, "end_seconds": 15},
            {"asset_id": "narration", "start_seconds": 15, "end_seconds": 30},
        ],
        observed_narration_segments=[
            {"asset_id": "narration", "start_seconds": 15, "end_seconds": 30},
        ],
    )
    assert artifact["checks"]["narration_timing"]["status"] == "fail"


def test_schema_rejects_invalid_manual_rescue_timestamp(tmp_path: Path) -> None:
    with pytest.raises(jsonschema.ValidationError):
        _artifact(
            tmp_path,
            manual_rescue={
                "used": True,
                "rescue_id": "rescue-1",
                "reason": "operator repair",
                "recorded_at": "not-a-timestamp",
            },
        )


def test_artifact_is_registered_for_schema_and_checkpoints() -> None:
    assert "technical_conformance" in ARTIFACT_NAMES
    assert "technical_conformance" in SUPPLEMENTARY_ARTIFACTS
