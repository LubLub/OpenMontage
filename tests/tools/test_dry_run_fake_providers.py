"""Offline deterministic provider facade used by the Studio Dry Run."""

from __future__ import annotations

from pathlib import Path

from testing.fake_providers import DryRunProviderSet
from tools.tool_registry import ToolRegistry


FAKE_TOOL_NAMES = {
    "dry_run_narration",
    "dry_run_image",
    "dry_run_music",
    "dry_run_video",
    "dry_run_thumbnail",
}


def test_fake_providers_are_not_auto_discovered() -> None:
    registry = ToolRegistry()
    registry.discover()

    assert FAKE_TOOL_NAMES.isdisjoint(registry.list_all())


def test_stable_facade_writes_identifiable_zero_cost_artifacts(tmp_path: Path) -> None:
    providers = DryRunProviderSet()
    calls = [
        providers.narration(tmp_path / "audio/narration.wav", "Measured narration."),
        providers.image(tmp_path / "images/scene.png", "A quiet historical room"),
        providers.music(tmp_path / "audio/music.wav", 0.25),
        providers.video(tmp_path / "video/hero.mp4", {"scene": "hero", "seed": 7}),
        providers.thumbnail(tmp_path / "images/thumbnail.png", "Quiet history"),
    ]

    for result in calls:
        assert result.success is True
        assert result.cost_usd == 0.0
        assert result.data["provider"] == "dry_run"
        assert result.data["tool"] in FAKE_TOOL_NAMES
        assert Path(result.data["path"]).is_file()
        assert len(result.data["sha256"]) == 64


def test_same_inputs_produce_same_bytes(tmp_path: Path) -> None:
    providers = DryRunProviderSet()

    first = providers.image(tmp_path / "a.png", "same prompt")
    second = providers.image(tmp_path / "b.png", "same prompt")
    video_a = providers.video(tmp_path / "a.mp4", {"b": 2, "a": 1})
    video_b = providers.video(tmp_path / "b.mp4", {"a": 1, "b": 2})

    assert first.data["sha256"] == second.data["sha256"]
    assert video_a.data["sha256"] == video_b.data["sha256"]
