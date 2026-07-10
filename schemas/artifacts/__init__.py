"""Artifact schema loading and validation utilities."""

from __future__ import annotations

import json
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
    jsonschema.validate(instance=data, schema=schema)
    if name == "claim_ledger":
        _validate_claim_ledger_semantics(data)


def list_schemas() -> list[str]:
    """List all available artifact schema names."""
    return [p.stem.replace(".schema", "") for p in SCHEMA_DIR.glob("*.schema.json")]
