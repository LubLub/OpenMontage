"""Tests for the Higgsfield CLI provider stack.

Every credit-spending and network path is mocked — NO live `generate create`,
no real downloads, no real `account status` beyond what monkeypatch fakes.
"""

from __future__ import annotations

import json
import math
import subprocess
from pathlib import Path

import pytest

from lib import higgsfield_cli
from lib import higgsfield_preflight
from tools.graphics.higgsfield_nano_banana import HiggsfieldNanoBanana
from tools.video.higgsfield_cli_video import HiggsfieldCliVideo


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #

def _completed(stdout: str = "", stderr: str = "", returncode: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=["higgsfield"], returncode=returncode, stdout=stdout, stderr=stderr)


@pytest.fixture(autouse=True)
def _fake_binary(monkeypatch):
    """Pretend the CLI is installed so locate_binary() never returns None."""
    monkeypatch.setattr(higgsfield_cli, "locate_binary", lambda: "/opt/homebrew/bin/higgsfield")


def _authorization(tool: str, model: str, params: dict, max_credits: float) -> dict:
    return {
        "approval_id": "approval-1",
        "paid_actions_authorized": True,
        "tool": tool,
        "model": model,
        "max_credits": max_credits,
        "request_sha256": higgsfield_cli.spend_request_hash(
            tool=tool,
            model=model,
            params=params,
        ),
    }


# --------------------------------------------------------------------------- #
# auth_guard
# --------------------------------------------------------------------------- #

def test_auth_guard_fails_when_live_probe_fails(monkeypatch):
    # account status exits non-zero -> auth_error -> not ok.
    monkeypatch.setattr(higgsfield_cli, "_run_cli", lambda args, timeout=30: _completed(stderr="unauthorized", returncode=1))
    ok, warnings = higgsfield_cli.auth_guard()
    assert ok is False
    assert any("auth" in w.lower() or "login" in w.lower() for w in warnings)


def test_auth_status_classifies_timeout_without_raising(monkeypatch):
    monkeypatch.setattr(
        higgsfield_cli,
        "_run_cli",
        lambda args, timeout=30: (_ for _ in ()).throw(
            subprocess.TimeoutExpired("higgsfield", timeout)
        ),
    )

    status = higgsfield_cli.auth_status()

    assert status == {"state": "auth_error", "error": "account status probe failed"}


def test_auth_status_rejects_non_object_json(monkeypatch):
    monkeypatch.setattr(
        higgsfield_cli,
        "_run_cli",
        lambda args, timeout=30: _completed(stdout="[]"),
    )

    status = higgsfield_cli.auth_status()

    assert status == {"state": "auth_error", "error": "account status returned invalid data"}


def test_auth_guard_ok_with_empty_refresh_token_but_warns(monkeypatch, tmp_path):
    """KEY REGRESSION: the user's real setup has an empty refresh_token but a
    working live probe. auth_guard must return ok=True WITH a warning, never
    hard-fail on the empty refresh token."""
    creds = tmp_path / "credentials.json"
    creds.write_text(json.dumps({"access_token": "x", "refresh_token": "", "scope": "s", "token_type": "Bearer"}))
    monkeypatch.setattr(higgsfield_cli, "CREDENTIALS_PATH", creds)

    def fake_run(args, timeout=30):
        assert args[:2] == ["account", "status"]  # only the FREE probe is used
        return _completed(stdout=json.dumps({"credits": 10, "email": "u@example.com", "subscription_plan_type": "free"}))

    monkeypatch.setattr(higgsfield_cli, "_run_cli", fake_run)

    ok, warnings = higgsfield_cli.auth_guard()
    assert ok is True
    assert any("refresh token" in w.lower() for w in warnings), warnings


def test_auth_guard_ok_no_warning_with_refresh_token(monkeypatch, tmp_path):
    creds = tmp_path / "credentials.json"
    creds.write_text(json.dumps({"access_token": "x", "refresh_token": "present"}))
    monkeypatch.setattr(higgsfield_cli, "CREDENTIALS_PATH", creds)
    monkeypatch.setattr(
        higgsfield_cli, "_run_cli",
        lambda args, timeout=30: _completed(stdout=json.dumps({"credits": 10, "email": "u@e.com", "subscription_plan_type": "free"})),
    )
    ok, warnings = higgsfield_cli.auth_guard()
    assert ok is True
    assert not any("refresh token" in w.lower() for w in warnings)


