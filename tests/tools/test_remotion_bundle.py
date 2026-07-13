from __future__ import annotations

import hashlib
import json
from pathlib import Path

import jsonschema
import pytest

from schemas.artifacts import canonical_hash, validate_artifact
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

    inputs = {
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
    component_hash = f"sha256:{'c' * 64}"
    editorial_package = {
        "version": "1.0",
        "package_id": "history-sleep--ep-001-editorial",
        "package_version": 1,
        "content_hash": "",
        "episode_thesis": "Quiet institutions reveal how public life changed.",
        "fact_checked_script": {
            "artifact_name": "script",
            "artifact_version": "1.0",
            "content_hash": component_hash,
        },
        "claim_ledger": {
            "artifact_name": "claim_ledger",
            "artifact_version": "1.0",
            "content_hash": component_hash,
            "ledger_version": 1,
        },
        "shotlist": {
            "artifact_name": "scene_plan",
            "artifact_version": "1.0",
            "content_hash": canonical_hash(inputs["scene_plan"]),
        },
        "provider_plan": {
            "artifact_name": "proposal_packet",
            "artifact_version": "1.0",
            "content_hash": canonical_hash(inputs["proposal_packet"]),
        },
        "expected_cost": {"currency": "USD", "amount": 0},
    }
    editorial_scope = dict(editorial_package)
    editorial_scope.pop("content_hash")
    editorial_package["content_hash"] = canonical_hash(editorial_scope)
    inputs["editorial_package"] = editorial_package
    inputs["asset_manifest"]["approval_scope"] = {
        "package_id": editorial_package["package_id"],
        "package_version": editorial_package["package_version"],
        "content_hash": editorial_package["content_hash"],
    }
    return inputs


def _rebind_editorial(inputs: dict[str, object]) -> None:
    package = inputs["editorial_package"]
    package["shotlist"]["content_hash"] = canonical_hash(inputs["scene_plan"])
    package["provider_plan"]["content_hash"] = canonical_hash(
        inputs["proposal_packet"]
    )
    scope = dict(package)
    scope.pop("content_hash")
    package["content_hash"] = canonical_hash(scope)
    inputs["asset_manifest"]["approval_scope"] = {
        "package_id": package["package_id"],
        "package_version": package["package_version"],
        "content_hash": package["content_hash"],
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
    assert bundle["approval_scope"]["content_hash"] == inputs["editorial_package"][
        "content_hash"
    ]
    assert {item["path"] for item in bundle["source_files"]} == {
        "Root.tsx",
        "assets/hero.png",
        "index.tsx",
        "project.json",
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
    assert Path(first.data["versioned_path"]).read_bytes() == first_bytes
    assert (
        Path(first.data["snapshot_dir"]) / "project" / "Root.tsx"
    ).read_text(encoding="utf-8") == "export const Root = () => null;\n"
    assert (
        Path(first.data["snapshot_dir"]) / "project" / "artifacts" / "remotion-props.json"
    ).is_file()
    validate_artifact("remotion_bundle", bundle)


def test_bundle_validation_rejects_content_tampering(tmp_path: Path) -> None:
    project = tmp_path / "history-sleep--ep-001"
    result = RemotionBundle().execute(_inputs(project))
    assert result.success, result.error

    tampered = json.loads(json.dumps(result.data["bundle"]))
    tampered["render_spec"]["output_path"] = "renders/unapproved.mp4"

    with pytest.raises(jsonschema.ValidationError, match="content_hash"):
        validate_artifact("remotion_bundle", tampered)


def test_bundle_fails_before_writing_when_schema_validation_fails(
    tmp_path: Path,
) -> None:
    project = tmp_path / "history-sleep--ep-001"
    inputs = _inputs(project)
    inputs["edit_decisions"]["bespoke"]["art_direction"] = 42

    result = RemotionBundle().execute(inputs)

    assert not result.success
    assert not (project / "artifacts" / "remotion_bundle.json").exists()


def test_atelier_render_rejects_an_output_not_locked_by_the_bundle(
    tmp_path: Path,
) -> None:
    project = tmp_path / "history-sleep--ep-001"
    inputs = _inputs(project)
    result = RemotionBundle().execute(inputs)
    assert result.success, result.error
    inputs["remotion_bundle"] = result.data["bundle"]
    inputs["output_path"] = str(project / "renders" / "unapproved.mp4")

    with pytest.raises(ValueError, match="output_path does not match"):
        VideoCompose._lock_atelier_render_inputs(
            inputs,
            inputs["edit_decisions"],
        )


def test_atelier_render_rejects_source_drift_after_bundle_creation(
    tmp_path: Path,
) -> None:
    project = tmp_path / "history-sleep--ep-001"
    inputs = _inputs(project)
    result = RemotionBundle().execute(inputs)
    assert result.success, result.error
    inputs["remotion_bundle"] = result.data["bundle"]
    (project / "Root.tsx").write_text(
        "export const Root = () => <div>changed</div>;\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="bundle file no longer matches"):
        VideoCompose._lock_atelier_render_inputs(
            inputs,
            inputs["edit_decisions"],
        )


def test_atelier_render_rejects_a_new_file_after_bundle_creation(
    tmp_path: Path,
) -> None:
    project = tmp_path / "history-sleep--ep-001"
    inputs = _inputs(project)
    result = RemotionBundle().execute(inputs)
    assert result.success, result.error
    inputs["remotion_bundle"] = result.data["bundle"]
    (project / "new-import.json").write_text('{"late":true}\n', encoding="utf-8")

    with pytest.raises(ValueError, match="live project inventory"):
        VideoCompose._lock_atelier_render_inputs(
            inputs,
            inputs["edit_decisions"],
        )


def test_bundle_rejects_scene_plan_outside_the_editorial_approval_scope(
    tmp_path: Path,
) -> None:
    project = tmp_path / "history-sleep--ep-001"
    inputs = _inputs(project)
    inputs["scene_plan"]["scenes"].append(
        {"id": "scene-002", "duration_seconds": 4}
    )

    result = RemotionBundle().execute(inputs)

    assert not result.success
    assert "shotlist does not bind the supplied artifact" in result.error


def test_atelier_qa_ladder_typechecks_validates_stills_and_proxies(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "history-sleep--ep-001"
    inputs = _inputs(project)
    bundle_result = RemotionBundle().execute(inputs)
    assert bundle_result.success, bundle_result.error
    inputs["remotion_bundle"] = bundle_result.data["bundle"]
    inputs["output_path"] = str(project / "renders" / "final.mp4")
    locked_inputs, locked_edit = VideoCompose._lock_atelier_render_inputs(
        inputs,
        inputs["edit_decisions"],
    )
    commands: list[list[str]] = []

    def fake_run_command(
        self: VideoCompose,
        cmd: list[str],
        *,
        timeout: int | None = None,
        cwd: Path | None = None,
    ) -> None:
        commands.append(cmd)
        if len(cmd) > 5 and cmd[2] in {"still", "render"}:
            output = Path(cmd[5])
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(b"qa artifact")

    monkeypatch.setattr(VideoCompose, "run_command", fake_run_command)

    qa = VideoCompose()._run_atelier_qa_ladder(
        inputs=locked_inputs,
        edit_decisions=locked_edit,
        effective_entry=project / "index.tsx",
        composer_dir=project,
    )

    assert qa["status"] == "pass"
    assert [command[1:3] for command in commands] == [
        ["tsc", "--noEmit"],
        ["remotion", "compositions"],
        ["remotion", "still"],
        ["remotion", "render"],
    ]


def test_bundle_receipts_stage_non_typescript_imports(tmp_path: Path) -> None:
    project = tmp_path / "history-sleep--ep-001"
    source_dir = project / "source"
    source_dir.mkdir(parents=True)
    entry = source_dir / "index.tsx"
    entry.write_text("import data from './data.json';\n", encoding="utf-8")
    data = source_dir / "data.json"
    data.write_text('{"title":"History"}\n', encoding="utf-8")
    composer = tmp_path / "composer"

    effective_entry = VideoCompose()._stage_atelier_project(
        entry,
        composer,
        project_dir=project,
        source_receipts=[
            {"path": "source/index.tsx", "sha256": _sha256(entry.read_bytes())},
            {"path": "source/data.json", "sha256": _sha256(data.read_bytes())},
        ],
    )

    assert effective_entry.read_text(encoding="utf-8").startswith("import data")
    assert (effective_entry.parent / "data.json").read_text(encoding="utf-8") == (
        '{"title":"History"}\n'
    )


def test_bundle_staging_replaces_newer_stale_bytes(tmp_path: Path) -> None:
    project = tmp_path / "history-sleep--ep-001"
    project.mkdir()
    entry = project / "index.tsx"
    entry.write_text("export const current = true;\n", encoding="utf-8")
    composer = tmp_path / "composer"
    stale = composer / "projects" / project.name / "index.tsx"
    stale.parent.mkdir(parents=True)
    stale.write_text("export const stale = true;\n", encoding="utf-8")
    stale.touch()

    effective_entry = VideoCompose()._stage_atelier_project(
        entry,
        composer,
        project_dir=project,
        source_receipts=[
            {"path": "index.tsx", "sha256": _sha256(entry.read_bytes())},
        ],
    )

    assert effective_entry.read_bytes() == entry.read_bytes()


def test_generative_documentary_render_requires_bundle_before_compose(
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
    bundle_result = RemotionBundle().execute(inputs)
    assert bundle_result.success, bundle_result.error
    inputs.update(
        {
            "remotion_bundle": bundle_result.data["bundle"],
            "remotion_bundle_path": bundle_result.data["path"],
            "remotion_bundle_versioned_path": bundle_result.data["versioned_path"],
            "remotion_bundle_snapshot_dir": bundle_result.data["snapshot_dir"],
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
        "versioned_path": captured["remotion_bundle_versioned_path"],
        "snapshot_dir": captured["remotion_bundle_snapshot_dir"],
        "content_hash": captured["remotion_bundle"]["content_hash"],
    }


def test_generative_documentary_render_fails_without_prebuilt_bundle(
    tmp_path: Path,
) -> None:
    project = tmp_path / "history-sleep--ep-001"
    inputs = _inputs(project)
    inputs.update(
        {
            "operation": "render",
            "output_path": str(project / "renders" / "final.mp4"),
        }
    )

    result = VideoCompose().execute(inputs)

    assert not result.success
    assert "prebuilt remotion_bundle is required" in result.error


def test_bundle_rejects_an_output_path_outside_the_project(tmp_path: Path) -> None:
    project = tmp_path / "history-sleep--ep-001"
    inputs = _inputs(project)
    inputs["proposal_packet"]["production_plan"]["render_output_path"] = "../final.mp4"
    _rebind_editorial(inputs)

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


def test_bundle_rejects_an_asset_manifest_bound_to_another_editorial_package(
    tmp_path: Path,
) -> None:
    project = tmp_path / "history-sleep--ep-001"
    inputs = _inputs(project)
    inputs["asset_manifest"]["approval_scope"]["content_hash"] = f"sha256:{'b' * 64}"

    result = RemotionBundle().execute(inputs)

    assert not result.success
    assert "approval_scope does not match editorial_package" in result.error


def test_bundle_rejects_public_media_symlinked_from_outside_the_run(
    tmp_path: Path,
) -> None:
    project = tmp_path / "history-sleep--ep-001"
    inputs = _inputs(project)
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"approved historical anchor")
    (project / "public" / "linked.png").symlink_to(outside)

    result = RemotionBundle().execute(inputs)

    assert not result.success
    assert "public asset must stay inside project_dir" in result.error


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
    bundle_result = RemotionBundle().execute(inputs)
    assert bundle_result.success, bundle_result.error
    inputs.update(
        {
            "remotion_bundle": bundle_result.data["bundle"],
            "remotion_bundle_path": bundle_result.data["path"],
            "remotion_bundle_versioned_path": bundle_result.data["versioned_path"],
            "remotion_bundle_snapshot_dir": bundle_result.data["snapshot_dir"],
        }
    )
    (project / "assets" / "hero.png").write_bytes(b"changed after approval")

    result = VideoCompose().execute(inputs)

    assert not result.success
    assert "Remotion bundle drifted before render" in result.error
    assert "bundle file no longer matches remotion_bundle: assets/hero.png" in result.error
