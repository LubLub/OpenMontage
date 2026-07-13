"""Deterministic, source-bound local assets for provenance-aware documentaries."""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageEnhance

from tools.base_tool import (
    BaseTool,
    Determinism,
    ExecutionMode,
    ResourceProfile,
    ToolResult,
    ToolStability,
    ToolTier,
)


PAPER = (239, 233, 216)
SOOT = (34, 38, 42)
BLUE = (37, 99, 235)
AMBER = (245, 158, 11)
COOL_WHITE = (228, 238, 248)
OPERATIONS = {
    "source_reframe",
    "network_diagram",
    "dual_network_diagram",
    "local_motion",
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _project_path(
    project_dir: Path,
    relative: str,
    *,
    existing: bool,
) -> Path:
    candidate = Path(relative)
    if candidate.is_absolute() or not relative or ".." in candidate.parts:
        raise ValueError("asset path must be project-relative")
    if existing:
        unresolved = project_dir / candidate
        if unresolved.is_symlink():
            raise ValueError("asset source must be a safe regular file")
        resolved = unresolved.resolve(strict=True)
        if not resolved.is_file():
            raise ValueError("asset source must be a safe regular file")
    else:
        parent = (project_dir / candidate).parent
        parent.mkdir(parents=True, exist_ok=True)
        resolved_parent = parent.resolve(strict=True)
        resolved = resolved_parent / candidate.name
        if resolved.exists() and resolved.is_symlink():
            raise ValueError("asset output cannot replace a symlink")
    if not resolved.is_relative_to(project_dir):
        raise ValueError("asset path escapes the project")
    return resolved


def _positive_int(recipe: dict[str, Any], name: str) -> int:
    value = recipe.get(name)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"recipe {name} must be a positive integer")
    return value


def _unit_float(recipe: dict[str, Any], name: str, default: float) -> float:
    value = recipe.get(name, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"recipe {name} must be numeric")
    value = float(value)
    if not 0 <= value <= 1:
        raise ValueError(f"recipe {name} must be between zero and one")
    return value


def _source_reframe(source: Path, recipe: dict[str, Any]) -> Image.Image:
    width = _positive_int(recipe, "width")
    height = _positive_int(recipe, "height")
    center_x = _unit_float(recipe, "center_x", 0.5)
    center_y = _unit_float(recipe, "center_y", 0.5)
    zoom = recipe.get("zoom", 1.0)
    if (
        isinstance(zoom, bool)
        or not isinstance(zoom, (int, float))
        or not 1.0 <= float(zoom) <= 4.0
    ):
        raise ValueError("recipe zoom must be between 1 and 4")
    with Image.open(source) as opened:
        image = opened.convert("RGB")
    target_ratio = width / height
    source_ratio = image.width / image.height
    if source_ratio >= target_ratio:
        base_height = image.height
        base_width = base_height * target_ratio
    else:
        base_width = image.width
        base_height = base_width / target_ratio
    crop_width = base_width / float(zoom)
    crop_height = base_height / float(zoom)
    center_pixel_x = image.width * center_x
    center_pixel_y = image.height * center_y
    left = max(0.0, min(image.width - crop_width, center_pixel_x - crop_width / 2))
    top = max(0.0, min(image.height - crop_height, center_pixel_y - crop_height / 2))
    framed = image.crop(
        (
            round(left),
            round(top),
            round(left + crop_width),
            round(top + crop_height),
        )
    ).resize((width, height), Image.Resampling.LANCZOS)
    saturation = recipe.get("color_saturation", 1.0)
    if (
        isinstance(saturation, bool)
        or not isinstance(saturation, (int, float))
        or not 0 <= float(saturation) <= 2
    ):
        raise ValueError("recipe color_saturation must be between 0 and 2")
    return ImageEnhance.Color(framed).enhance(float(saturation))


def _diagram(recipe: dict[str, Any], *, dual: bool) -> Image.Image:
    width = _positive_int(recipe, "width")
    height = _positive_int(recipe, "height")
    if not isinstance(recipe.get("seed"), int):
        raise ValueError("diagram recipe requires an integer seed")
    image = Image.new("RGB", (width, height), PAPER)
    draw = ImageDraw.Draw(image, "RGBA")
    margin_x = max(20, round(width * 0.08))
    baseline = round(height * (0.72 if dual else 0.58))
    draw.rectangle(
        (margin_x // 2, round(height * 0.1), width - margin_x // 2, round(height * 0.9)),
        outline=(*SOOT, 80),
        width=max(1, round(width / 640)),
    )
    draw.line(
        (margin_x, baseline, width - margin_x, baseline),
        fill=(*BLUE, 225),
        width=max(4, round(width * 0.01)),
    )
    for fraction, lift in ((0.15, 0.22), (0.36, 0.38), (0.56, 0.18), (0.76, 0.4), (0.91, 0.24)):
        x = round(width * fraction)
        y = round(height * lift)
        draw.line((x, baseline, x, y), fill=(*BLUE, 220), width=max(3, round(width * 0.007)))
        radius = max(5, round(width * 0.013))
        draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=(*AMBER, 240))
    if dual:
        upper_y = round(height * 0.34)
        points = [round(width * fraction) for fraction in (0.18, 0.38, 0.58, 0.78)]
        draw.line((points[0], upper_y, points[-1], upper_y), fill=(*COOL_WHITE, 255), width=max(4, round(width * 0.009)))
        for x in points:
            radius = max(5, round(width * 0.011))
            draw.ellipse((x - radius, upper_y - radius, x + radius, upper_y + radius), fill=(*COOL_WHITE, 255))
    return image