def test_auth_guard_treats_non_object_credentials_as_advisory_warning(
    monkeypatch,
    tmp_path,
):
    credentials = tmp_path / "credentials.json"
    credentials.write_text("[]")
    monkeypatch.setattr(higgsfield_cli, "CREDENTIALS_PATH", credentials)
    monkeypatch.setattr(
        higgsfield_cli,
        "_run_cli",
        lambda args, timeout=30: _completed(stdout='{"credits": 10}'),
    )

    ok, warnings = higgsfield_cli.auth_guard()

    assert ok is True
    assert any("refresh token" in warning.lower() for warning in warnings)


# --------------------------------------------------------------------------- #
# estimate_credits + per-model flag construction
# --------------------------------------------------------------------------- #

def test_estimate_credits_parses_credits(monkeypatch):
    monkeypatch.setattr(higgsfield_cli, "_run_cli", lambda args, timeout=60: _completed(stdout='{"credits": 10}'))
    assert higgsfield_cli.estimate_credits("kling3_0", {"prompt": "x"}) == 10.0


@pytest.mark.parametrize("credits", [-1, math.nan, math.inf, True, "not-a-number"])
def test_estimate_credits_rejects_invalid_quotes(monkeypatch, credits):
    monkeypatch.setattr(
        higgsfield_cli,
        "_run_cli",
        lambda args, timeout=60: _completed(stdout=json.dumps({"credits": credits})),
    )

    with pytest.raises(higgsfield_cli.HiggsfieldCLIError, match="invalid credits"):
        higgsfield_cli.estimate_credits("kling3_0", {"prompt": "x"})


def test_estimate_credits_kling_uses_mode_not_resolution(monkeypatch):
    captured = {}

    def fake_run(args, timeout=60):
        captured["args"] = args
        return _completed(stdout='{"credits": 12.5}')

    monkeypatch.setattr(higgsfield_cli, "_run_cli", fake_run)
    credits = higgsfield_cli.estimate_credits("kling3_0", {"prompt": "p", "mode": "pro", "duration": 5, "aspect_ratio": "16:9"})
    assert credits == 12.5
    args = captured["args"]
    assert args[:3] == ["generate", "cost", "kling3_0"]
    assert "--mode" in args and "pro" in args
    assert "--resolution" not in args  # kling has no resolution param
    assert "--aspect-ratio" in args  # dash-cased


def test_estimate_credits_seedance_uses_resolution_and_bool_audio(monkeypatch):
    captured = {}
    monkeypatch.setattr(higgsfield_cli, "_run_cli", lambda args, timeout=60: (captured.setdefault("args", args), _completed(stdout='{"credits": 22.5}'))[1])
    higgsfield_cli.estimate_credits(
        "seedance_2_0",
        {"prompt": "p", "resolution": "720p", "mode": "std", "generate_audio": False, "image_references": ["/a.png", "/b.png"]},
    )
    args = captured["args"]
    assert "--resolution" in args and "720p" in args
    # bool renders as explicit --generate-audio false
    gi = args.index("--generate-audio")
    assert args[gi + 1] == "false"
    # repeated media flag
    assert args.count("--image-references") == 2


def test_no_spend_preflight_uses_only_status_and_cost_commands(monkeypatch):
    calls: list[list[str]] = []

    def fake_run(args, timeout=60):
        calls.append(args)
        if args[:2] == ["account", "status"]:
            return _completed(stdout='{"credits": 100, "subscription_plan_type": "free"}')
        if args[:2] == ["generate", "cost"]:
            return _completed(stdout='{"credits": 10}')
        return pytest.fail(f"unsafe preflight command: {args}")

    monkeypatch.setattr(higgsfield_cli, "_run_cli", fake_run)

    assert higgsfield_cli.auth_status()["state"] == "ok"
    result = higgsfield_preflight.preflight_shotlist(
        [{"id": "s1", "model": "kling3_0", "params": {"mode": "std"}}],
        episode_credit_cap=50.0,
        monthly_ledger_path=None,
        monthly_ceiling=1000.0,
    )

    assert result["decision"] == "ok"
    assert calls
    assert all(call[:2] in (["account", "status"], ["generate", "cost"]) for call in calls)
    assert not any(call[:2] == ["generate", "create"] for call in calls)


