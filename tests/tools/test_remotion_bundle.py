from __future__ import annotations

import hashlib
import json
from pathlib import Path

import jsonschema
import pytest

from schemas.artifacts import validate_artifact
from tools.base_tool import ToolResult
from tools.video.remotion_bundle import RemotionBundle
from tools.video.video_compose import VideoCompose


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _inputs(project: Path) -> dict[str, object]:
    (project / "artifacts").mkdir(parents=True)
    (project / "assets").mkdir()
    (project / "public").mkdir()

    asset_bytes = b"approved historical anchor"
    (project / "assets" / "hero.png").write_bytes(asset_bytes)
    (project / "public" / "hero.png").write_bytes(asset_bytes)
    (project / "index.tsx").write_text("export {Root} from './Root';\n", encoding="utf-8")
    (project / "Root.tsx").write_text("export const Root = () => null;\n", encoding="utf-8")
    (project / "artifacts" / "remotion-props.json").write_text(
        json.dumps({"scenes": [{"id": "scene-001", "asset": "hero.png"}]}),
        encoding="utf-8",
    )
    (project / "project.json").write_text(
        json.dumps({"id": "history-sleep--ep-001"}), encoding="utf-8"
    )

    return {
        "project_dir": str(project),
        "proposal_packet": {
            "production_plan": {
                "pipeline": "generative-documentary",
                "render_runtime": "remotion",
                "composition_mode": "atelier",
                "render_output_path": "renders/final.mp4",
            }
        },
        "scene_plan": {
            "version": "1.0",
            "scenes": [{"id": "scene-001", "duration_seconds": 12}],
        },
        "asset_manifest": {
            "version": "1.0",
            "profile": "provenance-aware-documentary-v1",
            "approval_scope": {
                "package_id": "history-sleep--ep-001-editorial",
                "package_version": 1,
                "content_hash": f"sha256:{'a' * 64}",
            },
            "assets": [
                {
                    "id": "hero",
                    "type": "image",
                    "path": "assets/hero.png",
                    "source_tool": "archive_acquisition",
                    "scene_id": "scene-001",
                    "sha256": _sha256(asset_bytes),
                }
            ],
        },
        "edit_decisions": {
            "version": "1.0",
            "render_runtime": "remotion",
            "composition_mode": "atelier",
            "bespoke": {
                "entry": str(project / "index.tsx"),
                "composition_id": "HistorySleepEpisode",
                "art_direction": "Quiet archival paper, restrained amber light, slow motion.",
                "props_path": str(project / "artifacts" / "remotion-props.json"),
                "public_dir": str(project / "public"),
                "crf": 18,
                "concurrency": 8,
            },
        },
    }


def test_builds_a_deterministic_hash_bound_remotion_bundle(tmp_path: Path) -> None:
    project = tmp_path / "history-sleep--ep-001"
    inputs = _inputs(project)

    first = RemotionBundle().execute(inputs)
    first_bytes = (project / "artifacts" / "remotion_bundle.json").read_bytes()
    second = RemotionBundle().execute(inputs)

    assert first.success, first.error
    assert second.success, second.error
    assert (project / "artifacts" / "remotion_bundle.json").read_bytes() == first_bytes

    bundle = first.data["bundle"]
    assert bundle["version"] == "1.0"
    assert bundle["project_id"] == "history-sleep--ep-001"
    assert bundle["render_spec"] == {
        "render_runtime": "remotion",
        "composition_mode": "atelier",
        "composition_id": "HistorySleepEpisode",
        "entry": "index.tsx",
        "props_path": "artifacts/remotion-props.json",
        "public_dir": "public",
        "output_path": "renders/final.mp4",
        "crf": 18,
        "concurrency": 8,
    }
    assert bundle["approval_scope"]["content_hash"] == f"sha256:{'a' * 64}"
    assert {item["path"] for item in bundle["source_files"]} == {
        "Root.tsx",
        "index.tsx",
    }
    assert bundle["public_assets"] == [
        {
            "path": "public/hero.png",
            "sha256": _sha256(b"approved historical anchor"),
            "asset_id": "hero",
        }
    ]
    assert bundle["content_hash"].startswith("sha256:")
    assert len(bundle["content_hash"]) == 71
    validate_artifact("remotion_bundle", bundle)


