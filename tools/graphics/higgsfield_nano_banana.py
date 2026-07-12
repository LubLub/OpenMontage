"""Higgsfield Nano Banana Pro image generation via the Higgsfield CLI.

Unlike `tools/video/higgsfield_video.py` (which calls the HTTP Cloud API), this
provider drives the locally-installed `higgsfield` binary. Auth is the CLI's own
OAuth token — there are NO HIGGSFIELD_API_KEY/SECRET env vars here. Readiness is
the FREE `account status` live probe via lib.higgsfield_cli.auth_guard().
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

_MODEL = "nano_banana_2"
_DEFAULT_RESOLUTION = "2k"


class HiggsfieldNanoBanana(BaseTool):
    name = "higgsfield_nano_banana"
    version = "0.1.0"
    tier = ToolTier.GENERATE
    capability = "image_generation"
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
    # No Nano-Banana Layer-3 skill exists.
    agent_skills: list[str] = []

    capabilities = ["generate_image", "text_to_image", "image_to_image"]
    supports = {
        "reference_image": True,
        "multiple_reference_images": True,
        "aspect_ratio": True,
        "resolution": True,
    }
    best_for = [
        "photoreal natural-history stills and illustrated documentary plates via Higgsfield CLI",
        "reference-conditioned character/subject consistency (up to 14 image references)",
        "high-resolution (up to 4k) single-image generation billed in Higgsfield credits",
    ]
    not_good_for = ["offline generation", "strict seeded reproducibility", "video motion"]
    fallback_tools = ["grok_image", "google_imagen"]

    input_schema = {
        "type": "object",
        "required": ["prompt", "spend_authorization"],
        "properties": {
            "prompt": {"type": "string"},
            "aspect_ratio": {
                "type": "string",
                "enum": ["1:1", "3:2", "2:3", "4:3", "3:4", "4:5", "5:4", "9:16", "16:9", "21:9"],
                "description": "Higgsfield aspect ratio (default 1:1 at the CLI).",
            },
            "resolution": {
                "type": "string",
                "enum": ["1k", "2k", "4k"],
                "default": _DEFAULT_RESOLUTION,
                "description": "Output resolution tier. Nano Banana Pro costs 2 credits at any resolution.",
            },
            "image_references": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Local image paths (auto-uploaded by the CLI) or upload IDs. Max 14.",
            },
            "output_path": {"type": "string"},
            "spend_authorization": {"type": "object"},
        },
    }

    resource_profile = ResourceProfile(
        cpu_cores=1, ram_mb=512, vram_mb=0, disk_mb=100, network_required=True
    )
    retry_policy = RetryPolicy(max_retries=2, retryable_errors=["rate_limit", "timeout"])
    idempotency_key_fields = ["prompt", "aspect_ratio", "resolution", "image_references"]
    side_effects = ["writes image file to output_path", "spends Higgsfield credits via the CLI"]
    user_visible_verification = ["Inspect generated image for prompt fidelity and reference consistency"]

    # ---- helpers ----

    def _params(self, inputs: dict[str, Any]) -> dict[str, Any]:
        """Build an immutable param dict for the CLI (never mutates inputs)."""
        params: dict[str, Any] = {
            "prompt": inputs["prompt"],
            "resolution": inputs.get("resolution", _DEFAULT_RESOLUTION),
        }
        if inputs.get("aspect_ratio"):
            params["aspect_ratio"] = inputs["aspect_ratio"]
        refs = inputs.get("image_references")
        if refs:
            params["image_references"] = list(refs)
        return params

    def get_status(self) -> ToolStatus:
        ok, _ = higgsfield_cli.auth_guard()
        return ToolStatus.AVAILABLE if ok else ToolStatus.UNAVAILABLE

    def estimate_cost(self, inputs: dict[str, Any]) -> float:
        """USD estimate = real credits × HIGGSFIELD_CREDIT_USD rate.

        The raw credit count is always the source of truth and is also stored in
        ToolResult.data["credits"] by execute(). Falls back to a static 2-credit
        figure (Nano Banana Pro's flat price) if the FREE cost probe is
        unreachable, so cost estimation never hard-fails.
        """
        credits = higgsfield_cli.estimate_credits(_MODEL, self._params(inputs))
        return higgsfield_cli.credits_to_usd(credits)

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        ok, warnings = higgsfield_cli.auth_guard()
        if not ok:
            return ToolResult(
                success=False,
                error="Higgsfield CLI not authenticated. " + "; ".join(warnings),
            )

        params = self._params(inputs)

        authorization_error = higgsfield_cli.validate_spend_authorization(
            inputs.get("spend_authorization"),
            tool=self.name,
            model=_MODEL,
            params=params,
        )
        if authorization_error:
            return ToolResult(success=False, error=authorization_error)

        # Real credit figure first (FREE), so we can report it even on failure.
        try:
            credits = higgsfield_cli.estimate_credits(_MODEL, params)
        except higgsfield_cli.HiggsfieldCLIError:
            return ToolResult(success=False, error="Live credit quote is unavailable")
        authorization_error = higgsfield_cli.validate_spend_authorization(
            inputs.get("spend_authorization"),
            tool=self.name,
            model=_MODEL,
            params=params,
            quoted_credits=credits,
        )
        if authorization_error:
            return ToolResult(success=False, error=authorization_error)
        try:
            cost_usd = higgsfield_cli.credits_to_usd(credits)
        except higgsfield_cli.HiggsfieldCLIError as exc:
            return ToolResult(success=False, error=str(exc))

        output_path = Path(inputs.get("output_path") or "higgsfield_nano_banana.png")
        if not output_path.suffix:
            output_path = output_path.with_suffix(".png")

        start = time.time()
        try:
            job = higgsfield_cli.generate(
                _MODEL,
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
                    "model": _MODEL,
                    "job_id": exc.job_id,
                    "recovery_required": True,
                },
            )
        except Exception as exc:  # CLI error, timeout before a job is created
            return ToolResult(
                success=False,
                error=f"Higgsfield Nano Banana generation failed: {exc}",
                data={"credits": credits, "model": _MODEL},
            )

        return ToolResult(
            success=True,
            data={
                "provider": "higgsfield",
                "model": _MODEL,
                "prompt": inputs["prompt"],
                "resolution": params["resolution"],
                "aspect_ratio": params.get("aspect_ratio"),
                "credits": credits,
                "job_id": job.get("job_id"),
                "output": str(output_path),
                "output_path": str(output_path),
                "format": output_path.suffix.lstrip("."),
                "warnings": warnings,
            },
            artifacts=[str(output_path)],
            cost_usd=cost_usd,
            duration_seconds=round(time.time() - start, 2),
            model=_MODEL,
        )