def test_build_param_args_does_not_mutate_input():
    params = {"prompt": "p", "image_references": ["/a.png"]}
    snapshot = json.dumps(params, sort_keys=True)
    higgsfield_cli.build_param_args(params)
    assert json.dumps(params, sort_keys=True) == snapshot


# --------------------------------------------------------------------------- #
# generate() output-URL extraction (no network)
# --------------------------------------------------------------------------- #

def test_generate_extracts_id_and_downloads(monkeypatch, tmp_path):
    job_json = json.dumps({"id": "job-123", "results": [{"url": "https://cdn/out.png"}]})
    monkeypatch.setattr(higgsfield_cli, "_run_cli", lambda args, timeout=None: _completed(stdout=job_json))

    downloaded = {}

    def fake_download(url, output_path, timeout=300):
        downloaded["url"] = url
        Path(output_path).write_bytes(b"PNGDATA")

    monkeypatch.setattr(higgsfield_cli, "_download", fake_download)

    out = tmp_path / "out.png"
    params = {"prompt": "p"}
    result = higgsfield_cli.generate(
        "nano_banana_2",
        params,
        out,
        tool="higgsfield_nano_banana",
        spend_authorization=_authorization(
            "higgsfield_nano_banana", "nano_banana_2", params, 2.0
        ),
        quoted_credits=2.0,
    )
    assert result["job_id"] == "job-123"
    assert downloaded["url"] == "https://cdn/out.png"
    assert out.read_bytes() == b"PNGDATA"
    assert "output_url" not in result
    assert "raw" not in result


def test_provider_result_does_not_expose_signed_output_url(monkeypatch, tmp_path):
    import lib.higgsfield_cli as hc

    secret = "signed-query-secret"
    monkeypatch.setattr(hc, "auth_guard", lambda: (True, []))
    monkeypatch.setattr(hc, "estimate_credits", lambda model, params: 2.0)
    monkeypatch.setenv("HIGGSFIELD_CREDIT_USD", "0.01")

    def fake_generate(model, params, output_path, **kwargs):
        Path(output_path).write_bytes(b"IMG")
        return {
            "job_id": "job-1",
            "output_path": str(output_path),
            "output_url": f"https://cdn/out.png?token={secret}",
            "raw": {"secret": secret},
        }

    monkeypatch.setattr(hc, "generate", fake_generate)
    params = {"prompt": "x", "resolution": "2k"}
    result = HiggsfieldNanoBanana().execute(
        {
            "prompt": "x",
            "output_path": str(tmp_path / "out.png"),
            "spend_authorization": _authorization(
                "higgsfield_nano_banana", "nano_banana_2", params, 2.0
            ),
        }
    )

    serialized = json.dumps(result.data)
    assert result.success is True
    assert secret not in serialized
    assert "output_url" not in result.data


def test_download_streams_with_a_size_cap_and_preserves_existing_output(
    monkeypatch,
    tmp_path,
):
    class Response:
        headers = {}

        def raise_for_status(self):
            return None

        def iter_content(self, chunk_size):
            yield b"a" * 6
            yield b"b" * 6

        def close(self):
            return None

    monkeypatch.setattr("requests.get", lambda *args, **kwargs: Response())
    output = tmp_path / "existing.bin"
    output.write_bytes(b"original")

    with pytest.raises(higgsfield_cli.HiggsfieldCLIError, match="size limit"):
        higgsfield_cli._download(
            "https://cdn.example/out.bin",
            output,
            max_bytes=10,
        )

    assert output.read_bytes() == b"original"
    assert list(tmp_path.glob("*.part")) == []


def test_generate_download_failure_preserves_paid_job_id(monkeypatch, tmp_path):
    job_json = json.dumps({"id": "job-paid-123", "url": "https://cdn/out.png"})
    monkeypatch.setattr(
        higgsfield_cli,
        "_run_cli",
        lambda args, timeout=None: _completed(stdout=job_json),
    )
    monkeypatch.setattr(
        higgsfield_cli,
        "_download",
        lambda *args, **kwargs: (_ for _ in ()).throw(TimeoutError("signed detail")),
    )

    with pytest.raises(higgsfield_cli.HiggsfieldJobRecoveryRequired) as exc:
        higgsfield_cli.generate(
            "nano_banana_2",
            (params := {"prompt": "x"}),
            tmp_path / "out.png",
            tool="higgsfield_nano_banana",
            spend_authorization=_authorization(
                "higgsfield_nano_banana", "nano_banana_2", params, 2.0
            ),
            quoted_credits=2.0,
        )

    assert exc.value.job_id == "job-paid-123"
    assert exc.value.output_path == tmp_path / "out.png"
    assert "signed detail" not in str(exc.value)


