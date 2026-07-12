from __future__ import annotations

import json
import math
import urllib.request
from types import SimpleNamespace

import pytest

from lib.higgsfield_cli import spend_request_hash
from lib.live_provider_preflight import _NoRedirect, run_live_provider_preflight


def _plan() -> dict:
    return {
        "research": {
            "tool": "openrouter_responses_web_search",
            "provider": "openrouter",
            "model": "google/gemini-3.5-flash",
            "credential_ref": "env:OPENROUTER_API_KEY",
            "smoke": {
                "input": "Find the date the World Wide Web was invented and cite one source.",
                "max_tool_calls": 1,
                "max_output_tokens": 256,
                "reasoning_effort": "low",
                "search_engine": "exa",
                "max_results": 1,
                "max_total_results": 1,
                "max_characters": 2000,
                "max_usd": 0.25,
                "key_limit_usd": 0.25,
                "pricing": {
                    "input_usd_per_million_tokens": 1.5,
                    "output_usd_per_million_tokens": 9.0,
                    "web_search_usd_per_call": 0.005,
                },
            },
            "fallbacks": ["manual_primary_source_research"],
        },
        "narration": {
            "tool": "elevenlabs_tts",
            "provider": "elevenlabs",
            "model": "eleven_multilingual_v2",
            "credential_ref": "env:ELEVENLABS_API_KEY",
            "voice_ref": "env:HISTORY_SLEEP_VOICE_ID",
            "fallbacks": ["openai_tts"],
        },
        "image_generation": {
            "tool": "higgsfield_nano_banana",
            "provider": "higgsfield",
            "model": "nano_banana_2",
            "quote_params": {"prompt": "Rehearsal still", "aspect_ratio": "16:9"},
            "quantity": 2,
            "fallbacks": ["openai_image"],
        },
        "selective_video_generation": {
            "tool": "higgsfield_cli_video",
            "provider": "higgsfield",
            "model": "kling3_0",
            "quote_params": {"prompt": "Rehearsal motion", "duration": 5},
            "fallbacks": ["seedance_v1_5_pro"],
        },
        "audio": {
            "tool": "audio_mixer",
            "provider": "ffmpeg",
            "model": "ffmpeg",
            "fallbacks": [],
        },
        "composition": {
            "tool": "video_compose",
            "provider": "ffmpeg",
            "model": "ffmpeg",
            "fallbacks": ["remotion", "hyperframes"],
        },
    }


class FakeHiggsfield:
    def __init__(self) -> None:
        self.commands: list[tuple] = []

    def auth_status(self) -> dict:
        self.commands.append(("account", "status"))
        return {"state": "ok", "credits": 100, "email": "must-not-leak@example.com"}

    def estimate_credits(self, model: str, params: dict) -> float:
        self.commands.append(("generate", "cost", model, params))
        return 2.5 if model == "nano_banana_2" else 8.0

    def generate(self, *args, **kwargs):  # pragma: no cover - must be unreachable
        raise AssertionError("paid generation was reached")


def _request_json(url: str, headers: dict[str, str], timeout: int) -> object:
    assert timeout <= 30
    if url == "https://openrouter.ai/api/v1/key":
        assert headers == {"Authorization": "Bearer openrouter-secret"}
        return {"data": {"limit": 0.25, "limit_remaining": 0.25}}
    if url == "https://openrouter.ai/api/v1/models":
        assert headers == {"Authorization": "Bearer openrouter-secret"}
        return {"data": [{"id": "google/gemini-3.5-flash"}]}
    if url.startswith("https://api.elevenlabs.io/v1/voices/"):
        assert headers == {"xi-api-key": "eleven-secret"}
        assert url.endswith("voice-secret")
        return {"voice_id": "voice-secret", "name": "must-not-leak"}
    if url == "https://api.elevenlabs.io/v1/models":
        assert headers == {"xi-api-key": "eleven-secret"}
        return [
            {
                "model_id": "eleven_multilingual_v2",
                "can_do_text_to_speech": True,
            }
        ]
    raise AssertionError(url)


def _run_command(command: list[str], timeout: int) -> SimpleNamespace:
    assert timeout <= 15
    outputs = {
        ("ffmpeg", "-hide_banner", "-filters"): "amix loudnorm",
        ("ffmpeg", "-hide_banner", "-encoders"): "libx264",
        ("ffprobe", "-version"): "ffprobe version secret-build",
    }
    assert tuple(command) in outputs
    return SimpleNamespace(returncode=0, stdout=outputs[tuple(command)], stderr="")


