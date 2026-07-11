"""Contract tests for Backlot Phase 0: gate enforcement, checkpoint history,
project markers, and tool-event instrumentation."""

import json
import os

import pytest

from lib.checkpoint import (
    CheckpointValidationError,
    HISTORY_DIRNAME,
    PROJECT_MARKER_FILENAME,
    init_project,
    read_checkpoint,
    supersede_checkpoint,
    write_checkpoint,
)
from lib.events import emit_event, infer_project_dir, read_events


def _minimal_script() -> dict:
    return {
        "version": "1.0",
        "title": "Test Script",
        "total_duration_seconds": 10,
        "sections": [
            {"id": "s1", "text": "Hello.", "start_seconds": 0, "end_seconds": 10}
        ],
    }


class TestGateEnforcement:
    """GI-4: gated stages cannot be completed without approval evidence."""

    def test_completed_without_approval_raises(self, tmp_path):
        with pytest.raises(CheckpointValidationError, match="GATE VIOLATION"):
            write_checkpoint(
                tmp_path, "proj", "script", "completed",
                artifacts={"script": _minimal_script()},
                pipeline_type="animated-explainer",
            )

    def test_awaiting_human_is_the_correct_gate_state(self, tmp_path):
        path = write_checkpoint(
            tmp_path, "proj", "script", "awaiting_human",
            artifacts={"script": _minimal_script()},
            pipeline_type="animated-explainer",
        )
        cp = json.loads(path.read_text())
        assert cp["status"] == "awaiting_human"
        # Manifest gating is reflected in the checkpoint even when the
        # caller didn't pass human_approval_required.
        assert cp["human_approval_required"] is True

    def test_completed_with_approval_passes(self, tmp_path):
        path = write_checkpoint(
            tmp_path, "proj", "script", "completed",
            artifacts={"script": _minimal_script()},
            pipeline_type="animated-explainer",
            human_approved=True,
        )
        assert path.exists()

    def test_assets_stage_now_gates(self, tmp_path):
        """The assets gate flip: every pipeline's assets stage requires approval."""
        manifest_assets = {"version": "1.0", "assets": [], "total_cost_usd": 0.0}
        with pytest.raises(CheckpointValidationError, match="GATE VIOLATION"):
            write_checkpoint(
                tmp_path, "proj", "assets", "completed",
                artifacts={"asset_manifest": manifest_assets},
                pipeline_type="cinematic",
            )

    def test_ungated_stage_unaffected(self, tmp_path):
        from tests.contracts.test_phase0_contracts import sample_artifact

        path = write_checkpoint(
            tmp_path, "proj", "research", "completed",
            artifacts={"research_brief": sample_artifact("research_brief")},
            pipeline_type="animated-explainer",
        )
        assert path.exists()


