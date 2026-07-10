"""Deterministic, zero-cost provider adapters for offline pipeline Dry Runs.

This module deliberately lives outside ``tools/`` so normal registry discovery
cannot expose fake providers to production selection or provider menus.
"""

from __future__ import annotations

import hashlib
import json
import struct
import wave
import zlib
from pathlib import Path
from typing import Any

from tools.base_tool import (
    BaseTool,
    Determinism,
    ToolResult,
    ToolRuntime,
    ToolStability,
    ToolTier,
)


def _path(value: str | Path) -> Path:
    path = Path(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _result(tool: BaseTool, path: Path) -> ToolResult:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return ToolResult(
        success=True,
        data={
            "tool": tool.name,
            "provider": tool.provider,
            "path": str(path),
            "sha256": digest,
        },
        artifacts=[str(path)],
        cost_usd=0.0,
        model="deterministic-fixture-v1",
    )


def _write_silent_wav(path: Path, seconds: float) -> None:
    frame_rate = 8_000
    frame_count = max(1, round(seconds * frame_rate))
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(frame_rate)
        output.writeframes(b"\x00\x00" * frame_count)


def _png_bytes(seed_text: str) -> bytes:
    digest = hashlib.sha256(seed_text.encode("utf-8")).digest()
    pixel = bytes((digest[0], digest[1], digest[2], 255))
    raw = b"\x00" + pixel

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, level=9))
        + chunk(b"IEND", b"")
    )


class _DryRunTool(BaseTool):
    provider = "dry_run"
    stability = ToolStability.BETA
    determinism = Determinism.DETERMINISTIC
    runtime = ToolRuntime.LOCAL
    dependencies: list[str] = []
    best_for = ["offline deterministic pipeline verification"]
    not_good_for = ["creative evaluation", "production output"]
    supports = {"offline": True, "fake": True, "zero_cost": True}

    def estimate_cost(self, inputs: dict[str, Any]) -> float:
        return 0.0


class DryRunNarration(_DryRunTool):
    name = "dry_run_narration"
    tier = ToolTier.VOICE
    capability = "tts"

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        path = _path(inputs["output_path"])
        _write_silent_wav(path, 1.0)
        return _result(self, path)


class DryRunImage(_DryRunTool):
    name = "dry_run_image"
    tier = ToolTier.GENERATE
    capability = "image_generation"

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        path = _path(inputs["output_path"])
        path.write_bytes(_png_bytes(str(inputs.get("prompt", ""))))
        return _result(self, path)


class DryRunMusic(_DryRunTool):
    name = "dry_run_music"
    tier = ToolTier.GENERATE
    capability = "music_generation"

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        seconds = float(inputs.get("seconds", 1.0))
        if seconds <= 0:
            return ToolResult(success=False, error="seconds must be positive")
        path = _path(inputs["output_path"])
        _write_silent_wav(path, seconds)
        return _result(self, path)


class DryRunVideo(_DryRunTool):
    name = "dry_run_video"
    tier = ToolTier.GENERATE
    capability = "video_generation"

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        path = _path(inputs["output_path"])
        payload = json.dumps(
            inputs.get("payload", {}),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        marker = hashlib.sha256(payload).digest()
        path.write_bytes(b"\x00\x00\x00\x18ftypmp42dry-run\x00\x00" + marker)
        return _result(self, path)


class DryRunThumbnail(_DryRunTool):
    name = "dry_run_thumbnail"
    tier = ToolTier.GENERATE
    capability = "thumbnail_generation"

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        path = _path(inputs["output_path"])
        path.write_bytes(_png_bytes(str(inputs.get("prompt", ""))))
        return _result(self, path)


class DryRunProviderSet:
    """Stable facade used by the Studio's offline Dry Run runner."""

    def __init__(self) -> None:
        self._narration = DryRunNarration()
        self._image = DryRunImage()
        self._music = DryRunMusic()
        self._video = DryRunVideo()
        self._thumbnail = DryRunThumbnail()

    def narration(self, output_path: str | Path, text: str) -> ToolResult:
        return self._narration.execute({"output_path": str(output_path), "text": text})

    def image(self, output_path: str | Path, prompt: str) -> ToolResult:
        return self._image.execute({"output_path": str(output_path), "prompt": prompt})

    def music(self, output_path: str | Path, seconds: float) -> ToolResult:
        return self._music.execute({"output_path": str(output_path), "seconds": seconds})

    def video(self, output_path: str | Path, payload: Any) -> ToolResult:
        return self._video.execute({"output_path": str(output_path), "payload": payload})

    def thumbnail(self, output_path: str | Path, prompt: str) -> ToolResult:
        return self._thumbnail.execute({"output_path": str(output_path), "prompt": prompt})
