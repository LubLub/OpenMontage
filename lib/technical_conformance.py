"""Fail-closed technical conformance analysis for rendered video.

The analyzer makes only mechanical claims. Creative acceptance remains a human
decision at Release Approval. Command execution is injectable so contract tests
can exercise every detector without depending on a local FFmpeg installation.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path
from typing import Any, Callable, Protocol

from schemas.artifacts import validate_artifact


class CommandResult(Protocol):
    returncode: int
    stdout: str
    stderr: str


CommandRunner = Callable[[list[str], int], CommandResult]

REQUIRED_CHECKS = (
    "file_integrity",
    "duration",
    "resolution",
    "asset_completeness",
    "broken_frames",
    "frozen_frames",
    "unintended_silence",
    "unsafe_peaks",
    "narration_timing",
)


def _run_command(command: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _canonical_hash(value: Any) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _check(
    status: str,
    source: str,
    observed: str,
    expected: str,
    issue: str | None = None,
) -> dict[str, Any]:
    return {
        "status": status,
        "evidence_source": source,
        "observed": observed,
        "expected": expected,
        "issues": [issue] if issue else [],
    }


def _durations(output: str, label: str) -> list[float]:
    return [
        float(value)
        for value in re.findall(rf"{re.escape(label)}\s*:\s*([0-9]+(?:\.[0-9]+)?)", output)
    ]


def build_technical_conformance(
    *,
    conformance_id: str,
    conformance_version: int,
    project_id: str,
    channel_snapshot_hash: str,
    compose_attempt_id: str,
    compose_output_fingerprint: str,
    project_path: str,
    render_sha256: str,
    render_report_content_hash: str,
    policy_id: str,
    policy_version: int,
    policy_content_hash: str,
    execution_mode: str,
    checks: dict[str, dict[str, Any]],
    manual_rescue: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build and validate conformance from precomputed nine-check evidence.

    This is the shared derivation seam for real FFmpeg evidence and explicitly
    marked deterministic Dry Run evidence. Callers cannot choose the root
    status or Production Proof result independently.
    """

    if set(checks) != set(REQUIRED_CHECKS):
        missing = sorted(set(REQUIRED_CHECKS) - set(checks))
        extra = sorted(set(checks) - set(REQUIRED_CHECKS))
        raise ValueError(
            f"technical conformance requires exactly nine checks; missing={missing}, extra={extra}"
        )
    invalid_statuses = {
        name: check.get("status")
        for name, check in checks.items()
        if check.get("status") not in {"pass", "fail"}
    }
    if invalid_statuses:
        raise ValueError(f"technical conformance checks have invalid statuses: {invalid_statuses}")

    rescue = manual_rescue or {
        "used": False,
        "rescue_id": None,
        "reason": None,
        "recorded_at": None,
    }
    failed = sorted(name for name in REQUIRED_CHECKS if checks[name]["status"] == "fail")
    status = "fail" if failed else "pass"
    proof_reasons = [f"technical_conformance_failed:{name}" for name in failed]
    if rescue.get("used"):
        proof_reasons.append("manual_rescue_used")
    if execution_mode == "deterministic_fixture":
        proof_reasons.append("deterministic_fixture")
    proof_eligible = (
        status == "pass"
        and not rescue.get("used")
        and execution_mode == "production"
    )

    scope = {
        "version": "1.0",
        "conformance_id": conformance_id,
        "conformance_version": conformance_version,
        "render": {
            "project_id": project_id,
            "channel_snapshot_hash": channel_snapshot_hash,
            "compose_attempt_id": compose_attempt_id,
            "compose_output_fingerprint": compose_output_fingerprint,
            "path": project_path,
            "sha256": render_sha256,
            "render_report_content_hash": render_report_content_hash,
        },
        "policy": {
            "policy_id": policy_id,
            "policy_version": policy_version,
            "content_hash": policy_content_hash,
        },
        "execution_mode": execution_mode,
        "status": status,
        "checks": checks,
        "manual_rescue": rescue,
        "production_proof": {
            "eligible": proof_eligible,
            "ineligibility_reasons": proof_reasons,
        },
    }
    artifact = {**scope, "content_hash": _canonical_hash(scope)}
    validate_artifact("technical_conformance", artifact)
    return artifact


