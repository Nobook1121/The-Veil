"""媒体资产派生（设计文档 §8.5）。

上传图片时生成缩略图（长边 640，透明图用 PNG、其余用 JPEG），上传视频时
在 ffmpeg 可用的情况下抽第 1 秒帧生成 poster 并用 ffprobe 读取时长。

所有派生失败都只记录告警并返回空字典：原始文件仍然可用，缩略图接口会回退
到原图，不能让一次缩略图失败阻断素材上传。
"""
from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

THUMB_MAX_EDGE = 640
THUMB_JPEG_QUALITY = 78
FFMPEG_TIMEOUT_SECONDS = 30


def _run(command: list[str]) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=FFMPEG_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def _probe_duration_ms(source: Path) -> int | None:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return None
    result = _run([
        ffprobe,
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(source),
    ])
    if result is None or result.returncode != 0:
        return None
    try:
        return max(0, int(round(float(result.stdout.strip()) * 1000)))
    except (TypeError, ValueError):
        return None


def _to_relative(path: Path, assets_dir: Path) -> str:
    return f"assets/{path.relative_to(assets_dir).as_posix()}"


def image_derivatives(source: Path, assets_dir: Path, stem: str, *, max_edge: int = THUMB_MAX_EDGE) -> dict[str, Any]:
    """生成图片缩略图；小图不放大，直接交给缩略图接口回退原图。"""
    try:
        import pymupdf

        probe = pymupdf.Pixmap(str(source))
        width, height = int(probe.width), int(probe.height)
        has_alpha = bool(probe.alpha)
    except Exception:
        logger.warning("媒体缩略图：无法读取图片尺寸 %s", source, exc_info=True)
        return {}

    result: dict[str, Any] = {"width": width, "height": height}
    if width <= 0 or height <= 0:
        return result
    if max(width, height) <= max_edge:
        return result

    try:
        import pymupdf

        with pymupdf.open(str(source)) as document:
            page = document[0]
            scale = max_edge / max(page.rect.width, page.rect.height, 1.0)
            pixmap = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=has_alpha)
            suffix = ".png" if has_alpha else ".jpg"
            target = assets_dir / f"{stem}_thumb{suffix}"
            if has_alpha:
                pixmap.save(str(target))
            else:
                pixmap.save(str(target), jpg_quality=THUMB_JPEG_QUALITY)
        result["thumb_path"] = _to_relative(target, assets_dir)
    except Exception:
        logger.warning("媒体缩略图：生成失败 %s", source, exc_info=True)
    return result


def video_derivatives(source: Path, assets_dir: Path, stem: str, *, max_edge: int = THUMB_MAX_EDGE) -> dict[str, Any]:
    """生成视频 poster 与时长；缺少 ffmpeg/ffprobe 时只返回时长（若有）。"""
    result: dict[str, Any] = {}
    duration = _probe_duration_ms(source)
    if duration is not None:
        result["duration_ms"] = duration

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        logger.info("媒体 poster：未找到 ffmpeg，跳过 %s", source)
        return result

    target = assets_dir / f"{stem}_poster.jpg"
    for seek in ("1", "0.1"):
        completed = _run([
            ffmpeg,
            "-loglevel", "error",
            "-ss", seek,
            "-i", str(source),
            "-frames:v", "1",
            "-vf", f"scale='min({max_edge},iw)':-2",
            "-y", str(target),
        ])
        if completed is not None and completed.returncode == 0 and target.is_file():
            result["poster_path"] = _to_relative(target, assets_dir)
            break
    else:
        logger.warning("媒体 poster：生成失败 %s", source)
    return result


def cleanup_derivatives(root: Path, resource: Any) -> None:
    """删除资源时同时清理缩略图 / poster，避免派生文件残留。"""
    if not isinstance(resource, dict):
        return
    for key in ("thumb_path", "poster_path"):
        relative = str(resource.get(key) or "").strip()
        if not relative or relative.startswith("../") or "/../" in relative:
            continue
        candidate = (root / relative).resolve()
        try:
            candidate.relative_to(root.resolve())
        except ValueError:
            continue
        if candidate.is_file():
            candidate.unlink()


__all__ = ["image_derivatives", "video_derivatives", "cleanup_derivatives"]