def _write_png(path: Path, image: Image.Image) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        image.save(temporary, format="PNG", optimize=False, compress_level=9)
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _write_motion(path: Path, recipe: dict[str, Any]) -> None:
    scene_id = recipe.get("scene_id")
    source_asset_id = recipe.get("source_asset_id")
    instructions = recipe.get("instructions")
    if not isinstance(scene_id, str) or not scene_id.strip():
        raise ValueError("local motion requires scene_id")
    if not isinstance(source_asset_id, str) or not source_asset_id.strip():
        raise ValueError("local motion requires source_asset_id")
    if not isinstance(instructions, str) or not instructions.strip():
        raise ValueError("local motion requires instructions")
    document = {
        "scene_id": scene_id.strip(),
        "source_asset_id": source_asset_id.strip(),
        "instructions": instructions.strip(),
    }
    payload = json.dumps(
        document,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8") + b"\n"
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_bytes(payload)
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


class DocumentaryLocalAsset(BaseTool):
    """Render declarative local documentary assets without network or commands."""

    name = "documentary_local_asset"
    version = "1.0.0"
    tier = ToolTier.CORE
    capability = "documentary_local_asset"
    provider = "local"
    stability = ToolStability.EXPERIMENTAL
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.DETERMINISTIC
    dependencies = ["python:PIL"]
    capabilities = sorted(OPERATIONS)
    resource_profile = ResourceProfile(
        cpu_cores=1,
        ram_mb=512,
        vram_mb=0,
        disk_mb=100,
        network_required=False,
    )
    side_effects = ["writes one hash-verifiable project-local image or motion artifact"]
    idempotency_key_fields = [
        "operation",
        "source_path",
        "source_sha256",
        "output_path",
        "recipe",
    ]
    input_schema = {
        "type": "object",
        "required": ["project_dir", "operation", "output_path", "recipe"],
        "properties": {
            "project_dir": {"type": "string"},
            "operation": {"type": "string", "enum": sorted(OPERATIONS)},
            "source_path": {"type": "string"},
            "source_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
            "output_path": {"type": "string"},
            "recipe": {"type": "object"},
            "dry_run": {"type": "boolean", "default": False},
        },
    }

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        try:
            project_dir = Path(inputs["project_dir"]).resolve(strict=True)
            if not project_dir.is_dir():
                raise ValueError("project_dir must be a directory")
            operation = inputs["operation"]
            if operation not in OPERATIONS:
                raise ValueError("unknown documentary local-asset operation")
            output_relative = inputs["output_path"]
            output = _project_path(project_dir, output_relative, existing=False)
            recipe = inputs["recipe"]
            if not isinstance(recipe, dict):
                raise ValueError("recipe must be an object")
            source = None
            if operation == "source_reframe":
                source_relative = inputs.get("source_path")
                expected_hash = inputs.get("source_sha256")
                if not isinstance(source_relative, str) or not isinstance(expected_hash, str):
                    raise ValueError("source_reframe requires source path and hash")
                source = _project_path(project_dir, source_relative, existing=True)
                if _sha256(source) != expected_hash:
                    raise ValueError("documentary source hash changed")
            if inputs.get("dry_run") is True:
                return ToolResult(
                    success=True,
                    data={
                        "operation": operation,
                        "would_write": output_relative,
                        "provider": self.provider,
                        "model": self.version,
                    },
                    cost_usd=0.0,
                    model=self.version,
                )
            if operation == "source_reframe":
                assert source is not None
                _write_png(output, _source_reframe(source, recipe))
                media_type = "image"
            elif operation == "network_diagram":
                _write_png(output, _diagram(recipe, dual=False))
                media_type = "image"
            elif operation == "dual_network_diagram":
                _write_png(output, _diagram(recipe, dual=True))
                media_type = "image"
            else:
                _write_motion(output, recipe)
                media_type = "animation"
            return ToolResult(
                success=True,
                data={
                    "operation": operation,
                    "path": output_relative,
                    "sha256": _sha256(output),
                    "size_bytes": output.stat().st_size,
                    "media_type": media_type,
                    "provider": self.provider,
                    "model": self.version,
                },
                artifacts=[str(output)],
                cost_usd=0.0,
                model=self.version,
            )
        except (KeyError, OSError, TypeError, ValueError) as exc:
            return ToolResult(success=False, error=str(exc), cost_usd=0.0)
