from __future__ import annotations

import hashlib
from pathlib import Path

from PIL import Image
import yaml

from tools.graphics.documentary_local_asset import DocumentaryLocalAsset
from tools.tool_registry import ToolRegistry


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_registry_discovers_documentary_local_asset() -> None:
    registry = ToolRegistry()
    registry.discover()

    tool = registry.get("documentary_local_asset")
    assert tool is not None
    assert tool.provider == "local"
    assert tool.capability == "documentary_local_asset"
    assert tool.estimate_cost({}) == 0.0
    assert tool.resource_profile.network_required is False


def test_generative_documentary_assets_stage_advertises_local_asset_tool() -> None:
    root = Path(__file__).resolve().parents[2]
    pipeline = yaml.safe_load(
        (root / "pipeline_defs/generative-documentary.yaml").read_text()
    )
    assets = next(stage for stage in pipeline["stages"] if stage["name"] == "assets")

    assert "documentary_local_asset" in assets["tools_available"]


def test_source_reframe_is_hash_bound_and_byte_deterministic(tmp_path: Path) -> None:
    project = tmp_path / "project"
    source = project / "assets/sources/source.png"
    source.parent.mkdir(parents=True)
    Image.new("RGB", (64, 48), (120, 90, 60)).save(source)
    source_hash = _sha256(source)
    inputs = {
        "project_dir": str(project),
        "operation": "source_reframe",
        "source_path": "assets/sources/source.png",
        "source_sha256": source_hash,
        "output_path": "assets/images/output-a.png",
        "recipe": {
            "width": 320,
            "height": 180,
            "zoom": 1.1,
            "center_x": 0.5,
            "center_y": 0.5,
            "color_saturation": 0.9,
        },
    }

    first = DocumentaryLocalAsset().execute(inputs)
    second = DocumentaryLocalAsset().execute(
        {**inputs, "output_path": "assets/images/output-b.png"}
    )

    assert first.success is second.success is True
    assert first.cost_usd == second.cost_usd == 0.0
    assert first.data["sha256"] == second.data["sha256"]
    assert (project / first.data["path"]).read_bytes() == (
        project / second.data["path"]
    ).read_bytes()


def test_local_motion_writes_canonical_instruction_artifact(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()

    result = DocumentaryLocalAsset().execute(
        {
            "project_dir": str(project),
            "operation": "local_motion",
            "output_path": "assets/motion/s01-02.json",
            "recipe": {
                "scene_id": "s01-02",
                "source_asset_id": "s01-02-primary",
                "instructions": "Slow six percent dolly-out across the flat source image.",
            },
        }
    )

    assert result.success is True
    assert result.data["media_type"] == "animation"
    assert _sha256(project / result.data["path"]) == result.data["sha256"]
    assert (project / result.data["path"]).read_text() == (
        '{"instructions":"Slow six percent dolly-out across the flat source image.",'
        '"scene_id":"s01-02","source_asset_id":"s01-02-primary"}\n'
    )


def test_dry_run_and_unsafe_paths_never_write(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    tool = DocumentaryLocalAsset()

    dry = tool.execute(
        {
            "project_dir": str(project),
            "operation": "network_diagram",
            "output_path": "assets/images/network.png",
            "recipe": {"width": 320, "height": 180, "seed": 30306},
            "dry_run": True,
        }
    )
    escaped = tool.execute(
        {
            "project_dir": str(project),
            "operation": "network_diagram",
            "output_path": "../escape.png",
            "recipe": {"width": 320, "height": 180, "seed": 30306},
        }
    )

    assert dry.success is True
    assert dry.data["would_write"] == "assets/images/network.png"
    assert not (project / "assets/images/network.png").exists()
    assert escaped.success is False
    assert not (tmp_path / "escape.png").exists()


def test_source_reframe_rejects_symlinked_source(tmp_path: Path) -> None:
    project = tmp_path / "project"
    outside = tmp_path / "outside.png"
    Image.new("RGB", (64, 48), (120, 90, 60)).save(outside)
    source = project / "assets/sources/source.png"
    source.parent.mkdir(parents=True)
    source.symlink_to(outside)

    result = DocumentaryLocalAsset().execute(
        {
            "project_dir": str(project),
            "operation": "source_reframe",
            "source_path": "assets/sources/source.png",
            "source_sha256": _sha256(outside),
            "output_path": "assets/images/output.png",
            "recipe": {"width": 320, "height": 180},
        }
    )

    assert result.success is False
    assert "safe regular file" in result.error
    assert not (project / "assets/images/output.png").exists()
