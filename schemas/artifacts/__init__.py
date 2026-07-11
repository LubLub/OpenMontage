"""Artifact schema loading and validation utilities."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import jsonschema

SCHEMA_DIR = Path(__file__).parent

ARTIFACT_NAMES = [
    "research_brief",
    "claim_ledger",
    "proposal_packet",
    "brief",
    "script",
    "character_design",
    "rig_plan",
    "pose_library",
    "scene_plan",
    "action_timeline",
    "asset_manifest",
    "edit_decisions",
    "render_report",
    "publish_log",
    "release_package",
    "technical_conformance",
    "editorial_package",
    "review",
    "cost_log",
    "decision_log",
    "source_media_review",
    "final_review",
    "character_qa_report",
    "video_analysis_brief",
]


def _validate_claim_ledger_semantics(data: dict[str, Any]) -> None:
    claims = data.get("claims", [])
    claim_ids = [claim.get("claim_id") for claim in claims]
    if len(claim_ids) != len(set(claim_ids)):
        raise jsonschema.ValidationError("claim_id values must be unique")

    sources_by_id: dict[str, dict[str, Any]] = {}
    for claim in claims:
        sources = claim.get("sources", [])
        source_ids = [source.get("source_id") for source in sources]
        if len(source_ids) != len(set(source_ids)):
            raise jsonschema.ValidationError(
                f"source_id values must be unique within claim {claim.get('claim_id')!r}"
            )
        for source in sources:
            source_id = source["source_id"]
            existing = sources_by_id.get(source_id)
            if existing is not None and existing != source:
                raise jsonschema.ValidationError(
                    f"Source {source_id!r} has inconsistent metadata across claims"
                )
            sources_by_id[source_id] = source
            try:
                parsed = urlsplit(source["url"])
                valid_url = (
                    parsed.scheme in {"http", "https"}
                    and bool(parsed.netloc)
                    and bool(parsed.hostname)
                    and not any(character.isspace() for character in source["url"])
                )
            except ValueError:
                valid_url = False
            if not valid_url:
                raise jsonschema.ValidationError(
                    f"Claim source {source_id!r} URL must be an absolute http/https URL"
                )


def _validate_technical_conformance_semantics(data: dict[str, Any]) -> None:
    hash_scope = dict(data)
    stored_content_hash = hash_scope.pop("content_hash")
    encoded = json.dumps(
        hash_scope,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    expected_content_hash = "sha256:" + hashlib.sha256(encoded).hexdigest()
    if stored_content_hash != expected_content_hash:
        raise jsonschema.ValidationError(
            "technical_conformance.content_hash does not match its bound scope"
        )

    failed = sorted(
        name
        for name, check in data["checks"].items()
        if check["status"] == "fail"
    )
    expected_status = "fail" if failed else "pass"
    if data["status"] != expected_status:
        raise jsonschema.ValidationError(
            "technical_conformance.status must be derived from all required checks"
        )

    recorded_at = data["manual_rescue"]["recorded_at"]
    if recorded_at is not None:
        try:
            parsed = datetime.fromisoformat(recorded_at.replace("Z", "+00:00"))
        except (AttributeError, ValueError) as exc:
            raise jsonschema.ValidationError(
                "technical_conformance Manual Rescue timestamp is invalid"
            ) from exc
        if parsed.tzinfo is None:
            raise jsonschema.ValidationError(
                "technical_conformance Manual Rescue timestamp requires a timezone"
            )

    expected_reasons = [
        f"technical_conformance_failed:{name}" for name in failed
    ]
    if data["manual_rescue"]["used"]:
        expected_reasons.append("manual_rescue_used")
    if data["execution_mode"] == "deterministic_fixture":
        expected_reasons.append("deterministic_fixture")
    proof = data["production_proof"]
    expected_eligible = (
        expected_status == "pass"
        and not data["manual_rescue"]["used"]
        and data["execution_mode"] == "production"
    )
    if (
        proof["eligible"] != expected_eligible
        or proof["ineligibility_reasons"] != expected_reasons
    ):
        raise jsonschema.ValidationError(
            "technical_conformance.production_proof must be derived from "
            "check status, execution mode, and Manual Rescue"
        )


def _validate_release_package_semantics(data: dict[str, Any]) -> None:
    hash_scope = dict(data)
    stored_content_hash = hash_scope.pop("content_hash")
    encoded = json.dumps(
        hash_scope,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    expected_content_hash = "sha256:" + hashlib.sha256(encoded).hexdigest()
    if stored_content_hash != expected_content_hash:
        raise jsonschema.ValidationError(
            "release_package.content_hash does not match its bound scope"
        )
    files = {item["role"]: item for item in data["files"]}
    if (
        data["technical_conformance"]["render_sha256"] != data["video"]["sha256"]
        or files["video"]["path"] != data["video"]["path"]
        or files["video"]["sha256"] != data["video"]["sha256"]
        or files["thumbnail"]["path"] != data["thumbnail"]["path"]
        or files["thumbnail"]["sha256"] != data["thumbnail"]["sha256"]
    ):
        raise jsonschema.ValidationError(
            "release_package media descriptors must bind one conforming video and thumbnail"
        )

def load_schema(name: str) -> dict:
    """Load a JSON schema by artifact name."""
    path = SCHEMA_DIR / f"{name}.schema.json"
    if not path.exists():
        raise FileNotFoundError(f"Schema not found: {path}")
    with open(path) as f:
        return json.load(f)


def validate_artifact(name: str, data: dict[str, Any]) -> None:
    """Validate artifact data against its schema. Raises on failure."""
    schema = load_schema(name)
    jsonschema.validate(
        instance=data,
        schema=schema,
        format_checker=jsonschema.FormatChecker(),
    )
    if name == "claim_ledger":
        _validate_claim_ledger_semantics(data)
    elif name == "technical_conformance":
        _validate_technical_conformance_semantics(data)
    elif name == "release_package":
        _validate_release_package_semantics(data)


def list_schemas() -> list[str]:
    """List all available artifact schema names."""
    return [p.stem.replace(".schema", "") for p in SCHEMA_DIR.glob("*.schema.json")]