def test_preflight_probes_all_six_capabilities_and_defers_paid_research_smoke() -> None:
    higgsfield = FakeHiggsfield()

    report = run_live_provider_preflight(
        _plan(),
        environ={
            "OPENROUTER_API_KEY": "openrouter-secret",
            "ELEVENLABS_API_KEY": "eleven-secret",
            "HISTORY_SLEEP_VOICE_ID": "voice-secret",
        },
        request_json=_request_json,
        run_command=_run_command,
        higgsfield_module=higgsfield,
    )

    assert report["ready"] is False
    assert report["probe_mode"] == "read_only_no_spend"
    assert report["paid_actions_executed"] is False
    assert report["total_quoted_credits"] == 13.0
    assert [item["capability"] for item in report["capabilities"]] == [
        "research",
        "narration",
        "image_generation",
        "selective_video_generation",
        "audio",
        "composition",
    ]
    assert report["capabilities"][0]["status"] == "blocked"
    assert report["capabilities"][0]["evidence_code"] == "auth_and_model_verified"
    assert report["capabilities"][0]["reason_code"] == "paid_web_search_smoke_required"
    assert all(item["status"] == "ready" for item in report["capabilities"][1:])
    assert report["capabilities"][2]["unit_quoted_credits"] == 2.5
    assert report["capabilities"][2]["quoted_credits"] == 5.0
    assert higgsfield.commands == [
        ("account", "status"),
        ("generate", "cost", "nano_banana_2", {"prompt": "Rehearsal still", "aspect_ratio": "16:9"}),
        ("account", "status"),
        ("generate", "cost", "kling3_0", {"prompt": "Rehearsal motion", "duration": 5}),
    ]
    encoded = json.dumps(report)
    for secret in ("openrouter-secret", "eleven-secret", "voice-secret", "must-not-leak"):
        assert secret not in encoded


def test_explicit_bound_authorization_executes_one_capped_research_smoke() -> None:
    requests: list[tuple[str, dict, dict, int]] = []
    key_status_calls = 0

    def request_json(url: str, headers: dict[str, str], timeout: int) -> object:
        nonlocal key_status_calls
        if url == "https://openrouter.ai/api/v1/key":
            key_status_calls += 1
            remaining = 0.25 if key_status_calls == 1 else 0.2325
            return {"data": {"limit": 0.25, "limit_remaining": remaining}}
        return _request_json(url, headers, timeout)

    def post_json(url: str, headers: dict, body: dict, timeout: int) -> object:
        requests.append((url, headers, body, timeout))
        return {
            "id": "resp_research_smoke_001",
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "content": [
                        {
                            "type": "output_text",
                            "text": "must-not-leak",
                            "annotations": [
                                {
                                    "type": "url_citation",
                                    "url": "https://must-not-be-persisted.invalid",
                                }
                            ],
                        }
                    ],
                },
            ],
            "usage": {
                "input_tokens": 8000,
                "output_tokens": 32,
                "total_tokens": 8032,
                "server_tool_use": {"web_search_requests": 1},
            },
        }

    plan = _plan()
    request_sha = spend_request_hash(
        tool=plan["research"]["tool"],
        model=plan["research"]["model"],
        params=plan["research"]["smoke"],
    )
    report = run_live_provider_preflight(
        plan,
        environ={
            "OPENROUTER_API_KEY": "openrouter-secret",
            "ELEVENLABS_API_KEY": "eleven-secret",
            "HISTORY_SLEEP_VOICE_ID": "voice-secret",
        },
        request_json=request_json,
        post_json=post_json,
        run_command=_run_command,
        higgsfield_module=FakeHiggsfield(),
        research_smoke_authorization={
            "approval_id": "history-sleep--rehearsal-001--research-smoke",
            "paid_actions_authorized": True,
            "tool": plan["research"]["tool"],
            "model": plan["research"]["model"],
            "request_sha256": request_sha,
            "max_usd": 0.25,
        },
    )

    assert report["ready"] is True
    assert report["probe_mode"] == "capped_capability_validation"
    assert report["paid_actions_executed"] is True
    assert report["paid_production_actions_executed"] is False
    research = report["capabilities"][0]
    assert research["status"] == "ready"
    assert research["evidence_code"] == "paid_web_search_execution_verified"
    assert research["research_smoke"] == {
        "request_sha256": request_sha,
        "response_id": "resp_research_smoke_001",
        "web_search_calls": 1,
        "input_tokens": 8000,
        "output_tokens": 32,
        "total_tokens": 8032,
        "estimated_cost_usd": pytest.approx(0.017288),
        "actual_cost_usd": pytest.approx(0.0175),
        "max_usd": 0.25,
        "provider_key_limit_usd": 0.25,
        "provider_key_remaining_before_usd": 0.25,
        "provider_key_remaining_after_usd": 0.2325,
    }
    assert requests == [
        (
            "https://openrouter.ai/api/v1/responses",
            {
                "Authorization": "Bearer openrouter-secret",
                "Content-Type": "application/json",
            },
            {
                "model": "google/gemini-3.5-flash",
                "input": "Find the date the World Wide Web was invented and cite one source.",
                "tools": [
                    {
                        "type": "openrouter:web_search",
                        "parameters": {
                            "engine": "exa",
                            "max_results": 1,
                            "max_total_results": 1,
                            "max_characters": 2000,
                        },
                    }
                ],
                "tool_choice": "required",
                "max_tool_calls": 1,
                "max_output_tokens": 256,
                "reasoning": {"effort": "low"},
                "store": False,
            },
            30,
        )
    ]
    encoded = json.dumps(report)
    for forbidden in (
        "openrouter-secret",
        "must-not-leak",
        "must-not-be-persisted",
        "https://must-not-be-persisted.invalid",
    ):
        assert forbidden not in encoded