@pytest.mark.parametrize("returncode", [0, 1])
def test_generate_preserves_job_id_when_wait_result_has_no_output_url(
    monkeypatch,
    tmp_path,
    returncode,
):
    monkeypatch.setattr(
        higgsfield_cli,
        "_run_cli",
        lambda args, timeout=None: _completed(
            stdout='{"id": "job-recover-456"}',
            returncode=returncode,
        ),
    )

    with pytest.raises(higgsfield_cli.HiggsfieldJobRecoveryRequired) as exc:
        higgsfield_cli.generate(
            "kling3_0",
            (params := {"prompt": "x"}),
            tmp_path / "out.mp4",
            tool="higgsfield_cli_video",
            spend_authorization=_authorization(
                "higgsfield_cli_video", "kling3_0", params, 10.0
            ),
            quoted_credits=10.0,
        )

    assert exc.value.job_id == "job-recover-456"


def test_credit_conversion_requires_explicit_positive_rate(monkeypatch):
    monkeypatch.delenv("HIGGSFIELD_CREDIT_USD", raising=False)
    with pytest.raises(higgsfield_cli.HiggsfieldCLIError, match="HIGGSFIELD_CREDIT_USD"):
        higgsfield_cli.credits_to_usd(10)

    monkeypatch.setenv("HIGGSFIELD_CREDIT_USD", "0.02")
    assert higgsfield_cli.credits_to_usd(10) == 0.2


def test_direct_generate_cannot_bypass_spend_authorization(monkeypatch, tmp_path):
    monkeypatch.setattr(
        higgsfield_cli,
        "_run_cli",
        lambda *args, **kwargs: pytest.fail("unauthorized call reached the CLI"),
    )

    with pytest.raises(higgsfield_cli.HiggsfieldCLIError, match="authorization"):
        higgsfield_cli.generate(
            "kling3_0",
            {"prompt": "x"},
            tmp_path / "out.mp4",
            tool="higgsfield_cli_video",
            spend_authorization=None,
            quoted_credits=10.0,
        )


def test_cli_failures_do_not_echo_provider_output(monkeypatch):
    secret = "signed-secret-token"
    monkeypatch.setattr(
        higgsfield_cli,
        "_run_cli",
        lambda args, timeout=60: _completed(stderr=secret, returncode=1),
    )

    with pytest.raises(higgsfield_cli.HiggsfieldCLIError) as exc:
        higgsfield_cli.estimate_credits("kling3_0", {"prompt": "x"})

    assert secret not in str(exc.value)


# --------------------------------------------------------------------------- #
# provider execute() — mock helper.generate
# --------------------------------------------------------------------------- #

def test_nano_banana_execute_success(monkeypatch, tmp_path):
    import lib.higgsfield_cli as hc

    monkeypatch.setattr(hc, "auth_guard", lambda: (True, []))
    monkeypatch.setattr(hc, "estimate_credits", lambda model, params: 2.0)
    monkeypatch.setenv("HIGGSFIELD_CREDIT_USD", "0.01")

    out = tmp_path / "img.png"

    def fake_generate(model, params, output_path, wait_timeout=600, **kwargs):
        assert model == "nano_banana_2"
        Path(output_path).write_bytes(b"IMG")
        return {"job_id": "j1", "output_path": str(output_path), "output_url": "https://cdn/i.png", "raw": {}}

    monkeypatch.setattr(hc, "generate", fake_generate)

    result = HiggsfieldNanoBanana().execute(
        {
            "prompt": "a fox",
            "output_path": str(out),
            "spend_authorization": _authorization(
                "higgsfield_nano_banana",
                "nano_banana_2",
                {"prompt": "a fox", "resolution": "2k"},
                2.0,
            ),
        }
    )
    assert result.success is True
    assert out.exists()
    assert str(out) in result.artifacts
    assert result.data["credits"] == 2.0
    assert result.data["model"] == "nano_banana_2"


