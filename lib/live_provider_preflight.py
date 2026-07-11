"""Read-only production-provider probes for a Studio Rehearsal.

This module deliberately has no production execution seam.  It can authenticate
read-only provider endpoints, request Higgsfield's free credit quotes, and check
the local FFmpeg runtime.  It never generates media or records spend.
"""

from __future__ import annotations

import json
import math
import os
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from typing import Any

from lib import higgsfield_cli


CAPABILITIES = (
    "research",
    "narration",
    "image_generation",
    "selective_video_generation",
    "audio",
    "composition",
)


def _request_json(url: str, headers: dict[str, str], timeout: int) -> object:
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read(4 * 1024 * 1024 + 1)
            if len(payload) > 4 * 1024 * 1024:
                raise RuntimeError("read-only provider probe response was too large")
            return json.loads(payload)
    except (OSError, UnicodeError, json.JSONDecodeError, urllib.error.HTTPError) as exc:
        raise RuntimeError("read-only provider probe failed") from exc


def _run_command(command: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _env_value(reference: Any, environ: Mapping[str, str]) -> str | None:
    if not isinstance(reference, str) or not reference.startswith("env:"):
        return None
    value = environ.get(reference.removeprefix("env:"))
    return value if isinstance(value, str) and value.strip() else None


def _base_result(capability: str, choice: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "capability": capability,
        "tool": choice["tool"],
        "provider": choice["provider"],
        "model": choice["model"],
        "fallbacks": list(choice.get("fallbacks", [])),
    }


def _blocked(base: dict[str, Any], code: str) -> dict[str, Any]:
    return {**base, "status": "blocked", "reason_code": code}


def _probe_openai(
    choice: Mapping[str, Any],
    environ: Mapping[str, str],
    request_json: Callable[[str, dict[str, str], int], object],
) -> dict[str, Any]:
    base = _base_result("research", choice)
    key = _env_value(choice.get("credential_ref"), environ)
    if key is None:
        return _blocked(base, "credential_missing")
    try:
        payload = request_json(
            "https://api.openai.com/v1/models",
            {"Authorization": f"Bearer {key}"},
            30,
        )
        models = payload.get("data") if isinstance(payload, dict) else None
        available = {
            item.get("id")
            for item in models
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        } if isinstance(models, list) else set()
        if choice["model"] not in available:
            return _blocked(base, "model_unavailable")
    except Exception:
        return _blocked(base, "live_probe_failed")
    return {**base, "status": "ready", "evidence_code": "auth_and_model_verified"}


def _probe_elevenlabs(
    choice: Mapping[str, Any],
    environ: Mapping[str, str],
    request_json: Callable[[str, dict[str, str], int], object],
) -> dict[str, Any]:
    base = _base_result("narration", choice)
    key = _env_value(choice.get("credential_ref"), environ)
    voice_id = _env_value(choice.get("voice_ref"), environ)
    if key is None or voice_id is None:
        return _blocked(base, "credential_missing")
    try:
        payload = request_json(
            "https://api.elevenlabs.io/v1/voices/"
            + urllib.parse.quote(voice_id, safe=""),
            {"xi-api-key": key},
            30,
        )
        if not isinstance(payload, dict) or payload.get("voice_id") != voice_id:
            return _blocked(base, "voice_unavailable")
    except Exception:
        return _blocked(base, "live_probe_failed")
    return {**base, "status": "ready", "evidence_code": "auth_and_voice_verified"}


def _probe_higgsfield(
    capability: str,
    choice: Mapping[str, Any],
    module: Any,
) -> dict[str, Any]:
    base = _base_result(capability, choice)
    try:
        status = module.auth_status()
        if not isinstance(status, dict) or status.get("state") != "ok":
            return _blocked(base, "live_probe_failed")
        params = choice.get("quote_params")
        if not isinstance(params, dict):
            return _blocked(base, "quote_scope_invalid")
        quantity = choice.get("quantity", 1)
        if (
            isinstance(quantity, bool)
            or not isinstance(quantity, int)
            or not 1 <= quantity <= 10_000
        ):
            return _blocked(base, "quote_scope_invalid")
        quoted = module.estimate_credits(choice["model"], params)
        if (
            isinstance(quoted, bool)
            or not isinstance(quoted, (int, float))
            or not math.isfinite(quoted)
            or quoted < 0
        ):
            return _blocked(base, "credit_quote_invalid")
    except Exception:
        return _blocked(base, "live_probe_failed")
    total = float(quoted) * quantity
    if not math.isfinite(total):
        return _blocked(base, "credit_quote_invalid")
    return {
        **base,
        "status": "ready",
        "evidence_code": "auth_and_credit_quote_verified",
        "unit_quoted_credits": float(quoted),
        "quantity": quantity,
        "quoted_credits": total,
    }


def _probe_ffmpeg(
    capability: str,
    choice: Mapping[str, Any],
    run_command: Callable[[list[str], int], Any],
) -> dict[str, Any]:
    base = _base_result(capability, choice)
    try:
        process = run_command(["ffmpeg", "-version"], 15)
        if process.returncode != 0:
            return _blocked(base, "runtime_unavailable")
    except Exception:
        return _blocked(base, "runtime_unavailable")
    return {**base, "status": "ready", "evidence_code": "local_runtime_verified"}


def _validate_plan(plan: Mapping[str, Any]) -> None:
    if set(plan) != set(CAPABILITIES):
        raise ValueError("plan must contain exactly the six Rehearsal capabilities")
    for capability in CAPABILITIES:
        choice = plan[capability]
        if not isinstance(choice, Mapping):
            raise ValueError(f"{capability} choice must be an object")
        for key in ("tool", "provider", "model"):
            if not isinstance(choice.get(key), str) or not choice[key].strip():
                raise ValueError(f"{capability}.{key} must be a non-empty string")
        fallbacks = choice.get("fallbacks", [])
        if not isinstance(fallbacks, list) or not all(
            isinstance(item, str) and item for item in fallbacks
        ):
            raise ValueError(f"{capability}.fallbacks must be a string list")


def run_live_provider_preflight(
    plan: Mapping[str, Any],
    *,
    environ: Mapping[str, str] | None = None,
    request_json: Callable[[str, dict[str, str], int], object] = _request_json,
    run_command: Callable[[list[str], int], Any] = _run_command,
    higgsfield_module: Any = higgsfield_cli,
) -> dict[str, Any]:
    """Probe the fixed Rehearsal capability set without executing paid work."""
    _validate_plan(plan)
    environment = os.environ if environ is None else environ
    results = [
        _probe_openai(plan["research"], environment, request_json),
        _probe_elevenlabs(plan["narration"], environment, request_json),
        _probe_higgsfield(
            "image_generation", plan["image_generation"], higgsfield_module
        ),
        _probe_higgsfield(
            "selective_video_generation",
            plan["selective_video_generation"],
            higgsfield_module,
        ),
        _probe_ffmpeg("audio", plan["audio"], run_command),
        _probe_ffmpeg("composition", plan["composition"], run_command),
    ]
    total_credits = sum(
        item.get("quoted_credits", 0.0) for item in results
    )
    return {
        "schema_version": "1.0",
        "probe_mode": "read_only_no_spend",
        "paid_actions_executed": False,
        "ready": all(item["status"] == "ready" for item in results),
        "total_quoted_credits": float(total_credits),
        "capabilities": results,
    }