def test_research_smoke_authorization_must_match_the_exact_bounded_request() -> None:
    post_calls = 0

    def post_json(*args, **kwargs):
        nonlocal post_calls
        post_calls += 1
        raise AssertionError("mismatched authorization reached a paid request")

    report = run_live_provider_preflight(
        _plan(),
        environ={
            "OPENROUTER_API_KEY": "openrouter-secret",
            "ELEVENLABS_API_KEY": "eleven-secret",
            "HISTORY_SLEEP_VOICE_ID": "voice-secret",
        },
        request_json=_request_json,
        post_json=post_json,
        run_command=_run_command,
        higgsfield_module=FakeHiggsfield(),
        research_smoke_authorization={
            "approval_id": "research-smoke",
            "paid_actions_authorized": True,
            "tool": "openrouter_responses_web_search",
            "model": "google/gemini-3.5-flash",
            "request_sha256": "sha256:stale",
            "max_usd": 0.25,
        },
    )

    assert post_calls == 0
    assert report["paid_actions_executed"] is False
    assert report["capabilities"][0]["reason_code"] == "research_smoke_authorization_invalid"


def test_verified_research_smoke_evidence_is_reused_without_another_paid_call() -> None:
    plan = _plan()
    request_sha = spend_request_hash(
        tool=plan["research"]["tool"],
        model=plan["research"]["model"],
        params=plan["research"]["smoke"],
    )

    def post_json(*args, **kwargs):
        raise AssertionError("persisted evidence triggered another paid request")

    evidence = {
        "request_sha256": request_sha,
        "response_id": "resp_research_smoke_001",
        "web_search_calls": 1,
        "input_tokens": 8000,
        "output_tokens": 32,
        "total_tokens": 8032,
        "estimated_cost_usd": 0.017288,
        "actual_cost_usd": 0.0175,
        "max_usd": 0.25,
        "provider_key_limit_usd": 0.25,
        "provider_key_remaining_before_usd": 0.25,
        "provider_key_remaining_after_usd": 0.2325,
    }
    report = run_live_provider_preflight(
        plan,
        environ={
            "OPENROUTER_API_KEY": "openrouter-secret",
            "ELEVENLABS_API_KEY": "eleven-secret",
            "HISTORY_SLEEP_VOICE_ID": "voice-secret",
        },
        request_json=_request_json,
        post_json=post_json,
        run_command=_run_command,
        higgsfield_module=FakeHiggsfield(),
        research_smoke_evidence=evidence,
    )

    assert report["ready"] is True
    assert report["paid_actions_executed"] is False
    assert report["capabilities"][0]["research_smoke"] == evidence