def test_cli_video_execute_success_default_model(monkeypatch, tmp_path):
    import lib.higgsfield_cli as hc

    monkeypatch.setattr(hc, "auth_guard", lambda: (True, []))
    monkeypatch.setattr(hc, "estimate_credits", lambda model, params: 10.0)
    monkeypatch.setenv("HIGGSFIELD_CREDIT_USD", "0.01")

    seen = {}

    def fake_generate(model, params, output_path, wait_timeout=600, **kwargs):
        seen["model"] = model
        seen["params"] = params
        Path(output_path).write_bytes(b"VID")
        return {"job_id": "v1", "output_path": str(output_path), "output_url": "https://cdn/v.mp4", "raw": {}}

    monkeypatch.setattr(hc, "generate", fake_generate)

    out = tmp_path / "clip.mp4"
    result = HiggsfieldCliVideo().execute(
        {
            "prompt": "slow push over a reef",
            "output_path": str(out),
            "spend_authorization": _authorization(
                "higgsfield_cli_video",
                "kling3_0",
                {
                    "prompt": "slow push over a reef",
                    "duration": 5,
                    "aspect_ratio": "16:9",
                    "mode": "std",
                },
                10.0,
            ),
        }
    )
    assert result.success is True
    assert seen["model"] == "kling3_0"  # cost-optimal default
    assert "resolution" not in seen["params"]  # kling has no resolution param
    assert result.data["credits"] == 10.0
    assert str(out) in result.artifacts


def test_cli_video_execute_auth_failure_returns_error(monkeypatch):
    import lib.higgsfield_cli as hc

    monkeypatch.setattr(hc, "auth_guard", lambda: (False, ["auth probe failed"]))
    result = HiggsfieldCliVideo().execute({"prompt": "x"})
    assert result.success is False
    assert "not authenticated" in result.error


def test_paid_provider_refuses_to_generate_without_matching_authorization(monkeypatch):
    import lib.higgsfield_cli as hc

    monkeypatch.setattr(hc, "auth_guard", lambda: (True, []))
    monkeypatch.setattr(hc, "estimate_credits", lambda model, params: 10.0)
    monkeypatch.setattr(
        hc,
        "generate",
        lambda *args, **kwargs: pytest.fail("generate must not run without approval"),
    )

    result = HiggsfieldCliVideo().execute({"prompt": "x"})

    assert result.success is False
    assert "authorization" in result.error.lower()


def test_paid_provider_refuses_to_generate_when_live_quote_is_unavailable(monkeypatch):
    import lib.higgsfield_cli as hc

    monkeypatch.setattr(hc, "auth_guard", lambda: (True, []))
    monkeypatch.setattr(
        hc,
        "estimate_credits",
        lambda model, params: (_ for _ in ()).throw(hc.HiggsfieldCLIError("down")),
    )
    monkeypatch.setattr(
        hc,
        "generate",
        lambda *args, **kwargs: pytest.fail("generate must not run without a live quote"),
    )

    result = HiggsfieldCliVideo().execute(
        {
            "prompt": "x",
            "spend_authorization": _authorization(
                "higgsfield_cli_video",
                "kling3_0",
                {"prompt": "x", "duration": 5, "aspect_ratio": "16:9", "mode": "std"},
                10.0,
            ),
        }
    )

    assert result.success is False
    assert "live credit quote" in result.error.lower()


def test_paid_provider_refuses_to_generate_without_usd_rate(monkeypatch):
    import lib.higgsfield_cli as hc

    monkeypatch.setattr(hc, "auth_guard", lambda: (True, []))
    monkeypatch.setattr(hc, "estimate_credits", lambda model, params: 10.0)
    monkeypatch.delenv("HIGGSFIELD_CREDIT_USD", raising=False)
    monkeypatch.setattr(
        hc,
        "generate",
        lambda *args, **kwargs: pytest.fail("generate must not run without USD provenance"),
    )

    result = HiggsfieldCliVideo().execute(
        {
            "prompt": "x",
            "spend_authorization": _authorization(
                "higgsfield_cli_video",
                "kling3_0",
                {"prompt": "x", "duration": 5, "aspect_ratio": "16:9", "mode": "std"},
                10.0,
            ),
        }
    )

    assert result.success is False
    assert "HIGGSFIELD_CREDIT_USD" in result.error