def analyze_technical_conformance(
    render_path: str | Path,
    *,
    project_path: str,
    project_id: str,
    channel_snapshot_hash: str,
    compose_attempt_id: str,
    compose_output_fingerprint: str,
    render_report_content_hash: str,
    policy_id: str,
    policy_version: int,
    policy_content_hash: str,
    conformance_id: str,
    conformance_version: int = 1,
    execution_mode: str = "production",
    expected_duration_seconds: float,
    expected_resolution: str,
    expected_fps: float | None = None,
    expected_asset_count: int,
    found_asset_count: int,
    expected_narration_end_seconds: float,
    observed_narration_end_seconds: float,
    expected_assets: list[dict[str, str]] | None = None,
    observed_assets: list[dict[str, str]] | None = None,
    expected_narration_segments: list[dict[str, Any]] | None = None,
    observed_narration_segments: list[dict[str, Any]] | None = None,
    manual_rescue: dict[str, Any] | None = None,
    duration_tolerance_seconds: float = 0.5,
    narration_tolerance_seconds: float = 0.5,
    max_black_seconds: float = 0.25,
    max_frozen_seconds: float = 2.0,
    max_silence_seconds: float = 1.0,
    max_true_peak_dbfs: float = -1.0,
    runner: CommandRunner | None = None,
) -> dict[str, Any]:
    """Analyze one exact render and return a schema-valid conformance artifact."""

    path = Path(render_path)
    command_runner = runner or _run_command
    rescue = manual_rescue or {
        "used": False,
        "rescue_id": None,
        "reason": None,
        "recorded_at": None,
    }
    checks: dict[str, dict[str, Any]] = {}

    try:
        render_sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        render_sha256 = "0" * 64
        checks["file_integrity"] = _check(
            "fail", "ffprobe", "render unreadable", "readable container with video stream", str(exc)
        )

    probe_data: dict[str, Any] = {}
    if "file_integrity" not in checks:
        try:
            probe = command_runner(
                [
                    "ffprobe", "-v", "error", "-print_format", "json",
                    "-show_format", "-show_streams", str(path),
                ],
                30,
            )
            probe_data = json.loads(probe.stdout) if probe.returncode == 0 else {}
            video_stream = next(
                (
                    stream
                    for stream in probe_data.get("streams", [])
                    if stream.get("codec_type") == "video"
                ),
                None,
            )
            valid = probe.returncode == 0 and video_stream is not None
            checks["file_integrity"] = _check(
                "pass" if valid else "fail",
                "ffprobe",
                (
                    "readable container with video stream"
                    if valid
                    else "invalid or missing video stream"
                ),
                "readable container with video stream",
                None if valid else (probe.stderr.strip() or "ffprobe did not find a video stream"),
            )
        except (OSError, ValueError, json.JSONDecodeError, subprocess.SubprocessError) as exc:
            checks["file_integrity"] = _check(
                "fail",
                "ffprobe",
                "probe unavailable",
                "readable container with video stream",
                str(exc),
            )

    fmt = probe_data.get("format", {})
    video_stream = next(
        (stream for stream in probe_data.get("streams", []) if stream.get("codec_type") == "video"),
        {},
    )
    audio_stream = next(
        (stream for stream in probe_data.get("streams", []) if stream.get("codec_type") == "audio"),
        {},
    )
    try:
        duration = float(fmt.get("duration", 0))
    except (TypeError, ValueError):
        duration = 0.0
    try:
        measured_audio_duration = float(audio_stream.get("duration", duration))
    except (TypeError, ValueError):
        measured_audio_duration = 0.0
    duration_delta = abs(duration - expected_duration_seconds)
    checks["duration"] = _check(
        "pass" if duration_delta <= duration_tolerance_seconds else "fail",
        "ffprobe",
        f"{duration:g}s",
        f"{expected_duration_seconds:g}s +/- {duration_tolerance_seconds:g}s",
        (
            None
            if duration_delta <= duration_tolerance_seconds
            else "render duration is outside policy tolerance"
        ),
    )

    resolution = f"{video_stream.get('width', 0)}x{video_stream.get('height', 0)}"
    frame_rate = video_stream.get("avg_frame_rate", "0/1")
    try:
        numerator, denominator = str(frame_rate).split("/", 1)
        observed_fps = float(numerator) / float(denominator)
    except (ValueError, ZeroDivisionError):
        observed_fps = 0.0
    fps_ok = expected_fps is None or abs(observed_fps - expected_fps) <= 0.01
    resolution_ok = resolution == expected_resolution and fps_ok
    checks["resolution"] = _check(
        "pass" if resolution_ok else "fail",
        "ffprobe",
        f"{resolution} @ {observed_fps:g} fps" if expected_fps is not None else resolution,
        (
            f"{expected_resolution} @ {expected_fps:g} fps"
            if expected_fps is not None
            else expected_resolution
        ),
        None if resolution_ok else "render resolution or frame rate does not match policy",
    )

    expected_asset_set = sorted(
        expected_assets or [],
        key=lambda item: (item.get("asset_id", ""), item.get("path", "")),
    )
    observed_asset_set = sorted(
        observed_assets or [],
        key=lambda item: (item.get("asset_id", ""), item.get("path", "")),
    )
    exact_assets_supplied = expected_assets is not None and observed_assets is not None
    assets_complete = (
        expected_asset_set == observed_asset_set
        if exact_assets_supplied
        else expected_asset_count == found_asset_count
    )
    asset_observed = (
        f"{len(observed_asset_set)}/{len(expected_asset_set)} exact assets matched"
        if exact_assets_supplied
        else f"{found_asset_count}/{expected_asset_count} assets present"
    )
    asset_expected = (
        f"{len(expected_asset_set)}/{len(expected_asset_set)} exact assets matched"
        if exact_assets_supplied
        else f"{expected_asset_count}/{expected_asset_count} assets present"
    )
    checks["asset_completeness"] = _check(
        "pass" if assets_complete else "fail",
        "artifact_inventory",
        asset_observed,
        asset_expected,
        None if assets_complete else "one or more required assets are missing",
    )

    def filter_result(filter_expression: str) -> CommandResult | None:
        try:
            return command_runner(
                [
                    "ffmpeg", "-v", "info", "-i", str(path),
                    "-vf", filter_expression, "-f", "null", "-",
                ],
                180,
            )
        except (OSError, subprocess.SubprocessError):
            return None

    try:
        decode = command_runner(
            ["ffmpeg", "-v", "error", "-i", str(path), "-f", "null", "-"],
            180,
        )
        decode_ok = decode.returncode == 0 and not decode.stderr.strip()
        decode_issue = None if decode_ok else (decode.stderr.strip() or "full decode failed")
    except (OSError, subprocess.SubprocessError) as exc:
        decode_ok = False
        decode_issue = str(exc)
    black = filter_result(f"blackdetect=d={max_black_seconds}:pix_th=0.10")
    black_durations = _durations(black.stderr, "black_duration") if black else []
    black_total = sum(black_durations)
    black_ok = black is not None and black.returncode == 0 and black_total <= max_black_seconds
    if not black_ok and black is None:
        black_issue = "black-frame analysis unavailable"
    elif not black_ok:
        black_issue = "black-frame duration exceeds policy"
    else:
        black_issue = None
    checks["broken_frames"] = _check(
        "pass" if decode_ok and black_ok else "fail",
        "blackdetect" if decode_ok else "ffmpeg_decode",
        f"{black_total:g}s black; {'0' if decode_ok else '1+'} decode errors",
        f"<= {max_black_seconds:g}s black and 0 decode errors",
        decode_issue or black_issue,
    )

    freeze = filter_result(f"freezedetect=n=-60dB:d={max_frozen_seconds}")
    freeze_total = sum(_durations(freeze.stderr, "freeze_duration")) if freeze else 0.0
    freeze_ok = freeze is not None and freeze.returncode == 0 and freeze_total <= max_frozen_seconds
    checks["frozen_frames"] = _check(
        "pass" if freeze_ok else "fail",
        "freezedetect",
        f"{freeze_total:g}s frozen",
        f"<= {max_frozen_seconds:g}s unintended frozen video",
        None if freeze_ok else "frozen-frame duration exceeds policy or analysis failed",
    )

    try:
        silence = command_runner(
            [
                "ffmpeg", "-v", "info", "-i", str(path),
                "-af", f"silencedetect=noise=-50dB:d={max_silence_seconds}",
                "-f", "null", "-",
            ],
            180,
        )
    except (OSError, subprocess.SubprocessError):
        silence = None
    silence_total = sum(_durations(silence.stderr, "silence_duration")) if silence else 0.0
    silence_ok = (
        silence is not None
        and silence.returncode == 0
        and silence_total <= max_silence_seconds
    )
    checks["unintended_silence"] = _check(
        "pass" if silence_ok else "fail",
        "silencedetect",
        f"{silence_total:g}s detected silence",
        f"<= {max_silence_seconds:g}s unintended silence",
        None if silence_ok else "silence duration exceeds policy or analysis failed",
    )

    try:
        peak_result = command_runner(
            [
                "ffmpeg", "-v", "info", "-i", str(path),
                "-af", "ebur128=peak=true", "-f", "null", "-",
            ],
            180,
        )
    except (OSError, subprocess.SubprocessError):
        peak_result = None
    peak_matches = re.findall(
        r"Peak:\s*(-?[0-9]+(?:\.[0-9]+)?)\s*dBFS",
        peak_result.stderr if peak_result else "",
    )
    peak = float(peak_matches[-1]) if peak_matches else None
    peak_ok = (
        peak_result is not None
        and peak_result.returncode == 0
        and peak is not None
        and peak <= max_true_peak_dbfs
    )
    checks["unsafe_peaks"] = _check(
        "pass" if peak_ok else "fail",
        "ebur128",
        f"{peak:g} dBFS true peak" if peak is not None else "true peak unavailable",
        f"<= {max_true_peak_dbfs:g} dBFS true peak",
        None if peak_ok else "true peak exceeds policy or analysis failed",
    )

    if (
        expected_narration_segments is not None
        and observed_narration_segments is None
        and len(expected_narration_segments) == 1
        and audio_stream
    ):
        expected_segment = expected_narration_segments[0]
        observed_narration_segments = [
            {
                "asset_id": expected_segment.get("asset_id"),
                "start_seconds": 0,
                "end_seconds": measured_audio_duration,
            }
        ]
    if expected_narration_segments is not None and observed_narration_segments is not None:
        ordered_pairs = list(zip(expected_narration_segments, observed_narration_segments))
        identities_match = (
            len(expected_narration_segments) == len(observed_narration_segments)
            and all(
                expected.get("asset_id") == observed.get("asset_id")
                for expected, observed in ordered_pairs
            )
        )
        segment_deltas = [
            max(
                abs(
                    float(observed.get("start_seconds", -1))
                    - float(expected.get("start_seconds", 0))
                ),
                abs(
                    float(observed.get("end_seconds", -1))
                    - float(expected.get("end_seconds", 0))
                ),
            )
            for expected, observed in ordered_pairs
        ]
        narration_delta = max(segment_deltas, default=float("inf"))
        narration_ok = identities_match and narration_delta <= narration_tolerance_seconds
        narration_observed = (
            f"{narration_delta:g}s maximum segment drift; "
            f"{len(observed_narration_segments)}/{len(expected_narration_segments)} "
            "ordered segments measured"
        )
        narration_expected = (
            f"<= {narration_tolerance_seconds:g}s maximum segment drift; "
            f"{len(expected_narration_segments)}/{len(expected_narration_segments)} "
            "ordered segments measured"
        )
    elif expected_narration_segments is not None:
        narration_delta = float("inf")
        narration_ok = False
        narration_observed = "render-bound narration segment evidence unavailable"
        narration_expected = (
            f"{len(expected_narration_segments)} ordered narration segments measured"
        )
    else:
        narration_delta = abs(
            (measured_audio_duration or observed_narration_end_seconds)
            - expected_narration_end_seconds
        )
        narration_ok = narration_delta <= narration_tolerance_seconds
        narration_observed = f"{narration_delta:g}s end drift"
        narration_expected = f"<= {narration_tolerance_seconds:g}s end drift"
    checks["narration_timing"] = _check(
        "pass" if narration_ok else "fail",
        "timeline_comparison",
        narration_observed,
        narration_expected,
        None if narration_ok else "narration timing drift exceeds policy",
    )

    return build_technical_conformance(
        conformance_id=conformance_id,
        conformance_version=conformance_version,
        project_id=project_id,
        channel_snapshot_hash=channel_snapshot_hash,
        compose_attempt_id=compose_attempt_id,
        compose_output_fingerprint=compose_output_fingerprint,
        project_path=project_path,
        render_sha256=render_sha256,
        render_report_content_hash=render_report_content_hash,
        policy_id=policy_id,
        policy_version=policy_version,
        policy_content_hash=policy_content_hash,
        execution_mode=execution_mode,
        checks=checks,
        manual_rescue=rescue,
    )
