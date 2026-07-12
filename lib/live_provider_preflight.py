"""Production-provider probes for a Studio Rehearsal.

This module deliberately has no production execution seam.  It can authenticate
read-only provider endpoints, request Higgsfield's free credit quotes, check
the local FFmpeg runtime, and—only with bound authorization—run one capped
research web-search smoke. It never generates production media.
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
                "type": "openrouter:web_search",
                "parameters": {
                    "engine": smoke["search_engine"],
                    "max_results": smoke["max_results"],
                    "max_total_results": smoke["max_total_results"],
                    "max_characters": smoke["max_characters"],
                },
            }
        ],
        "tool_choice": "required",
        "max_tool_calls": smoke["max_tool_calls"],
        "max_output_tokens": smoke["max_output_tokens"],
        "reasoning": {"effort": smoke["reasoning_effort"]},
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
        "actual_cost_usd",
        "max_usd",
        "provider_key_limit_usd",
        "provider_key_remaining_before_usd",
        "provider_key_remaining_after_usd",
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
        and 0
        < float(evidence["provider_key_limit_usd"])
        <= float(choice["smoke"]["key_limit_usd"])
        and float(evidence["estimated_cost_usd"]) <= float(evidence["max_usd"])
        and float(evidence["actual_cost_usd"]) <= float(evidence["max_usd"])
        and float(evidence["provider_key_remaining_before_usd"])
        <= float(evidence["provider_key_limit_usd"])
        and 0
        <= float(evidence["provider_key_remaining_after_usd"])
        < float(evidence["provider_key_remaining_before_usd"])
        and math.isclose(
            float(evidence["actual_cost_usd"]),
            float(evidence["provider_key_remaining_before_usd"])
            - float(evidence["provider_key_remaining_after_usd"]),
        )
    )


def _has_url_citation(payload: Mapping[str, Any]) -> bool:
    output = payload.get("output")
    if not isinstance(output, list):
        return False
    for item in output:
        if not isinstance(item, Mapping):
            continue
        content_items = item.get("content")
        if not isinstance(content_items, list):
            continue
        for content in content_items:
            if not isinstance(content, Mapping):
                continue
            annotations = content.get("annotations")
            if not isinstance(annotations, list):
                continue
            if any(
                isinstance(annotation, Mapping)
                and annotation.get("type") == "url_citation"
                and isinstance(annotation.get("url"), str)
                and bool(annotation["url"])
                for annotation in annotations
            ):
                return True
    return False


def _research_smoke_evidence(
    payload: object,
    choice: Mapping[str, Any],
    provider_key_limit_usd: float,
    provider_key_remaining_before_usd: float,
    provider_key_remaining_after_usd: float,
) -> dict[str, Any] | None:
    if not isinstance(payload, dict) or payload.get("status") != "completed":
        return None
    response_id = payload.get("id")
    usage = payload.get("usage")
    if (
        not isinstance(response_id, str)
        or not response_id
        or not isinstance(usage, dict)
    ):
        return None
    server_tool_use = usage.get("server_tool_use")
    web_search_requests = (
        server_tool_use.get("web_search_requests")
        if isinstance(server_tool_use, dict)
        else None
    )
    if server_tool_use is not None:
        if (
            not isinstance(server_tool_use, dict)
            or isinstance(web_search_requests, bool)
            or not isinstance(web_search_requests, int)
            or web_search_requests != 1
        ):
            return None
    elif not _has_url_citation(payload):
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
    actual_cost = (
        provider_key_remaining_before_usd - provider_key_remaining_after_usd
    )
    if (
        isinstance(actual_cost, bool)
        or not isinstance(actual_cost, (int, float))
        or not math.isfinite(actual_cost)
        or actual_cost <= 0
    ):
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
        "actual_cost_usd": float(actual_cost),
        "max_usd": float(choice["smoke"]["max_usd"]),
        "provider_key_limit_usd": provider_key_limit_usd,
        "provider_key_remaining_before_usd": provider_key_remaining_before_usd,
        "provider_key_remaining_after_usd": provider_key_remaining_after_usd,
    }
    return evidence if _valid_research_smoke_evidence(evidence, choice) else None


def _probe_openrouter(
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
        key_payload = request_json(
            "https://openrouter.ai/api/v1/key",
            {"Authorization": f"Bearer {key}"},
            30,
        )
        key_data = key_payload.get("data") if isinstance(key_payload, dict) else None
        if not isinstance(key_data, Mapping) or "limit" not in key_data:
            return _blocked(base, "provider_key_limit_missing")
        provider_limit = key_data.get("limit")
        if (
            isinstance(provider_limit, bool)
            or not isinstance(provider_limit, (int, float))
            or not math.isfinite(provider_limit)
            or provider_limit <= 0
        ):
            return _blocked(base, "provider_key_limit_missing")
        if provider_limit > float(choice["smoke"]["key_limit_usd"]):
            return _blocked(base, "provider_key_limit_exceeded")
        remaining = key_data.get("limit_remaining")
        if (
            isinstance(remaining, bool)
            or not isinstance(remaining, (int, float))
            or not math.isfinite(remaining)
        ):
            return _blocked(base, "provider_key_budget_unverified")
        if remaining <= 0:
            return _blocked(base, "provider_key_budget_exhausted")
        payload = request_json(
            "https://openrouter.ai/api/v1/models",
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
            "https://openrouter.ai/api/v1/responses",
            {
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            },
            _research_smoke_request(choice),
            30,
        )
        after_payload = request_json(
            "https://openrouter.ai/api/v1/key",
            {"Authorization": f"Bearer {key}"},
            30,
        )
        after_data = (
            after_payload.get("data") if isinstance(after_payload, dict) else None
        )
        after_remaining = (
            after_data.get("limit_remaining")
            if isinstance(after_data, Mapping)
            else None
        )
        if (
            isinstance(after_remaining, bool)
            or not isinstance(after_remaining, (int, float))
            or not math.isfinite(after_remaining)
            or after_remaining < 0
        ):
            evidence = None
        else:
            evidence = _research_smoke_evidence(
                payload,
                choice,
                float(provider_limit),
                float(remaining),
                float(after_remaining),
            )
    except Exception:
        evidence = None
    if evidence is None:
        return {
            **_blocked(base, "paid_web_search_execution_unverified"),
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
        "search_engine",
        "max_results",
        "max_total_results",
        "max_characters",
        "max_usd",
        "key_limit_usd",
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
    if smoke["search_engine"] != "exa":
        raise ValueError("research.smoke.search_engine must be exa")
    if smoke["max_results"] != 1 or smoke["max_total_results"] != 1:
        raise ValueError("research.smoke search results must be exactly one")
    if (
        isinstance(smoke["max_characters"], bool)
        or not isinstance(smoke["max_characters"], int)
        or not 1 <= smoke["max_characters"] <= 5_000
    ):
        raise ValueError("research.smoke.max_characters is outside the safe bound")
    if smoke["reasoning_effort"] != "low":
        raise ValueError("research.smoke.reasoning_effort must be low")
    maximum = smoke["max_usd"]
    if (
        isinstance(maximum, bool)
        or not isinstance(maximum, (int, float))
        or not math.isfinite(maximum)
        or not 0 < maximum <= 1
    ):
        raise ValueError("research.smoke.max_usd is outside the safe bound")
    key_limit = smoke["key_limit_usd"]
    if (
        isinstance(key_limit, bool)
        or not isinstance(key_limit, (int, float))
        or not math.isfinite(key_limit)
        or not 0 < key_limit <= maximum
    ):
        raise ValueError("research.smoke.key_limit_usd is outside the safe bound")
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
    research = _probe_openrouter(
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
