from __future__ import annotations

import json
import math
import urllib.request
from types import SimpleNamespace

import pytest

from lib.live_provider_preflight import _NoRedirect, run_live_provider_preflight


def _plan() -> dict:
    return {
        "research": {
            "tool": "openai_responses_web_search",
            "provider": "openai",
            "model": "gpt-5.4-mini",
            "credential_ref": "env:OPENAI_API_KEY",
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
    if url == "https://api.openai.com/v1/models":
        assert headers == {"Authorization": "Bearer openai-secret"}
        return {"data": [{"id": "gpt-5.4-mini"}]}
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
            "OPENAI_API_KEY": "openai-secret",
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
    for secret in ("openai-secret", "eleven-secret", "voice-secret", "must-not-leak"):
        assert secret not in encoded


@pytest.mark.parametrize(
    ("missing", "expected_code"),
    [
        ("OPENAI_API_KEY", "credential_missing"),
        ("ELEVENLABS_API_KEY", "credential_missing"),
        ("HISTORY_SLEEP_VOICE_ID", "credential_missing"),
    ],
)
def test_missing_secret_reference_blocks_without_exposing_names_or_values(
    missing: str, expected_code: str
) -> None:
    env = {
        "OPENAI_API_KEY": "openai-secret",
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
            "OPENAI_API_KEY": "openai-secret",
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
        "https://api.openai.com/v1/models",
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
            "OPENAI_API_KEY": "openai-secret",
            "ELEVENLABS_API_KEY": "eleven-secret",
            "HISTORY_SLEEP_VOICE_ID": "voice-secret",
        },
        request_json=_request_json,
        run_command=_run_command,
        higgsfield_module=InvalidQuoteHiggsfield(),
    )

    assert report["ready"] is False
    assert report["capabilities"][2]["reason_code"] == "credit_quote_invalid"