class TestCheckpointHistory:
    """Superseded checkpoints are archived, not destroyed."""

    def test_overwrite_archives_previous(self, tmp_path):
        write_checkpoint(
            tmp_path, "proj", "script", "awaiting_human",
            artifacts={"script": _minimal_script()},
            pipeline_type="animated-explainer",
        )
        write_checkpoint(
            tmp_path, "proj", "script", "completed",
            artifacts={"script": _minimal_script()},
            pipeline_type="animated-explainer",
            human_approved=True,
        )
        history = list((tmp_path / "proj" / HISTORY_DIRNAME).glob("checkpoint_script_*.json"))
        assert len(history) == 1
        archived = json.loads(history[0].read_text())
        assert archived["status"] == "awaiting_human"
        current = read_checkpoint(tmp_path, "proj", "script")
        assert current["status"] == "completed"

    def test_in_progress_refreshes_are_not_archived(self, tmp_path):
        for _ in range(3):
            write_checkpoint(
                tmp_path, "proj", "assets", "in_progress",
                artifacts={},
                pipeline_type="cinematic",
                metadata={"partial_progress": {"completed_scene_ids": ["sc1"]}},
            )
        history_dir = tmp_path / "proj" / HISTORY_DIRNAME
        assert not history_dir.exists() or not list(history_dir.iterdir())

    def test_checkpoint_reader_rejects_symlinks_and_hard_links(self, tmp_path):
        path = write_checkpoint(
            tmp_path,
            "proj",
            "script",
            "awaiting_human",
            artifacts={"script": _minimal_script()},
            pipeline_type="animated-explainer",
        )
        outside = tmp_path / "outside.json"
        outside.write_bytes(path.read_bytes())
        path.unlink()
        path.symlink_to(outside)
        with pytest.raises(CheckpointValidationError, match="unsafe"):
            read_checkpoint(tmp_path, "proj", "script")
        path.unlink()
        os.link(outside, path)
        with pytest.raises(CheckpointValidationError, match="single-link"):
            read_checkpoint(tmp_path, "proj", "script")

    def test_resume_fields_round_trip_on_checkpoint(self, tmp_path):
        path = write_checkpoint(
            tmp_path,
            "proj",
            "script",
            "in_progress",
            artifacts={},
            input_fingerprint=f"sha256:{'a' * 64}",
            input_scope={"snapshot": f"sha256:{'b' * 64}"},
            output_fingerprint=f"sha256:{'c' * 64}",
            attempt_id="attempt-script-1",
        )

        checkpoint = json.loads(path.read_text())
        assert checkpoint["input_fingerprint"] == f"sha256:{'a' * 64}"
        assert checkpoint["input_scope"]["snapshot"] == f"sha256:{'b' * 64}"
        assert checkpoint["output_fingerprint"] == f"sha256:{'c' * 64}"
        assert checkpoint["attempt_id"] == "attempt-script-1"

    def test_invalid_resume_fingerprint_is_rejected(self, tmp_path):
        with pytest.raises(CheckpointValidationError):
            write_checkpoint(
                tmp_path,
                "proj",
                "script",
                "in_progress",
                artifacts={},
                input_fingerprint="not-a-digest",
            )

    def test_supersede_archives_before_removing_current(self, tmp_path):
        write_checkpoint(
            tmp_path,
            "proj",
            "script",
            "in_progress",
            artifacts={},
            input_fingerprint=f"sha256:{'a' * 64}",
            attempt_id="attempt-script-1",
        )

        archived_path = supersede_checkpoint(
            tmp_path,
            "proj",
            "script",
            reason="upstream script input changed",
            replacement_input_fingerprint=f"sha256:{'b' * 64}",
        )

        assert not (tmp_path / "proj" / "checkpoint_script.json").exists()
        archived = json.loads(archived_path.read_text())
        assert archived["status"] == "superseded"
        assert archived["supersession"]["reason"] == "upstream script input changed"
        assert archived["supersession"]["previous_status"] == "in_progress"
        assert archived["supersession"]["pending_recalculation"] is False
        assert archived["supersession"]["replacement_input_fingerprint"] == (
            f"sha256:{'b' * 64}"
        )
        assert archived["input_fingerprint"] == f"sha256:{'a' * 64}"

    def test_supersede_can_truthfully_defer_replacement_fingerprint(self, tmp_path):
        write_checkpoint(
            tmp_path,
            "proj",
            "script",
            "in_progress",
            artifacts={},
        )

        archived_path = supersede_checkpoint(
            tmp_path,
            "proj",
            "script",
            reason="upstream output is pending",
            replacement_input_fingerprint=None,
        )

        supersession = json.loads(archived_path.read_text())["supersession"]
        assert supersession["replacement_input_fingerprint"] is None
        assert supersession["pending_recalculation"] is True

    def test_supersede_archive_failure_leaves_current_checkpoint(self, tmp_path, monkeypatch):
        import lib.checkpoint as checkpoint_module

        write_checkpoint(
            tmp_path,
            "proj",
            "script",
            "in_progress",
            artifacts={},
        )
        current = tmp_path / "proj" / "checkpoint_script.json"
        original_write = checkpoint_module._write_durable_json

        def reject_history_write(path, document):
            if path.parent.name == HISTORY_DIRNAME:
                raise OSError("injected archive failure")
            return original_write(path, document)

        monkeypatch.setattr(
            checkpoint_module,
            "_write_durable_json",
            reject_history_write,
        )
        with pytest.raises(OSError, match="injected archive failure"):
            supersede_checkpoint(
                tmp_path,
                "proj",
                "script",
                reason="changed input",
                replacement_input_fingerprint=f"sha256:{'b' * 64}",
            )

        assert current.exists()

    def test_supersede_rejects_symlinked_history_directory(self, tmp_path):
        write_checkpoint(
            tmp_path,
            "proj",
            "script",
            "in_progress",
            artifacts={},
        )
        project = tmp_path / "proj"
        outside = tmp_path / "outside-history"
        outside.mkdir()
        (project / HISTORY_DIRNAME).symlink_to(outside, target_is_directory=True)

        with pytest.raises(CheckpointValidationError, match="directory"):
            supersede_checkpoint(
                tmp_path,
                "proj",
                "script",
                reason="changed input",
                replacement_input_fingerprint=None,
            )

        assert (project / "checkpoint_script.json").exists()
        assert not list(outside.iterdir())

    def test_write_checkpoint_does_not_follow_hostile_temp_symlink(
        self,
        tmp_path,
        monkeypatch,
    ):
        import lib.checkpoint as checkpoint_module

        project = tmp_path / "proj"
        project.mkdir()
        outside = tmp_path / "outside.json"
        outside.write_text("preserve me")
        fixed_hex = "1" * 32

        class FixedUuid:
            hex = fixed_hex

        monkeypatch.setattr(checkpoint_module.uuid, "uuid4", lambda: FixedUuid())
        hostile = project / f".checkpoint_script.json.{fixed_hex}.tmp"
        hostile.symlink_to(outside)

        with pytest.raises(FileExistsError):
            write_checkpoint(
                tmp_path,
                "proj",
                "script",
                "in_progress",
                artifacts={},
            )

        assert outside.read_text() == "preserve me"
        assert hostile.is_symlink()

    @pytest.mark.parametrize("link_kind", ["symlink", "hardlink"])
    def test_get_latest_checkpoint_rejects_unsafe_links(self, tmp_path, link_kind):
        from lib.checkpoint import get_latest_checkpoint

        project = tmp_path / "proj"
        project.mkdir()
        outside = tmp_path / "outside.json"
        outside.write_text(
            json.dumps(
                {
                    "version": "1.0",
                    "project_id": "proj",
                    "pipeline_type": "unknown",
                    "stage": "script",
                    "status": "in_progress",
                    "timestamp": "2026-07-11T00:00:00+00:00",
                    "checkpoint_policy": "guided",
                    "human_approval_required": False,
                    "human_approved": False,
                    "artifacts": {},
                }
            )
        )
        target = project / "checkpoint_script.json"
        if link_kind == "symlink":
            target.symlink_to(outside)
        else:
            os.link(outside, target)

        with pytest.raises(CheckpointValidationError, match="safe single-link"):
            get_latest_checkpoint(tmp_path, "proj")

    @pytest.mark.parametrize(
        "project_id",
        ["../outside", "nested/project", "nested\\project", ".", "..", "/tmp/outside"],
    )
    def test_checkpoint_paths_reject_unsafe_project_ids(self, tmp_path, project_id):
        with pytest.raises(CheckpointValidationError, match="project_id"):
            write_checkpoint(
                tmp_path,
                project_id,
                "script",
                "in_progress",
                artifacts={},
            )

        assert not (tmp_path.parent / "outside" / "checkpoint_script.json").exists()

    def test_checkpoint_paths_reject_symlinked_pipeline_root(self, tmp_path):
        real_root = tmp_path / "real-root"
        real_root.mkdir()
        linked_root = tmp_path / "linked-root"
        linked_root.symlink_to(real_root, target_is_directory=True)

        with pytest.raises(CheckpointValidationError, match="symlink"):
            write_checkpoint(
                linked_root,
                "proj",
                "script",
                "in_progress",
                artifacts={},
            )

        assert not list(real_root.iterdir())

    def test_checkpoint_paths_reject_symlinked_project_directory(self, tmp_path):
        outside = tmp_path / "outside"
        outside.mkdir()
        root = tmp_path / "projects"
        root.mkdir()
        (root / "proj").symlink_to(outside, target_is_directory=True)

        with pytest.raises(CheckpointValidationError, match="symlink"):
            write_checkpoint(
                root,
                "proj",
                "script",
                "in_progress",
                artifacts={},
            )

        assert not list(outside.iterdir())

    def test_supersede_rejects_project_id_escape_before_reading(self, tmp_path):
        outside = tmp_path.parent / "outside"
        outside.mkdir(exist_ok=True)
        checkpoint = outside / "checkpoint_script.json"
        checkpoint.write_text("preserve me")

        with pytest.raises(CheckpointValidationError, match="project_id"):
            supersede_checkpoint(
                tmp_path,
                "../outside",
                "script",
                reason="changed input",
                replacement_input_fingerprint=None,
            )

        assert checkpoint.read_text() == "preserve me"