@pytest.mark.parametrize(
    ("missing", "expected_code"),
    [
        ("OPENROUTER_API_KEY", "credential_missing"),
        ("ELEVENLABS_API_KEY", "credential_missing"),
        ("HISTORY_SLEEP_VOICE_ID", "credential_missing"),
    ],
)
def test_missing_secret_reference_blocks_without_exposing_names_or_values(
    missing: str, expected_code: str
) -> None:
    env = {
        "OPENROUTER_API_KEY": "openrouter-secret",
        "ELEVENLABS_API_KEY": "eleven-secret",
        "HISTORY_SLEEP_VOICE_ID": "voice-secret",
    }
    del env[missing]

    report = run_live_provider_preflight(
        _plan(),
        environ=env,
        request_json=_request_json,
        run_command=_run_command,
        higgsfield_module=FakeHiggsfield(),
    )

    assert report["ready"] is False
    assert any(item.get("reason_code") == expected_code for item in report["capabilities"])
    assert "secret" not in json.dumps(report)


def test_provider_failures_are_redacted_and_block_approval() -> None:
    def failing_request(*args, **kwargs):
        raise RuntimeError("provider dumped sk-super-secret and signed URL")

    report = run_live_provider_preflight(
        _plan(),
        environ={
            "OPENROUTER_API_KEY": "openrouter-secret",
            "ELEVENLABS_API_KEY": "eleven-secret",
            "HISTORY_SLEEP_VOICE_ID": "voice-secret",
        },
        request_json=failing_request,
        run_command=_run_command,
        higgsfield_module=FakeHiggsfield(),
    )

    assert report["ready"] is False
    assert report["capabilities"][0]["reason_code"] == "live_probe_failed"
    assert report["capabilities"][1]["reason_code"] == "live_probe_failed"
    encoded = json.dumps(report)
    assert "sk-super-secret" not in encoded
    assert "signed URL" not in encoded


def test_invalid_or_extra_capability_plan_is_rejected_before_probes() -> None:
    plan = _plan()
    plan["publishing"] = {"tool": "youtube", "provider": "youtube", "model": "api"}

    with pytest.raises(ValueError, match="exactly the six Rehearsal capabilities"):
        run_live_provider_preflight(plan)


def test_credential_probe_refuses_all_redirects() -> None:
    handler = _NoRedirect()
    request = urllib.request.Request(
        "https://openrouter.ai/api/v1/models",
        headers={"Authorization": "Bearer must-not-forward"},
    )

    assert handler.redirect_request(
        request,
        None,
        302,
        "redirect",
        {},
        "https://attacker.invalid/steal",
    ) is None


@pytest.mark.parametrize("quoted", [math.nan, math.inf, -1.0, True])
def test_invalid_credit_quotes_fail_closed(quoted: object) -> None:
    class InvalidQuoteHiggsfield(FakeHiggsfield):
        def estimate_credits(self, model: str, params: dict) -> object:
            return quoted

    report = run_live_provider_preflight(
        _plan(),
        environ={
            "OPENROUTER_API_KEY": "openrouter-secret",
            "ELEVENLABS_API_KEY": "eleven-secret",
            "HISTORY_SLEEP_VOICE_ID": "voice-secret",
        },
        request_json=_request_json,
        run_command=_run_command,
        higgsfield_module=InvalidQuoteHiggsfield(),
    )

    assert report["ready"] is False
    assert report["capabilities"][2]["reason_code"] == "credit_quote_invalid"


@pytest.mark.parametrize(
    ("key_data", "expected_code"),
    [
        ({"limit_remaining": 0.25}, "provider_key_limit_missing"),
        ({"limit": 1.0, "limit_remaining": 1.0}, "provider_key_limit_exceeded"),
        ({"limit": 0.25, "limit_remaining": 0.0}, "provider_key_budget_exhausted"),
    ],
)
def test_openrouter_key_limit_must_enforce_the_smoke_cap(
    key_data: dict, expected_code: str
) -> None:
    post_calls = 0

    def request_json(url: str, headers: dict[str, str], timeout: int) -> object:
        if url == "https://openrouter.ai/api/v1/key":
            return {"data": key_data}
        return _request_json(url, headers, timeout)

    def post_json(*args, **kwargs):
        nonlocal post_calls
        post_calls += 1
        raise AssertionError("unsafe key configuration reached a paid request")

    report = run_live_provider_preflight(
        _plan(),
        environ={
            "OPENROUTER_API_KEY": "openrouter-secret",
            "ELEVENLABS_API_KEY": "eleven-secret",
            "HISTORY_SLEEP_VOICE_ID": "voice-secret",
        },
        request_json=request_json,
        post_json=post_json,
        run_command=_run_command,
        higgsfield_module=FakeHiggsfield(),
    )

    assert post_calls == 0
    assert report["ready"] is False
    assert report["capabilities"][0]["reason_code"] == expected_code


