"""Shared helper for driving the Higgsfield CLI (`higgsfield ...`).

This is the single seam between OpenMontage's Higgsfield provider tools and the
locally-installed `higgsfield` binary. It deliberately keeps all subprocess and
network IO behind small, patchable module-level functions so tests can mock the
credit-spending path without ever touching the real CLI.

Auth model (NOT the HTTP API): the CLI carries its own OAuth token in
``~/.higgsfield/credentials.json``. We never read or echo that token. The
authoritative liveness check is the FREE ``account status`` probe.

Cost model: Higgsfield prices in *credits*. ``estimate_credits`` returns the
real credit figure from the FREE ``generate cost`` command; a USD conversion is
layered on top by callers via ``HIGGSFIELD_CREDIT_USD`` (see credits_to_usd).
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

# Path to the CLI's OAuth credential store. Read only to WARN about a missing
# refresh token — never to authenticate (the CLI does that itself).
CREDENTIALS_PATH = Path.home() / ".higgsfield" / "credentials.json"

_NO_REFRESH_WARNING = (
    "no refresh token in ~/.higgsfield/credentials.json — the CLI token will "
    "need a manual `higgsfield auth login` when it expires"
)


class HiggsfieldCLIError(RuntimeError):
    """Raised when a Higgsfield CLI invocation fails in a non-recoverable way."""


class HiggsfieldJobRecoveryRequired(HiggsfieldCLIError):
    """A paid job completed, but its output still needs safe recovery."""

    def __init__(self, *, job_id: str | None, output_path: Path) -> None:
        super().__init__("Higgsfield job was created; output recovery is required")
        self.job_id = job_id
        self.output_path = output_path


def validate_spend_authorization(
    authorization: Any,
    *,
    tool: str,
    model: str,
    params: dict[str, Any],
    quoted_credits: float | None = None,
) -> str | None:
    """Return an error when a paid call is not bound to explicit approval."""
    if not isinstance(authorization, dict):
        return "Paid generation requires explicit spend authorization"
    approval_id = authorization.get("approval_id")
    if not isinstance(approval_id, str) or not approval_id.strip():
        return "Spend authorization lacks an approval_id"
    if authorization.get("paid_actions_authorized") is not True:
        return "Spend authorization does not authorize paid actions"
    if authorization.get("tool") != tool or authorization.get("model") != model:
        return "Spend authorization does not match the selected tool and model"
    if authorization.get("request_sha256") != spend_request_hash(
        tool=tool,
        model=model,
        params=params,
    ):
        return "Spend authorization does not match the approved generation request"
    try:
        maximum = float(authorization["max_credits"])
    except (KeyError, TypeError, ValueError):
        return "Spend authorization lacks a valid credit ceiling"
    if not math.isfinite(maximum) or maximum < 0:
        return "Spend authorization lacks a valid credit ceiling"
    if quoted_credits is not None:
        if not math.isfinite(quoted_credits) or quoted_credits < 0:
            return "Live credit quote is invalid"
        if quoted_credits > maximum:
            return "Live credit quote exceeds the approved credit ceiling"
    return None


def spend_request_hash(*, tool: str, model: str, params: dict[str, Any]) -> str:
    """Bind an approval to the exact paid tool, native model, and inputs."""
    body = json.dumps(
        {"tool": tool, "model": model, "params": params},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(body).hexdigest()}"


# --------------------------------------------------------------------------- #
# Binary location
# --------------------------------------------------------------------------- #

def locate_binary() -> str | None:
    """Return the path to the `higgsfield` binary, or None if not found.

    Honors a HIGGSFIELD_BIN override before falling back to PATH lookup.
    """
    override = os.environ.get("HIGGSFIELD_BIN")
    if override:
        return override if (Path(override).exists() or shutil.which(override)) else None
    return shutil.which("higgsfield")


# --------------------------------------------------------------------------- #
# Low-level subprocess seam (patch this in tests)
# --------------------------------------------------------------------------- #

def _run_cli(args: list[str], *, timeout: int = 120) -> subprocess.CompletedProcess:
    """Run `higgsfield <args>` and return the CompletedProcess.

    This is the single subprocess call site. Tests monkeypatch this (or the
    higher-level helpers that wrap it) to avoid invoking the real CLI. It never
    raises on a non-zero exit — callers inspect returncode/stderr themselves so
    they can classify auth errors distinctly from missing-binary.
    """
    binary = locate_binary()
    if binary is None:
        raise HiggsfieldCLIError(
            "higgsfield CLI not found. Install it and/or set HIGGSFIELD_BIN."
        )
    return subprocess.run(
        [binary, *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )


def _parse_json_stdout(stdout: str) -> Any:
    """Parse JSON from CLI stdout, tolerating leading/trailing non-JSON noise.

    The CLI with --json prints a JSON document; some subcommands may prefix a
    status line. We locate the first balanced JSON object/array and parse it.
    """
    text = stdout.strip()
    if not text:
        raise HiggsfieldCLIError("Higgsfield CLI returned empty output where JSON was expected")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Fallback: find the first '{' or '[' and try to decode from there.
    for opener in ("{", "["):
        idx = text.find(opener)
        if idx != -1:
            try:
                return json.loads(text[idx:])
            except json.JSONDecodeError:
                continue
    raise HiggsfieldCLIError("Higgsfield CLI returned invalid JSON")


# --------------------------------------------------------------------------- #
# Param -> CLI flag mapping (per model)
# --------------------------------------------------------------------------- #

# Media params that map to repeated media flags taking a path-or-id. The CLI
# auto-uploads local paths, so we pass paths straight through.
_ARRAY_MEDIA_FLAGS = {
    "image_references": "--image-references",
    "video_references": "--video-references",
    "audio_references": "--audio-references",
}
_SINGLE_MEDIA_FLAGS = {
    "start_image": "--start-image",
    "end_image": "--end-image",
}


def _flag_for(param: str) -> str:
    """Convert an underscore param name to its dash-cased CLI flag."""
    return "--" + param.replace("_", "-")


def build_param_args(params: dict[str, Any]) -> list[str]:
    """Translate a param dict into an ordered list of CLI args.

    Rules (confirmed via `higgsfield generate create --help` and `model get`):
      * flags are dash-cased (aspect_ratio -> --aspect-ratio)
      * array media refs use repeated flags (--image-references a --image-references b)
      * start_image/end_image take a single path-or-id
      * booleans render as `--flag true` / `--flag false` (the CLI accepts the
        explicit value form; e.g. --generate-audio false)
      * None values are skipped entirely (immutable: input dict is never mutated)
    """
    args: list[str] = []
    for key, value in params.items():
        if value is None:
            continue
        if key in _ARRAY_MEDIA_FLAGS:
            flag = _ARRAY_MEDIA_FLAGS[key]
            items = value if isinstance(value, (list, tuple)) else [value]
            for item in items:
                if item is None:
                    continue
                args.extend([flag, str(item)])
            continue
        if key in _SINGLE_MEDIA_FLAGS:
            args.extend([_SINGLE_MEDIA_FLAGS[key], str(value)])
            continue
        if isinstance(value, bool):
            args.extend([_flag_for(key), "true" if value else "false"])
            continue
        args.extend([_flag_for(key), str(value)])
    return args


# --------------------------------------------------------------------------- #
# Auth (FREE probes only)
# --------------------------------------------------------------------------- #

def auth_status() -> dict[str, Any]:
    """Run the FREE `account status --json` probe and classify the result.

    Returns a dict:
      {"state": "ok", "credits": N, "email": ..., "plan": ...}
      {"state": "auth_error", "error": "..."}
      {"state": "cli_missing", "error": "..."}

    Never raises; never echoes the token.
    """
    if locate_binary() is None:
        return {"state": "cli_missing", "error": "higgsfield CLI not found on PATH"}
    try:
        proc = _run_cli(["account", "status", "--json"], timeout=30)
    except HiggsfieldCLIError:
        return {"state": "cli_missing", "error": "higgsfield CLI unavailable"}
    except (OSError, subprocess.SubprocessError):
        return {"state": "auth_error", "error": "account status probe failed"}
    if proc.returncode != 0:
        # Non-zero exit from `account status` means the token is invalid/expired
        # or the account is unreachable — an auth-layer failure, not a bug.
        return {"state": "auth_error", "error": "account status returned non-zero exit"}
    try:
        payload = _parse_json_stdout(proc.stdout)
    except HiggsfieldCLIError:
        return {"state": "auth_error", "error": "account status returned invalid data"}
    if not isinstance(payload, dict):
        return {"state": "auth_error", "error": "account status returned invalid data"}
    return {
        "state": "ok",
        "credits": payload.get("credits"),
        "email": payload.get("email"),
        "plan": payload.get("subscription_plan_type"),
    }


def _read_refresh_token_present() -> bool:
    """Return True iff credentials.json has a non-empty refresh_token.

    Reads the file only to decide whether to warn. The token value is never
    returned, logged, or echoed.
    """
    try:
        with open(CREDENTIALS_PATH, encoding="utf-8") as fh:
            creds = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(creds, dict):
        return False
    return bool(str(creds.get("refresh_token") or "").strip())


def auth_guard() -> tuple[bool, list[str]]:
    """Authoritative readiness check for Higgsfield-backed tools.

    ok is driven SOLELY by the live `account status` probe. The refresh-token
    check is advisory only: an empty/missing refresh_token appends a WARNING but
    NEVER flips ok to False (the user's working setup legitimately has an empty
    refresh_token — hard-failing on it would be a bug).

    Returns (ok, warnings).
    """
    warnings: list[str] = []
    status = auth_status()
    ok = status.get("state") == "ok"
    if not ok:
        state = status.get("state")
        if state == "cli_missing":
            warnings.append("higgsfield CLI not found — install it or set HIGGSFIELD_BIN")
        else:
            warnings.append(
                "higgsfield auth probe failed — run `higgsfield auth login` "
                f"({status.get('error', 'unknown error')})"
            )
    if not _read_refresh_token_present():
        warnings.append(_NO_REFRESH_WARNING)
    return ok, warnings


# --------------------------------------------------------------------------- #
# Cost estimation (FREE)
# --------------------------------------------------------------------------- #

def estimate_credits(model: str, params: dict[str, Any]) -> float:
    """Run the FREE `generate cost <model> ... --json` and return credit count.

    Raises HiggsfieldCLIError on failure so callers can decide how to degrade.
    """
    args = ["generate", "cost", model, *build_param_args(params), "--json"]
    proc = _run_cli(args, timeout=60)
    if proc.returncode != 0:
        raise HiggsfieldCLIError(f"`generate cost {model}` failed")
    payload = _parse_json_stdout(proc.stdout)
    credits = payload.get("credits") if isinstance(payload, dict) else None
    if credits is None:
        raise HiggsfieldCLIError(f"`generate cost {model}` returned no credits field")
    if isinstance(credits, bool):
        raise HiggsfieldCLIError(f"`generate cost {model}` returned invalid credits")
    try:
        quoted = float(credits)
    except (TypeError, ValueError) as exc:
        raise HiggsfieldCLIError(
            f"`generate cost {model}` returned invalid credits"
        ) from exc
    if not math.isfinite(quoted) or quoted < 0:
        raise HiggsfieldCLIError(f"`generate cost {model}` returned invalid credits")
    return quoted


# --------------------------------------------------------------------------- #
# Generation (COSTS CREDITS — the only spending path; fully mockable)
# --------------------------------------------------------------------------- #

# Keys the CLI job JSON may use for the job id and the output URL. We accept a
# range because the exact field is undocumented and the CLI is versioned; this
# keeps the parser resilient without guessing a single brittle key.
_JOB_ID_KEYS = ("id", "job_id", "jobId", "uuid")
_OUTPUT_URL_KEYS = ("url", "output_url", "outputUrl", "result_url", "resultUrl", "output", "video_url", "image_url")


def _extract_job_id(job: Any) -> str | None:
    if not isinstance(job, dict):
        return None
    for key in _JOB_ID_KEYS:
        val = job.get(key)
        if isinstance(val, str) and val:
            return val
    return None


def _extract_output_url(job: Any) -> str | None:
    """Pull the first output URL from a job JSON object.

    Handles both flat (job["url"]) and nested (job["results"][0]["url"] /
    job["outputs"][0]["url"] / job["output"]["url"]) shapes.
    """
    if not isinstance(job, dict):
        return None
    # Flat string fields.
    for key in _OUTPUT_URL_KEYS:
        val = job.get(key)
        if isinstance(val, str) and val.startswith(("http://", "https://")):
            return val
    # Nested collections of result objects.
    for coll_key in ("results", "outputs", "assets", "media"):
        coll = job.get(coll_key)
        if isinstance(coll, list):
            for item in coll:
                nested = _extract_output_url(item)
                if nested:
                    return nested
    # Nested single object.
    for obj_key in ("output", "result", "asset"):
        nested = _extract_output_url(job.get(obj_key))
        if nested:
            return nested
    return None


def _download(
    url: str,
    output_path: Path,
    *,
    timeout: int = 300,
    max_bytes: int = 512 * 1024 * 1024,
) -> None:
    """Stream a bounded response to a temporary file, then replace atomically."""
    import requests

    output_path.parent.mkdir(parents=True, exist_ok=True)
    response = requests.get(url, timeout=timeout, stream=True)
    temporary_path: Path | None = None
    try:
        response.raise_for_status()
        length = response.headers.get("Content-Length")
        if length is not None:
            try:
                declared_size = int(length)
            except ValueError as exc:
                raise HiggsfieldCLIError("Higgsfield output has an invalid size") from exc
            if declared_size > max_bytes:
                raise HiggsfieldCLIError("Higgsfield output exceeds the download size limit")

        written = 0
        with tempfile.NamedTemporaryFile(
            dir=output_path.parent,
            prefix=f"{output_path.name}.",
            suffix=".part",
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if not chunk:
                    continue
                written += len(chunk)
                if written > max_bytes:
                    raise HiggsfieldCLIError(
                        "Higgsfield output exceeds the download size limit"
                    )
                temporary.write(chunk)
        if written == 0:
            raise HiggsfieldCLIError("Higgsfield output is empty")
        temporary_path.replace(output_path)
        temporary_path = None
    finally:
        response.close()
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def generate(
    model: str,
    params: dict[str, Any],
    output_path: str | Path,
    *,
    tool: str,
    spend_authorization: Any,
    quoted_credits: float,
    wait_timeout: int = 600,
) -> dict[str, Any]:
    """Create a Higgsfield job, wait for it, and download the output.

    THIS SPENDS CREDITS. Every seam it uses (`_run_cli`, `_download`) is a
    module-level function so tests can patch them and never hit the network.

    Returns only the durable recovery ID and local output path. Provider payloads
    and signed download URLs are deliberately not exposed.

    Raises HiggsfieldCLIError if the CLI fails or no output URL is produced.
    The job id is extracted and included in the return value immediately so a
    caller can persist it even if the download later fails.
    """
    authorization_error = validate_spend_authorization(
        spend_authorization,
        tool=tool,
        model=model,
        params=params,
        quoted_credits=quoted_credits,
    )
    if authorization_error:
        raise HiggsfieldCLIError(authorization_error)

    out = Path(output_path)
    # `--wait` blocks until the job finishes; `--wait-timeout` bounds it.
    args = [
        "generate", "create", model,
        *build_param_args(params),
        "--wait",
        "--wait-timeout", f"{int(wait_timeout)}s",
        "--json",
    ]
    proc = _run_cli(args, timeout=wait_timeout + 60)
    try:
        job = _parse_json_stdout(proc.stdout)
    except HiggsfieldCLIError:
        job = {}
    # The CLI may print a list (one entry per job) or a single object.
    if isinstance(job, list):
        job = job[0] if job else {}

    job_id = _extract_job_id(job)
    if proc.returncode != 0:
        if job_id:
            raise HiggsfieldJobRecoveryRequired(job_id=job_id, output_path=out)
        raise HiggsfieldCLIError(f"`generate create {model}` failed")

    output_url = _extract_output_url(job)
    if not output_url:
        if job_id:
            raise HiggsfieldJobRecoveryRequired(job_id=job_id, output_path=out)
        raise HiggsfieldCLIError(f"`generate create {model}` produced no output URL")
    try:
        _download(output_url, out, timeout=min(wait_timeout, 300))
    except Exception as exc:
        raise HiggsfieldJobRecoveryRequired(
            job_id=job_id,
            output_path=out,
        ) from exc
    return {"job_id": job_id, "output_path": str(out)}


# --------------------------------------------------------------------------- #
# USD conversion helper
# --------------------------------------------------------------------------- #

def credit_usd_rate() -> float:
    """Return a configured, positive USD-per-credit conversion rate."""
    raw = os.environ.get("HIGGSFIELD_CREDIT_USD")
    if not raw:
        raise HiggsfieldCLIError(
            "HIGGSFIELD_CREDIT_USD is required for a sourced USD estimate"
        )
    try:
        rate = float(raw)
    except ValueError as exc:
        raise HiggsfieldCLIError("HIGGSFIELD_CREDIT_USD is invalid") from exc
    if not math.isfinite(rate) or rate <= 0:
        raise HiggsfieldCLIError("HIGGSFIELD_CREDIT_USD is invalid")
    return rate


def credits_to_usd(credits: float) -> float:
    """Convert a credit count to USD using the configured rate."""
    return round(credits * credit_usd_rate(), 6)
