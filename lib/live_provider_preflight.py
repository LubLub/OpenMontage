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


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _request_json(url: str, headers: dict[str, str], timeout: int) -> object:
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        opener = urllib.request.build_opener(_NoRedirect())
        with opener.open(request, timeout=timeout) as response:
            payload = response.read(4 * 1024 * 1024 + 1)
            if len(payload) > 4 * 1024 * 1024:
                raise RuntimeError("read-only provider probe response was too large")
            return json.loads(payload)
    except (OSError, UnicodeError, json.JSONDecodeError, urllib.error.HTTPError) as exc:
        raise RuntimeError("read-only provider probe failed") from exc


def _post_json(
    url: str,
    headers: dict[str, str],
    body: dict[str, Any],
    timeout: int,
) -> object:
    request = urllib.request.Request(
        url,
        data=json.dumps(body, separators=(",", ":")).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        opener = urllib.request.build_opener(_NoRedirect())
        with opener.open(request, timeout=timeout) as response:
            payload = response.read(4 * 1024 * 1024 + 1)
            if len(payload) > 4 * 1024 * 1024:
                raise RuntimeError("provider capability smoke response was too large")
            return json.loads(payload)
    except (OSError, UnicodeError, json.JSONDecodeError, urllib.error.HTTPError) as exc:
        raise RuntimeError("provider capability smoke failed") from exc


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


def _research_smoke_request(choice: Mapping[str, Any]) -> dict[str, Any]:
    smoke = choice["smoke"]
    return {
        "model": choice["model"],
        "input": smoke["input"],
        "tools": [
            {
                "type": "web_search",
                "search_context_size": smoke["search_context_size"],
            }
        ],
        "tool_choice": "required",
        "include": ["web_search_call.action.sources"],
        "max_tool_calls": smoke["max_tool_calls"],
        "max_output_tokens": smoke["max_output_tokens"],
        "reasoning": {"effort": smoke["reasoning_effort"]},
        "service_tier": smoke["service_tier"],
        "store": False,
    }


def _research_smoke_hash(choice: Mapping[str, Any]) -> str:
    return higgsfield_cli.spend_request_hash(
        tool=choice["tool"],
        model=choice["model"],
        params=dict(choice["smoke"]),
    )


def _valid_research_smoke_authorization(
    authorization: Any,
    choice: Mapping[str, Any],
) -> bool:
    if not isinstance(authorization, Mapping):
        return False
    approval_id = authorization.get("approval_id")
    maximum = authorization.get("max_usd")
    expected_maximum = choice["smoke"]["max_usd"]
    return (
        isinstance(approval_id, str)
        and bool(approval_id.strip())
        and authorization.get("paid_actions_authorized") is True
        and authorization.get("tool") == choice["tool"]
        and authorization.get("model") == choice["model"]
        and authorization.get("request_sha256") == _research_smoke_hash(choice)
        and not isinstance(maximum, bool)
        and isinstance(maximum, (int, float))
        and math.isfinite(maximum)
        and maximum > 0
        and math.isclose(float(maximum), float(expected_maximum))
    )


def _valid_research_smoke_evidence(evidence: Any, choice: Mapping[str, Any]) -> bool:
    if not isinstance(evidence, Mapping):
        return False
    if evidence.get("request_sha256") != _research_smoke_hash(choice):
        return False
    if not isinstance(evidence.get("response_id"), str) or not evidence["response_id"]:
        return False
    numeric_fields = (
        "web_search_calls",
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "estimated_cost_usd",
        "max_usd",
    )
    if any(
        isinstance(evidence.get(key), bool)
        or not isinstance(evidence.get(key), (int, float))
        or not math.isfinite(float(evidence[key]))
        or float(evidence[key]) < 0
        for key in numeric_fields
    ):
        return False
    return (
        evidence["web_search_calls"] == 1
        and evidence["total_tokens"]
        == evidence["input_tokens"] + evidence["output_tokens"]
        and math.isclose(float(evidence["max_usd"]), float(choice["smoke"]["max_usd"]))
        and float(evidence["estimated_cost_usd"]) <= float(evidence["max_usd"])
    )


def _research_smoke_evidence(
    payload: object,
    choice: Mapping[str, Any],
) -> dict[str, Any] | None:
    if not isinstance(payload, dict) or payload.get("status") != "completed":
        return None
    response_id = payload.get("id")
    output = payload.get("output")
    usage = payload.get("usage")
    if (
        not isinstance(response_id, str)
        or not response_id
        or not isinstance(output, list)
        or not isinstance(usage, dict)
    ):
        return None
    calls = [
        item
        for item in output
        if isinstance(item, dict) and item.get("type") == "web_search_call"
    ]
    if len(calls) != 1 or calls[0].get("status") != "completed":
        return None
    token_fields = ("input_tokens", "output_tokens", "total_tokens")
    if any(
        isinstance(usage.get(key), bool)
        or not isinstance(usage.get(key), int)
        or usage[key] < 0
        for key in token_fields
    ):
        return None
    if usage["total_tokens"] != usage["input_tokens"] + usage["output_tokens"]:
        return None
    pricing = choice["smoke"]["pricing"]
    estimated_cost = (
        usage["input_tokens"] * pricing["input_usd_per_million_tokens"] / 1_000_000
        + usage["output_tokens"] * pricing["output_usd_per_million_tokens"] / 1_000_000
        + pricing["web_search_usd_per_call"]
    )
    evidence = {
        "request_sha256": _research_smoke_hash(choice),
        "response_id": response_id,
        "web_search_calls": 1,
        "input_tokens": usage["input_tokens"],
        "output_tokens": usage["output_tokens"],
        "total_tokens": usage["total_tokens"],
        "estimated_cost_usd": estimated_cost,
        "max_usd": float(choice["smoke"]["max_usd"]),
    }
    return evidence if _valid_research_smoke_evidence(evidence, choice) else None


def _probe_openai(
    choice: Mapping[str, Any],
    environ: Mapping[str, str],
    request_json: Callable[[str, dict[str, str], int], object],
    post_json: Callable[[str, dict[str, str], dict[str, Any], int], object],
    authorization: Any,
    prior_evidence: Any,
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
    if prior_evidence is not None:
        if not _valid_research_smoke_evidence(prior_evidence, choice):
            return _blocked(base, "research_smoke_evidence_invalid")
        return {
            **base,
            "status": "ready",
            "evidence_code": "paid_web_search_execution_verified",
            "research_smoke": dict(prior_evidence),
        }
    if authorization is None:
        return {
            **base,
            "status": "blocked",
            "evidence_code": "auth_and_model_verified",
            "reason_code": "paid_web_search_smoke_required",
        }
    if not _valid_research_smoke_authorization(authorization, choice):
        return _blocked(base, "research_smoke_authorization_invalid")
    try:
        payload = post_json(
            "https://api.openai.com/v1/responses",
            {
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            },
            _research_smoke_request(choice),
            30,
        )
        evidence = _research_smoke_evidence(payload, choice)
    except Exception:
        evidence = None
    if evidence is None:
        return {
            **_blocked(base, "paid_web_search_smoke_failed"),
            "paid_action_executed": True,
        }
    return {
        **base,
        "status": "ready",
        "evidence_code": "paid_web_search_execution_verified",
        "paid_action_executed": True,
        "research_smoke": evidence,
    }


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
        models = request_json(
            "https://api.elevenlabs.io/v1/models",
            {"xi-api-key": key},
            30,
        )
        selected = next(
            (
                model
                for model in models
                if isinstance(model, dict) and model.get("model_id") == choice["model"]
            ),
            None,
        ) if isinstance(models, list) else None
        if not isinstance(selected, dict) or selected.get("can_do_text_to_speech") is not True:
            return _blocked(base, "model_unavailable")
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
        if capability == "audio":
            process = run_command(["ffmpeg", "-hide_banner", "-filters"], 15)
            output = f"{process.stdout}\n{process.stderr}"
            available = process.returncode == 0 and all(
                required in output for required in ("amix", "loudnorm")
            )
        else:
            process = run_command(["ffmpeg", "-hide_banner", "-encoders"], 15)
            probe = run_command(["ffprobe", "-version"], 15)
            output = f"{process.stdout}\n{process.stderr}"
            available = (
                process.returncode == 0
                and probe.returncode == 0
                and "libx264" in output
            )
        if not available:
            return _blocked(base, "runtime_unavailable")
    except Exception:
        return _blocked(base, "runtime_unavailable")
    return {
        **base,
        "status": "ready",
        "evidence_code": "required_local_runtime_features_verified",
    }


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
    smoke = plan["research"].get("smoke")
    if not isinstance(smoke, Mapping) or set(smoke) != {
        "input",
        "max_tool_calls",
        "max_output_tokens",
        "reasoning_effort",
        "search_context_size",
        "service_tier",
        "max_usd",
        "pricing",
    }:
        raise ValueError("research.smoke must define the bounded capability request")
    if not isinstance(smoke["input"], str) or not smoke["input"].strip():
        raise ValueError("research.smoke.input must be a non-empty string")
    if smoke["max_tool_calls"] != 1:
        raise ValueError("research.smoke.max_tool_calls must be exactly one")
    if (
        isinstance(smoke["max_output_tokens"], bool)
        or not isinstance(smoke["max_output_tokens"], int)
        or not 1 <= smoke["max_output_tokens"] <= 1024
    ):
        raise ValueError("research.smoke.max_output_tokens is outside the safe bound")
    if smoke["search_context_size"] != "low":
        raise ValueError("research.smoke.search_context_size must be low")
    if smoke["reasoning_effort"] != "low":
        raise ValueError("research.smoke.reasoning_effort must be low")
    if smoke["service_tier"] != "default":
        raise ValueError("research.smoke.service_tier must be default")
    maximum = smoke["max_usd"]
    if (
        isinstance(maximum, bool)
        or not isinstance(maximum, (int, float))
        or not math.isfinite(maximum)
        or not 0 < maximum <= 1
    ):
        raise ValueError("research.smoke.max_usd is outside the safe bound")
    pricing = smoke["pricing"]
    pricing_fields = {
        "input_usd_per_million_tokens",
        "output_usd_per_million_tokens",
        "web_search_usd_per_call",
    }
    if not isinstance(pricing, Mapping) or set(pricing) != pricing_fields:
        raise ValueError("research.smoke.pricing must bind the approved rates")
    if any(
        isinstance(pricing[key], bool)
        or not isinstance(pricing[key], (int, float))
        or not math.isfinite(pricing[key])
        or pricing[key] < 0
        for key in pricing_fields
    ):
        raise ValueError("research.smoke.pricing contains an invalid rate")


def run_live_provider_preflight(
    plan: Mapping[str, Any],
    *,
    environ: Mapping[str, str] | None = None,
    request_json: Callable[[str, dict[str, str], int], object] = _request_json,
    post_json: Callable[
        [str, dict[str, str], dict[str, Any], int], object
    ] = _post_json,
    run_command: Callable[[list[str], int], Any] = _run_command,
    higgsfield_module: Any = higgsfield_cli,
    research_smoke_authorization: Any = None,
    research_smoke_evidence: Any = None,
) -> dict[str, Any]:
    """Probe the fixed Rehearsal capabilities without paid production."""
    _validate_plan(plan)
    environment = os.environ if environ is None else environ
    research = _probe_openai(
        plan["research"],
        environment,
        request_json,
        post_json,
        research_smoke_authorization,
        research_smoke_evidence,
    )
    results = [
        research,
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
        "probe_mode": (
            "capped_capability_validation"
            if research.get("paid_action_executed") is True
            else "read_only_no_spend"
        ),
        "paid_actions_executed": research.get("paid_action_executed") is True,
        "paid_production_actions_executed": False,
        "ready": all(item["status"] == "ready" for item in results),
        "total_quoted_credits": float(total_credits),
        "capabilities": results,
    }