@pytest.mark.parametrize("web_search_requests", [0, 2, True, None])
def test_research_smoke_requires_usage_proof_of_exactly_one_web_search(
    web_search_requests: object,
) -> None:
    plan = _plan()
    request_sha = spend_request_hash(
        tool=plan["research"]["tool"],
        model=plan["research"]["model"],
        params=plan["research"]["smoke"],
    )

    def post_json(*args, **kwargs) -> object:
        return {
            "id": "resp_research_smoke_001",
            "status": "completed",
            "usage": {
                "input_tokens": 100,
                "output_tokens": 10,
                "total_tokens": 110,
                "server_tool_use": {"web_search_requests": web_search_requests},
            },
        }

    report = run_live_provider_preflight(
        plan,
        environ={
            "OPENROUTER_API_KEY": "openrouter-secret",
            "ELEVENLABS_API_KEY": "eleven-secret",
            "HISTORY_SLEEP_VOICE_ID": "voice-secret",
        },
        request_json=_request_json,
        post_json=post_json,
        run_command=_run_command,
        higgsfield_module=FakeHiggsfield(),
        research_smoke_authorization={
            "approval_id": "history-sleep--rehearsal-001--research-smoke",
            "paid_actions_authorized": True,
            "tool": plan["research"]["tool"],
            "model": plan["research"]["model"],
            "request_sha256": request_sha,
            "max_usd": 0.25,
        },
    )

    assert report["ready"] is False
    assert report["paid_actions_executed"] is True
    assert report["capabilities"][0]["reason_code"] == "paid_web_search_execution_unverified"


def test_research_smoke_derives_actual_cost_from_provider_key_balance() -> None:
    plan = _plan()
    key_status_calls = 0
    request_sha = spend_request_hash(
        tool=plan["research"]["tool"],
        model=plan["research"]["model"],
        params=plan["research"]["smoke"],
    )

    def post_json(*args, **kwargs) -> object:
        return {
            "id": "resp_research_smoke_001",
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "content": [
                        {
                            "type": "output_text",
                            "text": "must-not-persist",
                            "annotations": [
                                {
                                    "type": "url_citation",
                                    "url": "https://must-not-persist.invalid",
                                }
                            ],
                        }
                    ],
                }
            ],
            "usage": {
                "input_tokens": 100,
                "output_tokens": 10,
                "total_tokens": 110,
            },
        }

    def request_json(url: str, headers: dict[str, str], timeout: int) -> object:
        nonlocal key_status_calls
        if url == "https://openrouter.ai/api/v1/key":
            key_status_calls += 1
            remaining = 0.25 if key_status_calls == 1 else 0.23976
            return {"data": {"limit": 0.25, "limit_remaining": remaining}}
        return _request_json(url, headers, timeout)

    report = run_live_provider_preflight(
        plan,
        environ={
            "OPENROUTER_API_KEY": "openrouter-secret",
            "ELEVENLABS_API_KEY": "eleven-secret",
            "HISTORY_SLEEP_VOICE_ID": "voice-secret",
        },
        request_json=request_json,
        post_json=post_json,
        run_command=_run_command,
        higgsfield_module=FakeHiggsfield(),
        research_smoke_authorization={
            "approval_id": "history-sleep--rehearsal-001--research-smoke",
            "paid_actions_authorized": True,
            "tool": plan["research"]["tool"],
            "model": plan["research"]["model"],
            "request_sha256": request_sha,
            "max_usd": 0.25,
        },
    )

    assert report["ready"] is True
    assert report["paid_actions_executed"] is True
    evidence = report["capabilities"][0]["research_smoke"]
    assert evidence["actual_cost_usd"] == pytest.approx(0.01024)
    assert evidence["provider_key_remaining_before_usd"] == 0.25
    assert evidence["provider_key_remaining_after_usd"] == 0.23976