def test_video_provider_rejects_unapproved_model_substitution(monkeypatch):
    import lib.higgsfield_cli as hc

    monkeypatch.setattr(hc, "auth_guard", lambda: (True, []))
    monkeypatch.setattr(
        hc,
        "generate",
        lambda *args, **kwargs: pytest.fail("invalid model must not generate"),
    )

    result = HiggsfieldCliVideo().execute(
        {
            "prompt": "x",
            "model": "unknown-model",
            "spend_authorization": {
                "approval_id": "approval-1",
                "paid_actions_authorized": True,
                "tool": "higgsfield_cli_video",
                "model": "unknown-model",
                "max_credits": 10.0,
            },
        }
    )

    assert result.success is False
    assert "model" in result.error.lower()


def test_video_params_apply_declared_defaults_and_operation_contract():
    provider = HiggsfieldCliVideo()

    assert provider._params({"prompt": "x"}) == {
        "prompt": "x",
        "duration": 5,
        "aspect_ratio": "16:9",
        "mode": "std",
    }
    with pytest.raises(ValueError, match="start_image"):
        provider._params({"prompt": "x", "operation": "image_to_video"})
    with pytest.raises(ValueError, match="text_to_video"):
        provider._params(
            {"prompt": "x", "operation": "text_to_video", "start_image": "frame.png"}
        )


def test_video_idempotency_covers_every_output_affecting_input():
    fields = set(HiggsfieldCliVideo.idempotency_key_fields)
    assert {
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
    } <= fields


# --------------------------------------------------------------------------- #
# preflight_shotlist
# --------------------------------------------------------------------------- #

def _pricer(model, params):
    """Deterministic fake pricer: seedance is expensive, kling is cheap,
    resolution/mode lower the price."""
    if model == "seedance_2_0":
        res = params.get("resolution", "1080p")
        return {"4k": 90.0, "1080p": 45.0, "720p": 22.5, "480p": 7.5}.get(res, 45.0)
    if model == "kling3_0":
        mode = params.get("mode", "std")
        return {"4k": 20.0, "pro": 12.5, "std": 10.0}.get(mode, 10.0)
    return 0.0


def test_preflight_degrades_seedance_to_kling_then_passes():
    shots = [
        {"id": "s1", "model": "seedance_2_0", "params": {"resolution": "1080p"}, "priority": 5},
        {"id": "s2", "model": "seedance_2_0", "params": {"resolution": "1080p"}, "priority": 5},
    ]
    # 2 x 45 = 90 over a cap of 25; after seedance->kling: 2 x 10 = 20 <= 25.
    out = higgsfield_preflight.preflight_shotlist(
        shots, episode_credit_cap=25.0, monthly_ledger_path=None, monthly_ceiling=10_000.0, pricer=_pricer,
    )
    assert out["decision"] == "ok"
    assert out["total_credits"] == 20.0
    assert "downgrade_video_model:seedance_2_0->kling3_0" in out["degrade_steps"]
    assert all(s["model"] == "kling3_0" for s in out["plan"])


def test_preflight_impossible_aborts():
    # One shot, cheapest possible kling std = 10, cap is 5 -> abort.
    shots = [{"id": "s1", "model": "seedance_2_0", "params": {"resolution": "1080p"}, "priority": 5}]
    out = higgsfield_preflight.preflight_shotlist(
        shots, episode_credit_cap=5.0, monthly_ledger_path=None, monthly_ceiling=10_000.0, pricer=_pricer,
    )
    assert out["decision"] == "abort_ask_human"
    assert "over the episode cap" in out["reason"]


@pytest.mark.parametrize(
    "pricer",
    [
        lambda model, params: (_ for _ in ()).throw(higgsfield_cli.HiggsfieldCLIError("down")),
        lambda model, params: math.nan,
        lambda model, params: -1.0,
    ],
)
def test_preflight_fails_closed_when_any_shot_cannot_be_priced(pricer):
    shots = [{"id": "unpriced", "model": "kling3_0", "params": {}}]

    out = higgsfield_preflight.preflight_shotlist(
        shots,
        episode_credit_cap=100.0,
        monthly_ledger_path=None,
        monthly_ceiling=1000.0,
        pricer=pricer,
    )

    assert out["decision"] == "abort_ask_human"
    assert out["quote_status"] == "unavailable"
    assert out["unpriced_shot_ids"] == ["unpriced"]
    assert out["total_credits"] is None


def test_preflight_fails_closed_when_shot_has_no_model():
    out = higgsfield_preflight.preflight_shotlist(
        [{"id": "missing-model", "params": {}}],
        episode_credit_cap=100.0,
        monthly_ledger_path=None,
        monthly_ceiling=1000.0,
        pricer=_pricer,
    )

    assert out["decision"] == "abort_ask_human"
    assert out["unpriced_shot_ids"] == ["missing-model"]