def test_bundle_validation_rejects_content_tampering(tmp_path: Path) -> None:
    project = tmp_path / "history-sleep--ep-001"
    result = RemotionBundle().execute(_inputs(project))
    assert result.success, result.error

    tampered = json.loads(json.dumps(result.data["bundle"]))
    tampered["render_spec"]["output_path"] = "renders/unapproved.mp4"

    with pytest.raises(jsonschema.ValidationError, match="content_hash"):
        validate_artifact("remotion_bundle", tampered)


def test_generative_documentary_render_builds_bundle_before_compose(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "history-sleep--ep-001"
    inputs = _inputs(project)
    inputs.update(
        {
            "operation": "render",
            "output_path": str(project / "renders" / "final.mp4"),
        }
    )
    captured: dict[str, object] = {}

    def fake_render(
        self: VideoCompose,
        render_inputs: dict[str, object],
        edit_decisions: dict[str, object],
    ) -> ToolResult:
        captured.update(render_inputs)
        return ToolResult(success=True, data={"rendered": True})

    monkeypatch.setattr(VideoCompose, "_render_via_atelier", fake_render)

    result = VideoCompose().execute(inputs)

    assert result.success, result.error
    assert captured["remotion_bundle"]["content_hash"].startswith("sha256:")
    assert captured["remotion_bundle_path"] == str(
        project / "artifacts" / "remotion_bundle.json"
    )
    assert result.data["remotion_bundle"] == {
        "path": str(project / "artifacts" / "remotion_bundle.json"),
        "content_hash": captured["remotion_bundle"]["content_hash"],
    }


def test_bundle_rejects_an_output_path_outside_the_project(tmp_path: Path) -> None:
    project = tmp_path / "history-sleep--ep-001"
    inputs = _inputs(project)
    inputs["proposal_packet"]["production_plan"]["render_output_path"] = "../final.mp4"

    result = RemotionBundle().execute(inputs)

    assert not result.success
    assert "render_spec.output_path" in result.error


def test_bundle_rejects_asset_hash_drift(tmp_path: Path) -> None:
    project = tmp_path / "history-sleep--ep-001"
    inputs = _inputs(project)
    (project / "assets" / "hero.png").write_bytes(b"changed after approval")

    result = RemotionBundle().execute(inputs)

    assert not result.success
    assert "asset hash mismatch for hero" in result.error


def test_bundle_rejects_unmanifested_public_media(tmp_path: Path) -> None:
    project = tmp_path / "history-sleep--ep-001"
    inputs = _inputs(project)
    (project / "public" / "untracked.png").write_bytes(b"no provenance")

    result = RemotionBundle().execute(inputs)

    assert not result.success
    assert "public asset is not provenance-bound" in result.error


def test_render_stops_before_compose_when_approved_asset_drifted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "history-sleep--ep-001"
    inputs = _inputs(project)
    inputs.update(
        {
            "operation": "render",
            "output_path": str(project / "renders" / "final.mp4"),
        }
    )
    (project / "assets" / "hero.png").write_bytes(b"changed after approval")
    render_called = False

    def fake_render(
        self: VideoCompose,
        render_inputs: dict[str, object],
        edit_decisions: dict[str, object],
    ) -> ToolResult:
        nonlocal render_called
        render_called = True
        return ToolResult(success=True)

    monkeypatch.setattr(VideoCompose, "_render_via_atelier", fake_render)

    result = VideoCompose().execute(inputs)

    assert not result.success
    assert not render_called
    assert "Remotion bundle validation failed before render" in result.error
    assert "asset hash mismatch for hero" in result.error