class TestInitProject:
    def test_creates_layout_and_marker(self, tmp_path):
        pdir = init_project(
            "my-film", title="My Film", pipeline_type="cinematic",
            pipeline_dir=tmp_path, style_playbook="clean-professional",
        )
        assert (pdir / "artifacts").is_dir()
        assert (pdir / "assets" / "images").is_dir()
        assert (pdir / "renders").is_dir()
        marker = json.loads((pdir / PROJECT_MARKER_FILENAME).read_text())
        assert marker["project_id"] == "my-film"
        assert marker["pipeline_type"] == "cinematic"
        assert marker["style_playbook"] == "clean-professional"
        assert "created_at" in marker

    def test_idempotent_preserves_created_at(self, tmp_path):
        pdir = init_project("p", title="P", pipeline_type="cinematic", pipeline_dir=tmp_path)
        created = json.loads((pdir / PROJECT_MARKER_FILENAME).read_text())["created_at"]
        init_project("p", title="P2", pipeline_type="cinematic", pipeline_dir=tmp_path)
        marker = json.loads((pdir / PROJECT_MARKER_FILENAME).read_text())
        assert marker["created_at"] == created
        assert marker["title"] == "P2"


class TestEvents:
    def test_emit_and_read_roundtrip(self, tmp_path):
        emit_event(tmp_path, {"tool": "t1", "event": "start", "scene_id": "sc1"})
        emit_event(tmp_path, {"tool": "t1", "event": "finish", "duration_s": 1.2})
        events = read_events(tmp_path)
        assert len(events) == 2
        assert events[0]["event"] == "start"
        assert events[1]["duration_s"] == 1.2
        assert all("ts" in e for e in events)

    def test_read_tolerates_garbage_lines(self, tmp_path):
        (tmp_path / "events.jsonl").write_text('{"ok": 1}\nnot json\n{"ok": 2}\n')
        events = read_events(tmp_path)
        assert [e["ok"] for e in events] == [1, 2]

    def test_infer_project_dir_from_output_path(self):
        from lib.events import PROJECTS_DIR
        target = PROJECTS_DIR / "some-proj" / "assets" / "images" / "x.png"
        assert infer_project_dir({"output_path": str(target)}) == PROJECTS_DIR / "some-proj"
        assert infer_project_dir({"output_path": "C:/elsewhere/x.png"}) is None
        assert infer_project_dir("not-a-dict") is None