@pytest.mark.parametrize("cap", [math.nan, math.inf, -1.0])
def test_preflight_fails_closed_on_invalid_credit_caps(cap):
    out = higgsfield_preflight.preflight_shotlist(
        [{"id": "s1", "model": "kling3_0", "params": {}}],
        episode_credit_cap=cap,
        monthly_ledger_path=None,
        monthly_ceiling=1000.0,
        pricer=_pricer,
    )

    assert out["decision"] == "abort_ask_human"
    assert out["quote_status"] == "invalid_limits"


def test_preflight_drops_lowest_priority_shot():
    shots = [
        {"id": "keep", "model": "kling3_0", "params": {"mode": "std"}, "priority": 10},
        {"id": "drop", "model": "kling3_0", "params": {"mode": "std"}, "priority": 1},
    ]
    # 2 x 10 = 20 over cap 12; can't lower std further -> drop the priority-1 shot -> 10 <= 12.
    out = higgsfield_preflight.preflight_shotlist(
        shots, episode_credit_cap=12.0, monthly_ledger_path=None, monthly_ceiling=10_000.0, pricer=_pricer, min_shots=1,
    )
    assert out["decision"] == "ok"
    assert [s["id"] for s in out["plan"]] == ["keep"]
    assert "drop_lowest_priority_shot" in out["degrade_steps"]


def test_preflight_monthly_ceiling_enforced(tmp_path):
    ledger = tmp_path / "ledger.json"
    from lib.higgsfield_preflight import _current_month
    ledger.write_text(json.dumps({_current_month(): 9995.0}))
    shots = [{"id": "s1", "model": "kling3_0", "params": {"mode": "std"}, "priority": 5}]
    # Episode fits its own cap (10 <= 50) but 9995 + 10 = 10005 > 10000 ceiling.
    out = higgsfield_preflight.preflight_shotlist(
        shots, episode_credit_cap=50.0, monthly_ledger_path=str(ledger), monthly_ceiling=10_000.0, pricer=_pricer,
    )
    assert out["decision"] == "abort_ask_human"
    assert out["monthly_ok"] is False
    assert "monthly ceiling" in out["reason"]


@pytest.mark.parametrize("payload", ["not-json", "[]", '{"2026-07": -1}', '{"2026-07": NaN}'])
def test_preflight_fails_closed_when_monthly_ledger_is_untrustworthy(
    tmp_path,
    monkeypatch,
    payload,
):
    ledger = tmp_path / "ledger.json"
    ledger.write_text(payload)
    monkeypatch.setattr(higgsfield_preflight, "_current_month", lambda: "2026-07")

    out = higgsfield_preflight.preflight_shotlist(
        [{"id": "s1", "model": "kling3_0", "params": {"mode": "std"}}],
        episode_credit_cap=50.0,
        monthly_ledger_path=ledger,
        monthly_ceiling=1000.0,
        pricer=_pricer,
    )

    assert out["decision"] == "abort_ask_human"
    assert out["ledger_status"] == "unavailable"
    assert out["month_to_date_before"] is None


def test_preflight_refuses_to_commit_estimated_credits(tmp_path):
    ledger = tmp_path / "ledger.json"
    from lib.higgsfield_preflight import month_to_date_credits
    shots = [{"id": "s1", "model": "kling3_0", "params": {"mode": "std"}, "priority": 5}]
    with pytest.raises(ValueError, match="actual spend"):
        higgsfield_preflight.preflight_shotlist(
            shots,
            episode_credit_cap=50.0,
            monthly_ledger_path=str(ledger),
            monthly_ceiling=10_000.0,
            pricer=_pricer,
            commit=True,
        )
    assert month_to_date_credits(str(ledger)) == 0.0
    assert not ledger.exists()


# --------------------------------------------------------------------------- #
# registry discovery
# --------------------------------------------------------------------------- #

def test_registry_discovers_both_providers():
    from tools.tool_registry import ToolRegistry

    reg = ToolRegistry()
    reg.discover()
    image_names = {t.name for t in reg.get_by_capability("image_generation")}
    video_names = {t.name for t in reg.get_by_capability("video_generation")}
    assert "higgsfield_nano_banana" in image_names
    assert "higgsfield_cli_video" in video_names
