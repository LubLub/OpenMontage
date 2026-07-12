"""Higgsfield video generation via the Higgsfield CLI (Kling 3.0 / Seedance 2.0).

Distinct from `tools/video/higgsfield_video.py` (HTTP Cloud API): this provider
shells out to the locally-installed, OAuth-authed `higgsfield` binary through
lib.higgsfield_cli. Default model is kling3_0 — the cost-optimal choice for the
subtle Ken-Burns / slow-push motion this documentary channel needs.

Per-model param shapes (confirmed via `higgsfield model get`):
  * kling3_0   — mode(std/pro/4k), duration, aspect_ratio, start_image, end_image.
                 NOTE: NO `resolution` param.
  * seedance_2_0 — resolution(480p/720p/1080p/4k) + mode(std/fast), duration,
                   aspect_ratio, start_image, end_image, generate_audio.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from tools.base_tool import (
    BaseTool,
    Determinism,
    ExecutionMode,
    ResourceProfile,
    RetryPolicy,
    ToolResult,
    ToolRuntime,
    ToolStability,
    ToolStatus,
    ToolTier,
)

from lib import higgsfield_cli

# Cost-optimal default for subtle Ken-Burns motion. Referenced by the schema and
# every code path that reads `model`, so estimate_cost / execute can't diverge
# from the advertised default.
_DEFAULT_MODEL = "kling3_0"
_MODELS = ["kling3_0", "seedance_2_0"]


class HiggsfieldCliVideo(BaseTool):
    name = "higgsfield_cli_video"
    version = "0.1.0"
    tier = ToolTier.GENERATE
    capability = "video_generation"
    provider = "higgsfield"
    stability = ToolStability.EXPERIMENTAL
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.STOCHASTIC
    runtime = ToolRuntime.API

    dependencies = []
    install_instructions = (
        "Install the Higgsfield CLI and authenticate it once:\n"
        "  brew install higgsfield   # or per Higgsfield docs\n"
        "  higgsfield auth login\n"
        "  (Optional) set HIGGSFIELD_BIN to a non-PATH binary location.\n"
        "  Set HIGGSFIELD_CREDIT_USD to convert credits→USD (default placeholder 0.01)."
    )
    agent_skills = ["ai-video-gen"]

    capabilities = ["text_to_video", "image_to_video"]
    supports = {
        "text_to_video": True,
        "image_to_video": True,
        "start_frame_conditioning": True,
        "camera_direction": True,
        "native_audio": True,
    }
    best_for = [
        "cost-optimal subtle Ken-Burns / slow-push motion via Higgsfield CLI (Kling 3.0 default)",
        "image-to-video from a generated still (start_image conditioning)",
        "higher-fidelity cinematic clips with native audio on demand (Seedance 2.0)",
    ]
    not_good_for = ["offline generation", "budget projects without a Higgsfield subscription"]
    fallback_tools = ["higgsfield_video", "seedance_video", "kling_video"]

    input_schema = {
        "type": "object",
        "required": ["prompt", "spend_authorization"],
        "properties": {
            "prompt": {"type": "string"},
            "model": {
                "type": "string",
                "enum": _MODELS,
                "default": _DEFAULT_MODEL,
                "description": "kling3_0 (cost-optimal default) or seedance_2_0 (higher fidelity).",
            },
            "operation": {
                "type": "string",
                "enum": ["text_to_video", "image_to_video"],
                "default": "text_to_video",
            },
            "duration": {
                "type": "integer",
                "default": 5,
                "description": "Clip duration in seconds (int).",
            },
            "aspect_ratio": {
                "type": "string",
                "enum": ["16:9", "9:16", "1:1"],
                "default": "16:9",
            },
            "mode": {
                "type": "string",
                "description": (
                    "kling3_0: std/pro/4k (default std). seedance_2_0: std/fast "
                    "(default std; fast only supports <=720p)."
                ),
            },
            "resolution": {
                "type": "string",
                "enum": ["480p", "720p", "1080p", "4k"],
                "description": "seedance_2_0 only (default 720p). Ignored for kling3_0 (kling uses mode).",
            },
            "generate_audio": {
                "type": "boolean",
                "description": "seedance_2_0 only — native audio (default true).",
            },
            "start_image": {
                "type": "string",
                "description": "Local path (auto-uploaded) or upload id for image_to_video start frame.",
            },
            "end_image": {
                "type": "string",
                "description": "Local path (auto-uploaded) or upload id for the end frame.",
            },
            "output_path": {"type": "string"},
            "spend_authorization": {"type": "object"},
        },
    }

    resource_profile = ResourceProfile(
        cpu_cores=1, ram_mb=512, vram_mb=0, disk_mb=500, network_required=True
    )
    retry_policy = RetryPolicy(max_retries=2, retryable_errors=["rate_limit", "timeout"])
    idempotency_key_fields = [
        "prompt",
        "model",
        "operation",
        "duration",
        "aspect_ratio",
        "mode",
        "resolution",
        "generate_audio",
        "start_image",
        "end_image",
    ]
    side_effects = ["writes video file to output_path", "spends Higgsfield credits via the CLI"]
    user_visible_verification = ["Watch generated clip for motion coherence and start-frame fidelity"]

    # ---- helpers ----

    def _model(self, inputs: dict[str, Any]) -> str | None:
        model = inputs.get("model", _DEFAULT_MODEL)
        return model if model in _MODELS else None

    def _params(self, inputs: dict[str, Any]) -> dict[str, Any]:
        """Build an immutable, model-correct param dict (never mutates inputs).

        Kling gets `mode` but never `resolution`; Seedance gets both plus
        `generate_audio`. `start_image`/`end_image` power image_to_video and are
        auto-uploaded by the CLI when given a local path.
        """
        model = self._model(inputs)
        if model is None:
            raise ValueError("Unsupported Higgsfield CLI video model")
        operation = inputs.get("operation", "text_to_video")
        start_image = inputs.get("start_image")
        end_image = inputs.get("end_image")
        if operation == "image_to_video" and not start_image:
            raise ValueError("image_to_video requires start_image")
        if operation == "text_to_video" and (start_image or end_image):
            raise ValueError("text_to_video cannot include start_image or end_image")

        params: dict[str, Any] = {
            "prompt": inputs["prompt"],
            "duration": int(inputs.get("duration", 5)),
            "aspect_ratio": inputs.get("aspect_ratio", "16:9"),
            "mode": inputs.get("mode", "std"),
        }

        if model == "seedance_2_0":
            params["resolution"] = inputs.get("resolution", "720p")
            params["generate_audio"] = bool(inputs.get("generate_audio", True))
        # kling3_0: intentionally no resolution/generate_audio (unsupported params).

        # Start-frame conditioning for image_to_video (both models support it).
        if start_image:
            params["start_image"] = start_image
        if end_image:
            params["end_image"] = end_image

        return params

    def get_status(self) -> ToolStatus:
        ok, _ = higgsfield_cli.auth_guard()
        return ToolStatus.AVAILABLE if ok else ToolStatus.UNAVAILABLE

    def estimate_cost(self, inputs: dict[str, Any]) -> float:
        """USD estimate = real credits × HIGGSFIELD_CREDIT_USD. Raw credits are
        the source of truth (also stored in ToolResult.data["credits"])."""
        model = self._model(inputs)
        if model is None:
            raise ValueError("Unsupported Higgsfield CLI video model")
        credits = higgsfield_cli.estimate_credits(model, self._params(inputs))
        return higgsfield_cli.credits_to_usd(credits)

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        ok, warnings = higgsfield_cli.auth_guard()
        if not ok:
            return ToolResult(
                success=False,
                error="Higgsfield CLI not authenticated. " + "; ".join(warnings),
            )

        model = self._model(inputs)
        if model is None:
            return ToolResult(success=False, error="Unsupported Higgsfield CLI video model")
        try:
            params = self._params(inputs)
        except ValueError as exc:
            return ToolResult(success=False, error=str(exc))

        authorization_error = higgsfield_cli.validate_spend_authorization(
            inputs.get("spend_authorization"),
            tool=self.name,
            model=model,
            params=params,
        )
        if authorization_error:
            return ToolResult(success=False, error=authorization_error)

        try:
            credits = higgsfield_cli.estimate_credits(model, params)
        except higgsfield_cli.HiggsfieldCLIError:
            return ToolResult(success=False, error="Live credit quote is unavailable")
        authorization_error = higgsfield_cli.validate_spend_authorization(
            inputs.get("spend_authorization"),
            tool=self.name,
            model=model,
            params=params,
            quoted_credits=credits,
        )
        if authorization_error:
            return ToolResult(success=False, error=authorization_error)
        try:
            cost_usd = higgsfield_cli.credits_to_usd(credits)
        except higgsfield_cli.HiggsfieldCLIError as exc:
            return ToolResult(success=False, error=str(exc))

        output_path = Path(inputs.get("output_path") or "higgsfield_cli_video.mp4")
        if not output_path.suffix:
            output_path = output_path.with_suffix(".mp4")

        start = time.time()
        try:
            job = higgsfield_cli.generate(
                model,
                params,
                output_path,
                tool=self.name,
                spend_authorization=inputs.get("spend_authorization"),
                quoted_credits=credits,
            )
        except higgsfield_cli.HiggsfieldJobRecoveryRequired as exc:
            return ToolResult(
                success=False,
                error="Higgsfield job created; output recovery required",
                data={
                    "credits": credits,
                    "model": model,
                    "job_id": exc.job_id,
                    "recovery_required": True,
                },
            )
        except Exception as exc:
            return ToolResult(
                success=False,
                error=f"Higgsfield CLI video generation failed: {exc}",
                data={"credits": credits, "model": model},
            )

        from tools.video._shared import probe_output

        probed = probe_output(output_path)
        return ToolResult(
            success=True,
            data={
                "provider": "higgsfield",
                "model": model,
                "prompt": inputs["prompt"],
                "operation": inputs.get("operation", "text_to_video"),
                "aspect_ratio": params.get("aspect_ratio", "16:9"),
                "credits": credits,
                "job_id": job.get("job_id"),
                "output": str(output_path),
                "output_path": str(output_path),
                "format": "mp4",
                "warnings": warnings,
                **probed,
            },
            artifacts=[str(output_path)],
            cost_usd=cost_usd,
            duration_seconds=round(time.time() - start, 2),
            model=model,
        )