class TestBaseToolInstrumentation:
    def test_execute_emits_events(self, tmp_path, monkeypatch):
        import lib.events as events_mod
        monkeypatch.setattr(events_mod, "PROJECTS_DIR", tmp_path)

        from tools.base_tool import BaseTool, ToolResult

        class FakeTool(BaseTool):
            name = "fake_tool"

            def execute(self, inputs):
                return ToolResult(success=True, cost_usd=0.05)

        project = tmp_path / "proj-x"
        project.mkdir()
        out = project / "assets" / "clip.mp4"
        FakeTool().execute({"output_path": str(out), "scene_id": "sc3"})

        events = read_events(project)
        assert [e["event"] for e in events] == ["start", "finish"]
        assert events[0]["scene_id"] == "sc3"
        assert events[1]["success"] is True
        assert events[1]["cost_usd"] == 0.05

    def test_execute_emits_error_event_and_reraises(self, tmp_path, monkeypatch):
        import lib.events as events_mod
        monkeypatch.setattr(events_mod, "PROJECTS_DIR", tmp_path)

        from tools.base_tool import BaseTool

        class BoomTool(BaseTool):
            name = "boom_tool"

            def execute(self, inputs):
                raise RuntimeError("kaput")

        project = tmp_path / "proj-y"
        project.mkdir()
        with pytest.raises(RuntimeError, match="kaput"):
            BoomTool().execute({"output_path": str(project / "a.png")})
        events = read_events(project)
        assert [e["event"] for e in events] == ["start", "error"]
        assert "kaput" in events[1]["error"]

    def test_unattributable_call_emits_nothing_and_works(self, tmp_path):
        from tools.base_tool import BaseTool, ToolResult

        class PlainTool(BaseTool):
            name = "plain_tool"

            def execute(self, inputs):
                return ToolResult(success=True)

        result = PlainTool().execute({"text": "hello"})
        assert result.success is True
