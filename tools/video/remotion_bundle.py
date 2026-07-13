"""Build a reproducible, provenance-bound Remotion atelier bundle."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path
from typing import Any

import jsonschema

from schemas.artifacts import canonical_hash, validate_artifact
from tools.base_tool import (
    BaseTool,
    DependencyError,
    Determinism,
    ExecutionMode,
    ResourceProfile,
    ToolResult,
    ToolStability,
    ToolTier,
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _inside(project_dir: Path, raw_path: str, field: str) -> tuple[Path, str]:
    path = Path(raw_path)
    resolved = (path if path.is_absolute() else project_dir / path).resolve()
    try:
        relative = resolved.relative_to(project_dir)
    except ValueError as exc:
        raise ValueError(f"{field} must stay inside project_dir: {raw_path}") from exc
    return resolved, relative.as_posix()


def _write_json(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (
        json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    ).encode("utf-8")
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


def _snapshot_bundle_files(
    *,
    project_dir: Path,
    bundle: dict[str, Any],
    versioned_path: Path,
) -> Path:
    """Archive exact project and runtime bytes under the bundle content hash."""
    snapshot_dir = versioned_path.with_suffix("")
    receipts = [*bundle["source_files"], *bundle["public_assets"]]
    props_receipt = {
        "path": bundle["render_spec"]["props_path"],
        "sha256": bundle["input_hashes"]["props"].removeprefix("sha256:"),
    }
    receipts.append(props_receipt)
    composer_dir = Path(__file__).resolve().parents[2] / "remotion-composer"
    runtime_receipts = {
        "package.json": bundle["runtime_lock"]["package_json_sha256"],
        "package-lock.json": bundle["runtime_lock"]["package_lock_sha256"],
    }

    if snapshot_dir.exists():
        for receipt in receipts:
            archived = snapshot_dir / "project" / receipt["path"]
            if not archived.is_file() or _sha256(archived) != receipt["sha256"]:
                raise ValueError(f"immutable bundle snapshot is corrupt: {archived}")
        for name, digest in runtime_receipts.items():
            archived = snapshot_dir / "runtime" / name
            if not archived.is_file() or _sha256(archived) != digest:
                raise ValueError(f"immutable runtime snapshot is corrupt: {archived}")
        return snapshot_dir

    temporary = snapshot_dir.with_name(snapshot_dir.name + ".tmp")
    if temporary.exists():
        shutil.rmtree(temporary)
    try:
        for receipt in receipts:
            source, relative = _inside(
                project_dir,
                receipt["path"],
                "bundle snapshot source",
            )
            destination = temporary / "project" / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
        for name in runtime_receipts:
            destination = temporary / "runtime" / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(composer_dir / name, destination)
        temporary.replace(snapshot_dir)
    except Exception:
        if temporary.exists():
            shutil.rmtree(temporary)
        raise
    return snapshot_dir


def verify_remotion_bundle(
    bundle: dict[str, Any],
    project_dir: Path,
    inputs: dict[str, Any],
) -> None:
    """Fail if a validated bundle no longer describes the live render inputs."""
    validate_artifact("remotion_bundle", bundle)

    for name in (
        "proposal_packet",
        "scene_plan",
        "editorial_package",
        "asset_manifest",
        "edit_decisions",
    ):
        if canonical_hash(inputs.get(name)) != bundle["input_hashes"][name]:
            raise ValueError(f"{name} no longer matches remotion_bundle")

    props, _ = _inside(
        project_dir,
        bundle["render_spec"]["props_path"],
        "render_spec.props_path",
    )
    if f"sha256:{_sha256(props)}" != bundle["input_hashes"]["props"]:
        raise ValueError("props no longer match remotion_bundle")

    for receipt in (*bundle["source_files"], *bundle["public_assets"]):
        path, _ = _inside(project_dir, receipt["path"], "bundle file receipt")
        if not path.is_file() or _sha256(path) != receipt["sha256"]:
            raise ValueError(
                f"bundle file no longer matches remotion_bundle: {receipt['path']}"
            )

    composer_dir = Path(__file__).resolve().parents[2] / "remotion-composer"
    runtime_files = {
        "package_json_sha256": composer_dir / "package.json",
        "package_lock_sha256": composer_dir / "package-lock.json",
    }
    for field, path in runtime_files.items():
        if _sha256(path) != bundle["runtime_lock"][field]:
            raise ValueError(f"Remotion runtime lock drifted: {path.name}")

    rebuilt, _ = RemotionBundle()._build(inputs)
    validate_artifact("remotion_bundle", rebuilt)
    if rebuilt["content_hash"] != bundle["content_hash"]:
        raise ValueError("live project inventory no longer matches remotion_bundle")


class RemotionBundle(BaseTool):
    """Freeze the exact inputs to a generative-documentary Remotion render."""

    name = "remotion_bundle"
    version = "0.1.0"
    tier = ToolTier.CORE
    capability = "video_post"
    provider = "remotion"
    stability = ToolStability.EXPERIMENTAL
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.DETERMINISTIC

    dependencies = ["cmd:npx"]
    agent_skills = ["remotion-best-practices"]
    capabilities = ["build_versioned_remotion_bundle"]
    best_for = [
        "Binding an approved generative-documentary edit to an atelier Remotion render",
        "Reproducible local rendering and bake-off evidence",
    ]
    not_good_for = ["Authoring creative scene code", "Rendering video"]
    resource_profile = ResourceProfile(cpu_cores=1, ram_mb=256, disk_mb=10)
    side_effects = ["writes artifacts/remotion_bundle.json inside project_dir"]
    idempotency_key_fields = [
        "project_dir",
        "proposal_packet",
        "scene_plan",
        "editorial_package",
        "asset_manifest",
        "edit_decisions",
    ]

    input_schema = {
        "type": "object",
        "required": [
            "project_dir",
            "proposal_packet",
            "scene_plan",
            "editorial_package",
            "asset_manifest",
            "edit_decisions",
        ],
        "properties": {
            "project_dir": {"type": "string"},
            "proposal_packet": {"type": "object"},
            "scene_plan": {"type": "object"},
            "editorial_package": {"type": "object"},
            "asset_manifest": {"type": "object"},
            "edit_decisions": {"type": "object"},
            "bundle_output_path": {"type": "string"},
        },
    }

    def check_dependencies(self) -> None:
        super().check_dependencies()
        composer_dir = Path(__file__).resolve().parents[2] / "remotion-composer"
        missing = [
            path.name
            for path in (composer_dir / "package.json", composer_dir / "package-lock.json")
            if not path.is_file()
        ]
        if missing:
            raise DependencyError(
                "Remotion composer runtime lock is incomplete: " + ", ".join(missing)
            )

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        try:
            bundle, output_path = self._build(inputs)
            validate_artifact("remotion_bundle", bundle)
            versioned_path = (
                output_path.parent
                / "remotion-bundles"
                / f"{bundle['content_hash'].removeprefix('sha256:')}.json"
            )
            _write_json(versioned_path, bundle)
            snapshot_dir = _snapshot_bundle_files(
                project_dir=Path(inputs["project_dir"]).resolve(),
                bundle=bundle,
                versioned_path=versioned_path,
            )
            _write_json(output_path, bundle)
        except (jsonschema.ValidationError, KeyError, OSError, TypeError, ValueError) as exc:
            return ToolResult(success=False, error=str(exc))
        return ToolResult(
            success=True,
            data={
                "bundle": bundle,
                "path": str(output_path),
                "versioned_path": str(versioned_path),
                "snapshot_dir": str(snapshot_dir),
            },
            artifacts=[str(output_path), str(versioned_path), str(snapshot_dir)],
        )

    def _build(self, inputs: dict[str, Any]) -> tuple[dict[str, Any], Path]:
        project_dir = Path(inputs["project_dir"]).resolve()
        if not project_dir.is_dir():
            raise ValueError(f"project_dir not found: {project_dir}")

        proposal = inputs["proposal_packet"]
        scene_plan = inputs["scene_plan"]
        editorial_package = inputs["editorial_package"]
        asset_manifest = inputs["asset_manifest"]
        edit = inputs["edit_decisions"]
        plan = proposal.get("production_plan") or {}
        if plan.get("pipeline") != "generative-documentary":
            raise ValueError("proposal_packet must select generative-documentary")
        if plan.get("render_runtime") != "remotion" or edit.get("render_runtime") != "remotion":
            raise ValueError("proposal_packet and edit_decisions must lock render_runtime=remotion")
        if plan.get("composition_mode") != "atelier" or edit.get("composition_mode") != "atelier":
            raise ValueError(
                "proposal_packet and edit_decisions must lock composition_mode=atelier"
            )

        validate_artifact("editorial_package", editorial_package)
        expected_component_hashes = {
            "shotlist": canonical_hash(scene_plan),
            "provider_plan": canonical_hash(proposal),
        }
        for component, expected_hash in expected_component_hashes.items():
            if editorial_package[component]["content_hash"] != expected_hash:
                raise ValueError(
                    f"editorial_package {component} does not bind the supplied artifact"
                )

        expected_approval_scope = {
            "package_id": editorial_package.get("package_id"),
            "package_version": editorial_package.get("package_version"),
            "content_hash": editorial_package.get("content_hash"),
        }
        if asset_manifest.get("approval_scope") != expected_approval_scope:
            raise ValueError(
                "asset_manifest approval_scope does not match editorial_package"
            )

        bespoke = edit.get("bespoke") or {}
        entry, entry_relative = _inside(project_dir, bespoke["entry"], "bespoke.entry")
        props, props_relative = _inside(project_dir, bespoke["props_path"], "bespoke.props_path")
        public_dir, public_relative = _inside(
            project_dir, bespoke["public_dir"], "bespoke.public_dir"
        )
        for path, label in ((entry, "entry"), (props, "props_path")):
            if not path.is_file():
                raise ValueError(f"atelier {label} not found: {path}")
        if not public_dir.is_dir():
            raise ValueError(f"atelier public_dir not found: {public_dir}")

        project_record = json.loads((project_dir / "project.json").read_text(encoding="utf-8"))
        project_id = project_record.get("id") or project_record.get("project_id")
        if not project_id:
            raise ValueError("project.json must contain id or project_id")

        output_raw = inputs.get(
            "bundle_output_path",
            "artifacts/remotion_bundle.json",
        )
        output_path, _ = _inside(project_dir, output_raw, "bundle_output_path")
        versioned_dir = output_path.parent / "remotion-bundles"
        render_output, render_output_relative = _inside(
            project_dir,
            plan["render_output_path"],
            "render_spec.output_path",
        )

        asset_ids_by_hash: dict[str, list[str]] = {}
        for asset in asset_manifest.get("assets", []):
            asset_path, _ = _inside(project_dir, asset["path"], f"asset {asset.get('id')} path")
            if not asset_path.is_file():
                raise ValueError(f"asset file not found: {asset_path}")
            actual = _sha256(asset_path)
            expected = asset.get("sha256")
            if actual != expected:
                raise ValueError(
                    f"asset hash mismatch for {asset.get('id')}: "
                    f"expected {expected}, got {actual}"
                )
            asset_ids_by_hash.setdefault(actual, []).append(asset["id"])

        public_assets: list[dict[str, str]] = []
        for path in sorted(item for item in public_dir.rglob("*") if item.is_file()):
            public_path, asset_relative = _inside(
                project_dir,
                str(path),
                "public asset",
            )
            digest = _sha256(public_path)
            asset_ids = sorted(asset_ids_by_hash.get(digest, []))
            if not asset_ids:
                raise ValueError(
                    f"public asset is not provenance-bound in asset_manifest: "
                    f"{asset_relative}"
                )
            if len(asset_ids) != 1:
                raise ValueError(
                    "public asset hash maps to multiple asset_manifest records; "
                    f"use unique bytes or an explicit manifest identity: {asset_relative}"
                )
            public_assets.append(
                {
                    "path": asset_relative,
                    "sha256": digest,
                    "asset_id": asset_ids[0],
                }
            )

        source_files: list[dict[str, str]] = []
        for path in sorted(entry.parent.rglob("*")):
            if not path.is_file():
                continue
            source_path, source_relative = _inside(
                project_dir,
                str(path),
                "atelier source",
            )
            if (
                source_path in {props, output_path, render_output}
                or public_dir == source_path
                or public_dir in source_path.parents
                or versioned_dir == source_path
                or versioned_dir in source_path.parents
                or "snapshots" in source_path.relative_to(project_dir).parts
                or "remotion-qa" in source_path.relative_to(project_dir).parts
            ):
                continue
            source_files.append(
                {
                    "path": source_relative,
                    "sha256": _sha256(source_path),
                }
            )
        if not source_files:
            raise ValueError("atelier source tree contains no project-local files")

        composer_dir = Path(__file__).resolve().parents[2] / "remotion-composer"
        package_json = composer_dir / "package.json"
        package_lock = composer_dir / "package-lock.json"
        lock_data = json.loads(package_lock.read_text(encoding="utf-8"))
        remotion_version = (
            (lock_data.get("packages") or {}).get("node_modules/remotion", {}).get("version")
            or (lock_data.get("dependencies") or {}).get("remotion", {}).get("version")
        )
        if not remotion_version:
            raise ValueError("Remotion version not found in remotion-composer/package-lock.json")

        render_spec: dict[str, Any] = {
            "render_runtime": "remotion",
            "composition_mode": "atelier",
            "composition_id": bespoke["composition_id"],
            "entry": entry_relative,
            "props_path": props_relative,
            "public_dir": public_relative,
            "output_path": render_output_relative,
        }
        for option in ("scale", "crf", "concurrency"):
            if option in bespoke:
                render_spec[option] = bespoke[option]

        bundle: dict[str, Any] = {
            "version": "1.0",
            "project_id": project_id,
            "approval_scope": asset_manifest["approval_scope"],
            "input_hashes": {
                "proposal_packet": canonical_hash(proposal),
                "scene_plan": canonical_hash(scene_plan),
                "editorial_package": canonical_hash(editorial_package),
                "asset_manifest": canonical_hash(asset_manifest),
                "edit_decisions": canonical_hash(edit),
                "props": f"sha256:{_sha256(props)}",
            },
            "runtime_lock": {
                "remotion_version": remotion_version,
                "package_json_sha256": _sha256(package_json),
                "package_lock_sha256": _sha256(package_lock),
            },
            "art_direction": bespoke["art_direction"],
            "render_spec": render_spec,
            "source_files": source_files,
            "public_assets": public_assets,
        }
        bundle["content_hash"] = canonical_hash(bundle)

        return bundle, output_path
