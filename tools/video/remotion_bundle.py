"""Build a reproducible, provenance-bound Remotion atelier bundle."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from tools.base_tool import (
    BaseTool,
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


def _canonical_hash(value: Any) -> str:
    payload = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


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

    dependencies: list[str] = []
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
        "asset_manifest",
        "edit_decisions",
    ]

    input_schema = {
        "type": "object",
        "required": [
            "project_dir",
            "proposal_packet",
            "scene_plan",
            "asset_manifest",
            "edit_decisions",
        ],
        "properties": {
            "project_dir": {"type": "string"},
            "proposal_packet": {"type": "object"},
            "scene_plan": {"type": "object"},
            "asset_manifest": {"type": "object"},
            "edit_decisions": {"type": "object"},
            "output_path": {"type": "string"},
        },
    }

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        try:
            bundle, output_path = self._build(inputs)
            _write_json(output_path, bundle)
        except (KeyError, OSError, TypeError, ValueError) as exc:
            return ToolResult(success=False, error=str(exc))
        return ToolResult(
            success=True,
            data={"bundle": bundle, "path": str(output_path)},
            artifacts=[str(output_path)],
        )

    def _build(self, inputs: dict[str, Any]) -> tuple[dict[str, Any], Path]:
        project_dir = Path(inputs["project_dir"]).resolve()
        if not project_dir.is_dir():
            raise ValueError(f"project_dir not found: {project_dir}")

        proposal = inputs["proposal_packet"]
        scene_plan = inputs["scene_plan"]
        asset_manifest = inputs["asset_manifest"]
        edit = inputs["edit_decisions"]
        plan = proposal.get("production_plan") or {}
        if plan.get("pipeline") != "generative-documentary":
            raise ValueError("proposal_packet must select generative-documentary")
        if plan.get("render_runtime") != "remotion" or edit.get("render_runtime") != "remotion":
            raise ValueError("proposal_packet and edit_decisions must lock render_runtime=remotion")
        if plan.get("composition_mode") != "atelier" or edit.get("composition_mode") != "atelier":
            raise ValueError("proposal_packet and edit_decisions must lock composition_mode=atelier")

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

        asset_ids_by_hash: dict[str, list[str]] = {}
        for asset in asset_manifest.get("assets", []):
            asset_path, _ = _inside(project_dir, asset["path"], f"asset {asset.get('id')} path")
            if not asset_path.is_file():
                raise ValueError(f"asset file not found: {asset_path}")
            actual = _sha256(asset_path)
            expected = asset.get("sha256")
            if actual != expected:
                raise ValueError(f"asset hash mismatch for {asset.get('id')}: expected {expected}, got {actual}")
            asset_ids_by_hash.setdefault(actual, []).append(asset["id"])

        public_assets: list[dict[str, str]] = []
        for path in sorted(item for item in public_dir.rglob("*") if item.is_file()):
            digest = _sha256(path)
            asset_ids = sorted(asset_ids_by_hash.get(digest, []))
            if not asset_ids:
                raise ValueError(
                    f"public asset is not provenance-bound in asset_manifest: "
                    f"{path.relative_to(project_dir).as_posix()}"
                )
            public_assets.append(
                {
                    "path": path.relative_to(project_dir).as_posix(),
                    "sha256": digest,
                    "asset_id": asset_ids[0],
                }
            )

        source_extensions = {".tsx", ".ts", ".jsx", ".js", ".css"}
        source_files = [
            {
                "path": path.relative_to(project_dir).as_posix(),
                "sha256": _sha256(path),
            }
            for path in sorted(entry.parent.rglob("*"))
            if path.is_file()
            and path.suffix.lower() in source_extensions
            and project_dir in path.parents
        ]
        if not source_files:
            raise ValueError("atelier source tree contains no TypeScript, JavaScript, or CSS files")

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

        _, render_output_relative = _inside(
            project_dir,
            plan["render_output_path"],
            "render_spec.output_path",
        )

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
                "proposal_packet": _canonical_hash(proposal),
                "scene_plan": _canonical_hash(scene_plan),
                "asset_manifest": _canonical_hash(asset_manifest),
                "edit_decisions": _canonical_hash(edit),
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
        bundle["content_hash"] = _canonical_hash(bundle)

        output_raw = inputs.get("output_path", "artifacts/remotion_bundle.json")
        output_path, _ = _inside(project_dir, output_raw, "output_path")
        return bundle, output_path
