#!/usr/bin/env python3
"""Create transparent GIF stickers from Bilibili clips or local videos."""

from __future__ import annotations

import argparse
import math
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence
from urllib.parse import parse_qs, urlparse


BV_RE = re.compile(r"(BV[0-9A-Za-z]{10})")
DEFAULT_REMBG_MODEL = "auto"
FALLBACK_REMBG_MODEL = "isnet-general-use"
REMBG_MODEL_POOLS = {
    "effects": ["u2net", "birefnet-general-lite", "isnet-anime", "isnet-general-use"],
    "anime": ["isnet-anime", "birefnet-general", "birefnet-massive", "u2net", "isnet-general-use"],
    "portrait": ["birefnet-portrait", "birefnet-general", "u2net_human_seg", "u2net"],
    "object": ["birefnet-general", "isnet-general-use", "u2net"],
    "general": [FALLBACK_REMBG_MODEL, "u2net", "birefnet-general"],
}


@dataclass(frozen=True)
class SourceInfo:
    kind: str
    original: str
    bvid: str | None = None
    page: int = 1
    path: Path | None = None


@dataclass(frozen=True)
class TargetHint:
    raw: str
    subject: str | None
    position: str | None
    attributes: list[str]


@dataclass(frozen=True)
class CandidateBox:
    label: str
    score: float
    box: tuple[int, int, int, int]
    image_size: tuple[int, int]

    @property
    def center(self) -> tuple[float, float]:
        x1, y1, x2, y2 = self.box
        width, height = self.image_size
        return ((x1 + x2) / 2 / width, (y1 + y2) / 2 / height)


@dataclass(frozen=True)
class WorkLayout:
    root: Path
    video_dir: Path
    frames_raw_dir: Path
    frames_rgba_dir: Path
    preview_dir: Path
    output_dir: Path
    source_video: Path
    preview_processed: Path
    timeline_sheet: Path
    output_gif: Path
    transparent_gif: Path
    white_gif: Path
    transparent_webp: Path
    white_check: Path
    checker_sheet: Path


def parse_timestamp(value: str) -> float:
    text = value.strip()
    if not text:
        raise ValueError("timestamp cannot be empty")
    parts = text.split(":")
    try:
        if len(parts) == 1:
            seconds = float(parts[0])
        elif len(parts) == 2:
            seconds = int(parts[0]) * 60 + float(parts[1])
        elif len(parts) == 3:
            seconds = int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
        else:
            raise ValueError
    except ValueError as exc:
        raise ValueError(f"invalid timestamp: {value}") from exc
    if seconds < 0:
        raise ValueError("timestamp cannot be negative")
    return seconds


def format_timestamp(seconds: float) -> str:
    whole = int(seconds)
    millis = int(round((seconds - whole) * 1000))
    minutes, sec = divmod(whole, 60)
    hours, minute = divmod(minutes, 60)
    return f"{hours:02d}:{minute:02d}:{sec:02d}.{millis:03d}"


def parse_time_range(start: str, end: str, max_duration: float = 8.0) -> tuple[float, float]:
    start_s = parse_timestamp(start)
    end_s = parse_timestamp(end)
    if start_s >= end_s:
        raise ValueError("start must be before end")
    duration = end_s - start_s
    if duration > max_duration:
        raise ValueError(f"duration {duration:.2f}s exceeds max duration {max_duration:.2f}s")
    return start_s, duration


def parse_source(source: str) -> SourceInfo:
    match = BV_RE.search(source)
    if match:
        parsed = urlparse(source)
        page = 1
        if parsed.query:
            value = parse_qs(parsed.query).get("p", ["1"])[0]
            try:
                page = max(1, int(value))
            except ValueError:
                page = 1
        return SourceInfo(kind="bilibili", original=source, bvid=match.group(1), page=page)
    return SourceInfo(kind="file", original=source, path=Path(source).expanduser())


def parse_crop(value: str) -> str | tuple[int, int, int, int]:
    if value == "auto":
        return "auto"
    parts = [part.strip() for part in value.split(",")]
    if len(parts) != 4:
        raise ValueError("crop must be 'auto' or x,y,w,h")
    try:
        x, y, width, height = (int(part) for part in parts)
    except ValueError as exc:
        raise ValueError("crop values must be integers") from exc
    if width <= 0 or height <= 0:
        raise ValueError("crop needs positive width and height")
    if x < 0 or y < 0:
        raise ValueError("crop x and y cannot be negative")
    return x, y, width, height


def parse_target_hint(text: str | None) -> TargetHint | None:
    if not text:
        return None
    raw = text.strip()
    position = None
    position_patterns = [
        ("bottom-right", ("右下", "右下角", "下方右")),
        ("bottom-left", ("左下", "左下角", "下方左")),
        ("top-right", ("右上", "右上角", "上方右")),
        ("top-left", ("左上", "左上角", "上方左")),
        ("left", ("左边", "左侧", "左面")),
        ("right", ("右边", "右侧", "右面")),
        ("top", ("上方", "上面", "顶部")),
        ("bottom", ("下方", "下面", "底部")),
        ("center", ("中间", "中央", "正中")),
    ]
    for normalized, words in position_patterns:
        if any(word in raw for word in words):
            position = normalized
            break

    subject = None
    subject_patterns = [
        ("person", ("人", "人物", "角色", "男生", "女生", "小孩", "孩子")),
        ("cat", ("猫", "猫咪")),
        ("dog", ("狗", "狗狗")),
        ("face", ("脸", "表情", "头像")),
        ("hand", ("手", "手势")),
        ("text", ("字", "文字", "字幕")),
    ]
    for normalized, words in subject_patterns:
        if any(word in raw for word in words):
            subject = normalized
            break

    attributes = [word for word in ("帽子", "眼镜", "红色", "蓝色", "黄色", "白色", "黑色", "拿着") if word in raw]
    return TargetHint(raw=raw, subject=subject, position=position, attributes=attributes)


def split_models(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def infer_subject_type(subject_type: str, hint: TargetHint | None) -> str:
    if subject_type != "auto":
        return subject_type
    text = hint.raw if hint else ""
    if any(word in text for word in ("气泡", "光晕", "樱花", "特效", "贴纸")):
        return "effects"
    if any(word in text for word in ("Q版", "动漫", "动画", "游戏", "角色", "二次元")):
        return "anime"
    if any(word in text for word in ("真人", "半身", "人像", "头像", "脸")):
        return "portrait"
    if hint and hint.subject == "person":
        return "anime"
    return "general"


def resolve_rembg_model_pool(
    rembg_model_pool: str | None,
    subject_type: str,
    hint: TargetHint | None,
    rembg_model: str | None = None,
) -> list[str]:
    explicit_pool = split_models(rembg_model_pool)
    if explicit_pool and rembg_model_pool != "auto":
        return explicit_pool
    if rembg_model and rembg_model != "auto":
        return [rembg_model]
    inferred = infer_subject_type(subject_type, hint)
    return list(REMBG_MODEL_POOLS.get(inferred, REMBG_MODEL_POOLS["general"]))


def choose_candidate(candidates: Sequence[CandidateBox], hint: TargetHint | None) -> CandidateBox:
    if not candidates:
        raise ValueError("no candidates to choose from")
    if hint is None:
        return max(candidates, key=lambda candidate: candidate.score)

    def position_score(candidate: CandidateBox) -> float:
        cx, cy = candidate.center
        if hint.position == "left":
            return 1.0 - cx
        if hint.position == "right":
            return cx
        if hint.position == "top":
            return 1.0 - cy
        if hint.position == "bottom":
            return cy
        if hint.position == "center":
            return 1.0 - math.hypot(cx - 0.5, cy - 0.5)
        if hint.position == "top-left":
            return 1.0 - math.hypot(cx, cy)
        if hint.position == "top-right":
            return 1.0 - math.hypot(1.0 - cx, cy)
        if hint.position == "bottom-left":
            return 1.0 - math.hypot(cx, 1.0 - cy)
        if hint.position == "bottom-right":
            return 1.0 - math.hypot(1.0 - cx, 1.0 - cy)
        return 0.5

    def label_score(candidate: CandidateBox) -> float:
        if hint.subject is None:
            return 0.5
        return 1.0 if candidate.label.lower() in {hint.subject, "human" if hint.subject == "person" else hint.subject} else 0.0

    return max(candidates, key=lambda c: c.score * 0.30 + position_score(c) * 0.55 + label_score(c) * 0.15)


def find_binary(name: str) -> Path | None:
    found = shutil.which(name)
    if found:
        return Path(found)
    candidates = [
        Path(f"/usr/local/opt/{name}/bin/{name}"),
        Path(f"/opt/homebrew/opt/{name}/bin/{name}"),
        Path(f"/usr/local/bin/{name}"),
        Path(f"/opt/homebrew/bin/{name}"),
    ]
    return next((path for path in candidates if path.exists()), None)


def run_command(command: Sequence[str]) -> None:
    proc = subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if proc.returncode != 0:
        raise RuntimeError(f"command failed ({proc.returncode}): {' '.join(command)}\n{proc.stdout}")


def require_pillow():
    try:
        from PIL import Image, ImageChops
    except ImportError as exc:
        raise RuntimeError("Pillow is required for transparent frame processing. Install with: python -m pip install pillow") from exc
    return Image, ImageChops


def build_work_layout(work_dir: Path, output: Path | None = None) -> WorkLayout:
    root = Path(work_dir)
    video_dir = root / "video"
    frames_raw_dir = root / "frames_raw"
    frames_rgba_dir = root / "frames_rgba"
    preview_dir = root / "preview"
    output_dir = root / "output"
    output_gif = output if output is not None else output_dir / "sticker.gif"
    return WorkLayout(
        root=root,
        video_dir=video_dir,
        frames_raw_dir=frames_raw_dir,
        frames_rgba_dir=frames_rgba_dir,
        preview_dir=preview_dir,
        output_dir=output_dir,
        source_video=video_dir / "source.mp4",
        preview_processed=preview_dir / "preview_processed.png",
        timeline_sheet=preview_dir / "timeline_contact_sheet.png",
        output_gif=output_gif,
        transparent_gif=output_dir / "gif_transparent.gif",
        white_gif=output_dir / "gif_white.gif",
        transparent_webp=output_dir / "webp_transparent.webp",
        white_check=output_dir / "white_check.png",
        checker_sheet=output_dir / "checker_sheet.png",
    )


def ensure_work_layout(layout: WorkLayout) -> None:
    for directory in (
        layout.video_dir,
        layout.frames_raw_dir,
        layout.frames_rgba_dir,
        layout.preview_dir,
        layout.output_dir,
    ):
        directory.mkdir(parents=True, exist_ok=True)


def clear_generated_frames(directory: Path) -> None:
    if not directory.exists():
        return
    for frame in directory.glob("frame_*.png"):
        frame.unlink()


def source_to_video(
    source: SourceInfo,
    start: float,
    duration: float,
    work_dir: Path,
    cookie_file: Path | None,
    cookies_from_browser: str | None = None,
) -> Path:
    if source.kind == "file":
        assert source.path is not None
        if not source.path.exists():
            raise FileNotFoundError(f"local video not found: {source.path}")
        return source.path

    yt_dlp = find_binary("yt-dlp")
    ffmpeg = find_binary("ffmpeg")
    if yt_dlp is None:
        raise RuntimeError("Bilibili URL input needs yt-dlp for this prototype. Provide a local video file instead.")
    if ffmpeg is None:
        raise RuntimeError("Bilibili clip download needs ffmpeg for merging/cutting. Provide a local video file instead.")

    assert source.bvid is not None
    url = source.original if source.original.startswith("http") else f"https://www.bilibili.com/video/{source.bvid}/"
    video_dir = work_dir
    video_dir.mkdir(parents=True, exist_ok=True)
    output = video_dir / "source.%(ext)s"
    command = [
        str(yt_dlp),
        "--ffmpeg-location",
        str(ffmpeg.parent),
        "--force-overwrites",
        "--no-playlist",
        "-f",
        "bv*[vcodec^=avc1]/b[vcodec^=avc1]/bv*",
        "--download-sections",
        f"*{format_timestamp(start)}-{format_timestamp(start + duration)}",
        "--force-keyframes-at-cuts",
        "--merge-output-format",
        "mp4",
        "-o",
        str(output),
    ]
    if cookie_file:
        command.extend(["--cookies", str(cookie_file)])
    if cookies_from_browser:
        command.extend(["--cookies-from-browser", cookies_from_browser])
    command.append(url)
    try:
        run_command(command)
    except RuntimeError as exc:
        raise RuntimeError(
            "Bilibili download failed. This is often caused by login, region, copyright, or anti-scraping checks. "
            "Export the clip manually and pass the local video file, or retry with --cookie-file.\n"
            f"{exc}"
        ) from exc

    matches = sorted(video_dir.glob("source.*"))
    if not matches:
        raise RuntimeError("yt-dlp finished but no clip file was created")
    return matches[0]


def extract_frames(
    video: Path,
    start: float,
    duration: float,
    frames_dir: Path,
    fps: int,
    width: int,
    crop: str | tuple[int, int, int, int],
) -> list[Path]:
    ffmpeg = find_binary("ffmpeg")
    if ffmpeg is None:
        raise RuntimeError("ffmpeg is required. Install it with: brew install ffmpeg")
    frames_dir.mkdir(parents=True, exist_ok=True)
    filters: list[str] = [f"fps={fps}"]
    if isinstance(crop, tuple):
        x, y, crop_w, crop_h = crop
        filters.append(f"crop={crop_w}:{crop_h}:{x}:{y}")
    filters.append(f"scale={width}:-2:flags=lanczos")
    command = [
        str(ffmpeg),
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        f"{start:.3f}",
        "-t",
        f"{duration:.3f}",
        "-i",
        str(video),
        "-vf",
        ",".join(filters),
        str(frames_dir / "frame_%04d.png"),
    ]
    run_command(command)
    frames = sorted(frames_dir.glob("frame_*.png"))
    if not frames:
        raise RuntimeError("ffmpeg did not extract any frames")
    return frames


def chromakey_frame(image, key_rgb: tuple[int, int, int], threshold: int):
    rgba = image.convert("RGBA")
    pixels = rgba.load()
    width, height = rgba.size
    limit_sq = threshold * threshold
    kr, kg, kb = key_rgb
    for y in range(height):
        for x in range(width):
            r, g, b, a = pixels[x, y]
            distance_sq = (r - kr) ** 2 + (g - kg) ** 2 + (b - kb) ** 2
            if distance_sq <= limit_sq:
                pixels[x, y] = (r, g, b, 0)
    return rgba


def edge_remove_frame(image, threshold: int):
    rgba = image.convert("RGBA")
    pixels = rgba.load()
    width, height = rgba.size
    visited = bytearray(width * height)
    queue: list[tuple[int, int]] = []
    threshold_sq = threshold * threshold

    def push(x: int, y: int) -> None:
        index = y * width + x
        if not visited[index]:
            visited[index] = 1
            queue.append((x, y))

    for x in range(width):
        push(x, 0)
        push(x, height - 1)
    for y in range(height):
        push(0, y)
        push(width - 1, y)

    while queue:
        x, y = queue.pop()
        r, g, b, _ = pixels[x, y]
        pixels[x, y] = (r, g, b, 0)
        for nx, ny in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
            if nx < 0 or ny < 0 or nx >= width or ny >= height:
                continue
            index = ny * width + nx
            if visited[index]:
                continue
            nr, ng, nb, _ = pixels[nx, ny]
            distance_sq = (nr - r) ** 2 + (ng - g) ** 2 + (nb - b) ** 2
            if distance_sq <= threshold_sq:
                visited[index] = 1
                queue.append((nx, ny))
    return rgba


def parse_hex_color(value: str) -> tuple[int, int, int]:
    text = value.strip().lstrip("#")
    if len(text) != 6:
        raise ValueError("color must be RRGGBB or #RRGGBB")
    try:
        return int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16)
    except ValueError as exc:
        raise ValueError("color must be valid hex") from exc


def make_transparent_frames(
    raw_frames: Sequence[Path],
    transparent_dir: Path,
    mode: str,
    key_color: str,
    key_threshold: int,
    rembg_model: str = FALLBACK_REMBG_MODEL,
    rembg_alpha_matting: bool = False,
    rembg_post_process_mask: bool = False,
) -> list[Path]:
    Image, _ = require_pillow()
    transparent_dir.mkdir(parents=True, exist_ok=True)
    output: list[Path] = []

    if mode == "auto":
        rembg = find_binary("rembg")
        if rembg is None:
            raise RuntimeError("auto background removal needs rembg. Install with: python -m pip install 'rembg[cpu]'")
        for frame in raw_frames:
            out = transparent_dir / frame.name
            command = [str(rembg), "i", "-m", rembg_model]
            if rembg_alpha_matting:
                command.append("-a")
            if rembg_post_process_mask:
                command.append("-ppm")
            command.extend([str(frame), str(out)])
            run_command(command)
            output.append(out)
        return output

    key_rgb = parse_hex_color(key_color)
    for frame in raw_frames:
        image = Image.open(frame)
        if mode == "chromakey":
            transparent = chromakey_frame(image, key_rgb, key_threshold)
        elif mode == "edge":
            transparent = edge_remove_frame(image, key_threshold)
        elif mode == "none":
            transparent = image.convert("RGBA")
        else:
            raise ValueError(f"unsupported mode: {mode}")
        out = transparent_dir / frame.name
        transparent.save(out)
        output.append(out)
    return output


def create_trimap_from_alpha(alpha, erode_size: int, dilate_size: int):
    Image, _ = require_pillow()
    from PIL import ImageFilter
    alpha = alpha.convert("L")
    foreground = alpha.point(lambda value: 255 if value >= 250 else 0)
    possible = alpha.point(lambda value: 255 if value > 5 else 0)
    if erode_size > 0:
        foreground = foreground.filter(ImageFilter.MinFilter(erode_size * 2 + 1))
    if dilate_size > 0:
        possible = possible.filter(ImageFilter.MaxFilter(dilate_size * 2 + 1))
    trimap = Image.new("L", alpha.size, 127)
    trimap.paste(0, mask=possible.point(lambda value: 255 if value == 0 else 0))
    trimap.paste(255, mask=foreground)
    return trimap


def refine_alpha_frame(
    raw_frame: Path,
    rgba_frame: Path,
    method: str,
    erode_size: int = 3,
    dilate_size: int = 8,
):
    Image, _ = require_pillow()
    from PIL import ImageFilter
    raw = Image.open(raw_frame).convert("RGB")
    rgba = Image.open(rgba_frame).convert("RGBA")
    alpha = rgba.getchannel("A")
    if method == "none":
        return rgba
    if raw.size != rgba.size:
        raw = raw.resize(rgba.size, Image.Resampling.LANCZOS)
    if method == "feather":
        soft = alpha.filter(ImageFilter.GaussianBlur(max(0.5, dilate_size / 3)))
        out = raw.convert("RGBA")
        out.putalpha(soft)
        return out
    if method != "pymatting":
        raise ValueError(f"unsupported alpha matte method: {method}")

    try:
        import numpy as np
        from pymatting import estimate_alpha_cf, estimate_foreground_ml
    except ImportError as exc:
        raise RuntimeError("PyMatting is required for --alpha-matte pymatting. Install with: python -m pip install pymatting") from exc

    trimap_image = create_trimap_from_alpha(alpha, erode_size, dilate_size)
    image_np = np.asarray(raw, dtype=np.float64) / 255.0
    trimap_np = np.asarray(trimap_image, dtype=np.float64) / 255.0
    estimated_alpha = estimate_alpha_cf(
        image_np,
        trimap_np,
        cg_kwargs={"maxiter": 160, "rtol": 1e-5},
    )
    estimated_alpha = np.clip(estimated_alpha, 0.0, 1.0)
    estimated_alpha[trimap_np <= 0.01] = 0.0
    estimated_alpha[trimap_np >= 0.99] = 1.0
    smoothed = Image.fromarray((estimated_alpha * 255).astype("uint8"), "L").filter(ImageFilter.GaussianBlur(0.45))
    estimated_alpha = np.asarray(smoothed, dtype=np.float64) / 255.0
    estimated_alpha[trimap_np <= 0.01] = 0.0
    estimated_alpha[trimap_np >= 0.99] = 1.0
    foreground = estimate_foreground_ml(image_np, estimated_alpha)
    foreground = np.clip(foreground, 0.0, 1.0)
    out_np = np.dstack((foreground, estimated_alpha)) * 255.0
    return Image.fromarray(np.clip(out_np, 0, 255).astype("uint8"), "RGBA")


def refine_alpha_frames(
    raw_frames: Sequence[Path],
    transparent_frames: Sequence[Path],
    method: str,
    erode_size: int,
    dilate_size: int,
) -> None:
    if method == "none":
        return
    for raw_frame, transparent_frame in zip(raw_frames, transparent_frames):
        refine_alpha_frame(raw_frame, transparent_frame, method, erode_size, dilate_size).save(transparent_frame)


def alpha_bbox(frames: Sequence[Path], padding: int) -> tuple[int, int, int, int] | None:
    Image, ImageChops = require_pillow()
    bbox = None
    image_size = None
    for frame in frames:
        image = Image.open(frame).convert("RGBA")
        image_size = image.size
        alpha = image.getchannel("A")
        frame_bbox = alpha.getbbox()
        if frame_bbox is None:
            continue
        if bbox is None:
            bbox = frame_bbox
        else:
            bbox = (
                min(bbox[0], frame_bbox[0]),
                min(bbox[1], frame_bbox[1]),
                max(bbox[2], frame_bbox[2]),
                max(bbox[3], frame_bbox[3]),
            )
    if bbox is None or image_size is None:
        return None
    width, height = image_size
    return (
        max(0, bbox[0] - padding),
        max(0, bbox[1] - padding),
        min(width, bbox[2] + padding),
        min(height, bbox[3] + padding),
    )


def crop_frames_to_alpha(frames: Sequence[Path], padding: int) -> None:
    Image, _ = require_pillow()
    bbox = alpha_bbox(frames, padding)
    if bbox is None:
        return
    for frame in frames:
        image = Image.open(frame).convert("RGBA")
        image.crop(bbox).save(frame)


def select_frames_for_budget(frames: Sequence[Path], max_frames: int) -> list[Path]:
    if max_frames <= 0:
        raise ValueError("max_frames must be positive")
    frames = list(frames)
    if len(frames) <= max_frames:
        return frames
    if max_frames == 1:
        return [frames[len(frames) // 2]]

    selected: list[Path] = []
    last_index = len(frames) - 1
    for index in range(max_frames):
        frame_index = round(index * last_index / (max_frames - 1))
        frame = frames[frame_index]
        if frame not in selected:
            selected.append(frame)

    cursor = 0
    while len(selected) < max_frames and cursor < len(frames):
        frame = frames[cursor]
        if frame not in selected:
            selected.append(frame)
        cursor += 1
    return sorted(selected, key=frames.index)


def select_preview_frame(frames: Sequence[Path]) -> Path:
    if not frames:
        raise RuntimeError("no frames available for preview")
    return list(frames)[len(frames) // 2]


def _rgba_to_gif_palette_frame(image, alpha_threshold: int = 128):
    Image, _ = require_pillow()
    rgba = image.convert("RGBA")
    alpha = rgba.getchannel("A")
    matte = Image.new("RGB", rgba.size, (255, 255, 255))
    matte.paste(rgba.convert("RGB"), mask=alpha)
    try:
        paletted = matte.quantize(colors=255, method=Image.Quantize.FASTOCTREE)
    except AttributeError:
        paletted = matte.convert("P", palette=Image.ADAPTIVE, colors=255)
    palette = paletted.getpalette()[: 255 * 3]
    palette.extend([0, 0, 0])
    palette.extend([0] * (768 - len(palette)))
    paletted.putpalette(palette)
    transparent_mask = alpha.point(lambda value: 255 if value < alpha_threshold else 0)
    paletted.paste(255, transparent_mask)
    paletted.info["transparency"] = 255
    return paletted


def encode_gif(frames: Sequence[Path], output: Path, fps: int, duration: float | None = None) -> None:
    Image, _ = require_pillow()
    images = [_rgba_to_gif_palette_frame(Image.open(frame)) for frame in frames]
    if not images:
        raise RuntimeError("no transparent frames to encode")
    if duration is not None and duration > 0:
        duration_ms = max(20, round(duration * 1000 / len(images)))
    else:
        duration_ms = max(20, round(1000 / fps))
    output.parent.mkdir(parents=True, exist_ok=True)
    images[0].save(
        output,
        save_all=True,
        append_images=images[1:],
        duration=duration_ms,
        loop=0,
        disposal=2,
        transparency=255,
    )
    if output.stat().st_size == 0:
        raise RuntimeError("GIF output is empty")


def gif_frame_duration_ms(frame_count: int, fps: int, duration: float | None = None) -> int:
    if duration is not None and duration > 0:
        return max(20, round(duration * 1000 / frame_count))
    return max(20, round(1000 / fps))


def encode_white_gif(frames: Sequence[Path], output: Path, fps: int, duration: float | None = None) -> None:
    Image, _ = require_pillow()
    images = []
    for frame in frames:
        rgba = Image.open(frame).convert("RGBA")
        white = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
        white.alpha_composite(rgba)
        try:
            images.append(white.convert("RGB").quantize(colors=256, method=Image.Quantize.FASTOCTREE))
        except AttributeError:
            images.append(white.convert("RGB").convert("P", palette=Image.ADAPTIVE, colors=256))
    if not images:
        raise RuntimeError("no frames to encode")
    output.parent.mkdir(parents=True, exist_ok=True)
    images[0].save(
        output,
        save_all=True,
        append_images=images[1:],
        duration=gif_frame_duration_ms(len(images), fps, duration),
        loop=0,
        disposal=2,
    )
    if output.stat().st_size == 0:
        raise RuntimeError("white GIF output is empty")


def load_animated_image_frames(path: Path):
    Image, _ = require_pillow()
    from PIL import ImageSequence

    image = Image.open(path)
    return [frame.convert("RGBA").copy() for frame in ImageSequence.Iterator(image)]


def encode_gif_from_webp(
    webp: Path,
    output: Path,
    fps: int,
    duration: float | None = None,
    white_background: bool = False,
) -> None:
    Image, _ = require_pillow()
    frames = load_animated_image_frames(webp)
    if white_background:
        output.parent.mkdir(parents=True, exist_ok=True)
        images = []
        for frame in frames:
            white = Image.new("RGBA", frame.size, (255, 255, 255, 255))
            white.alpha_composite(frame)
            try:
                images.append(white.convert("RGB").quantize(colors=256, method=Image.Quantize.FASTOCTREE))
            except AttributeError:
                images.append(white.convert("RGB").convert("P", palette=Image.ADAPTIVE, colors=256))
        images[0].save(
            output,
            save_all=True,
            append_images=images[1:],
            duration=gif_frame_duration_ms(len(images), fps, duration),
            loop=0,
            disposal=2,
        )
        if output.stat().st_size == 0:
            raise RuntimeError("white GIF output is empty")
        return

    temp_dir = Path(tempfile.mkdtemp(prefix="webp-gif-frames-"))
    try:
        frame_paths = []
        for index, frame in enumerate(frames):
            frame_path = temp_dir / f"frame_{index:04d}.png"
            frame.save(frame_path)
            frame_paths.append(frame_path)
        encode_gif(frame_paths, output, fps, duration)
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def encode_webp(frames: Sequence[Path], output: Path, fps: int, duration: float | None = None) -> None:
    Image, _ = require_pillow()
    images = [Image.open(frame).convert("RGBA") for frame in frames]
    if not images:
        raise RuntimeError("no frames to encode")
    output.parent.mkdir(parents=True, exist_ok=True)
    images[0].save(
        output,
        save_all=True,
        append_images=images[1:],
        duration=gif_frame_duration_ms(len(images), fps, duration),
        loop=0,
        lossless=True,
        quality=95,
        method=6,
    )
    if output.stat().st_size == 0:
        raise RuntimeError("WebP output is empty")


def create_contact_sheet(frames: Sequence[Path], output: Path, columns: int = 5) -> None:
    Image, _ = require_pillow()
    thumbs = [Image.open(frame).convert("RGBA") for frame in frames[:20]]
    if not thumbs:
        return
    output.parent.mkdir(parents=True, exist_ok=True)
    thumb_w = 160
    thumb_h = 120
    rows = math.ceil(len(thumbs) / columns)
    sheet = Image.new("RGBA", (columns * thumb_w, rows * thumb_h), (240, 240, 240, 255))
    for index, image in enumerate(thumbs):
        image.thumbnail((thumb_w, thumb_h))
        x = (index % columns) * thumb_w + (thumb_w - image.width) // 2
        y = (index // columns) * thumb_h + (thumb_h - image.height) // 2
        sheet.alpha_composite(image, (x, y))
    sheet.convert("RGB").save(output)


def write_preview_processed(frame: Path, output: Path) -> None:
    Image, _ = require_pillow()
    output.parent.mkdir(parents=True, exist_ok=True)
    Image.open(frame).convert("RGBA").save(output)


def checkerboard(size: tuple[int, int], cell: int = 12):
    Image, _ = require_pillow()
    width, height = size
    image = Image.new("RGB", size, (238, 238, 238))
    pixels = image.load()
    for y in range(height):
        for x in range(width):
            if (x // cell + y // cell) % 2:
                pixels[x, y] = (194, 194, 194)
    return image


def create_background_sheet(frames: Sequence[Path], output: Path, background: str, columns: int = 4) -> None:
    Image, _ = require_pillow()
    selected = select_frames_for_budget(frames, min(12, len(frames))) if frames else []
    thumbs = []
    for frame in selected:
        rgba = Image.open(frame).convert("RGBA")
        if background == "checker":
            base = checkerboard(rgba.size).convert("RGBA")
        else:
            base = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
        base.alpha_composite(rgba)
        base.thumbnail((180, 140))
        thumbs.append(base.convert("RGB"))
    if not thumbs:
        return
    output.parent.mkdir(parents=True, exist_ok=True)
    thumb_w = 180
    thumb_h = 140
    rows = math.ceil(len(thumbs) / columns)
    sheet = Image.new("RGB", (columns * thumb_w, rows * thumb_h), (250, 250, 250))
    for index, image in enumerate(thumbs):
        x = (index % columns) * thumb_w + (thumb_w - image.width) // 2
        y = (index // columns) * thumb_h + (thumb_h - image.height) // 2
        sheet.paste(image, (x, y))
    sheet.save(output)


def create_quality_check_sheets(frames: Sequence[Path], layout: WorkLayout) -> None:
    create_background_sheet(frames, layout.white_check, "white")
    create_background_sheet(frames, layout.checker_sheet, "checker")


def create_rembg_model_compare(
    preview_frame: Path,
    layout: WorkLayout,
    models: Sequence[str],
    rembg_alpha_matting: bool,
    rembg_post_process_mask: bool,
) -> Path:
    Image, _ = require_pillow()
    rembg = find_binary("rembg")
    if rembg is None:
        raise RuntimeError("rembg model comparison needs rembg")
    compare_dir = layout.preview_dir / "rembg_models"
    compare_dir.mkdir(parents=True, exist_ok=True)
    checker_frames: list[tuple[str, Path]] = []
    for model in models:
        out = compare_dir / f"{model}.png"
        command = [str(rembg), "i", "-m", model]
        if rembg_alpha_matting:
            command.append("-a")
        if rembg_post_process_mask:
            command.append("-ppm")
        command.extend([str(preview_frame), str(out)])
        run_command(command)
        checker_frames.append((model, out))

    thumb_w = 220
    thumb_h = 170
    sheet = Image.new("RGB", (thumb_w * len(checker_frames), thumb_h + 24), (245, 245, 245))
    try:
        from PIL import ImageDraw

        draw = ImageDraw.Draw(sheet)
    except ImportError:
        draw = None
    for index, (model, frame) in enumerate(checker_frames):
        rgba = Image.open(frame).convert("RGBA")
        base = checkerboard(rgba.size).convert("RGBA")
        base.alpha_composite(rgba)
        base.thumbnail((thumb_w, thumb_h))
        x = index * thumb_w + (thumb_w - base.width) // 2
        y = 24 + (thumb_h - base.height) // 2
        sheet.paste(base.convert("RGB"), (x, y))
        if draw is not None:
            draw.text((index * thumb_w + 8, 6), model, fill=(0, 0, 0))
    output = layout.preview_dir / "rembg_model_compare.png"
    sheet.save(output)
    return output


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Create a transparent GIF sticker from a Bilibili clip or local video.")
    parser.add_argument("source", help="Bilibili URL/BV id or local video path")
    parser.add_argument("--start", required=True, help="start timestamp, e.g. 00:01:23.500")
    parser.add_argument("--end", required=True, help="end timestamp")
    parser.add_argument("--output", type=Path, help="output GIF path; defaults to <work-dir>/output/sticker.gif with --render")
    parser.add_argument("--render", action="store_true", help="generate the final GIF; without this only preview files are created")
    parser.add_argument("--target", help="natural-language target hint, e.g. 左边戴帽子的人")
    parser.add_argument("--crop", default="auto", help="'auto' or x,y,w,h before scaling")
    parser.add_argument("--mode", choices=("auto", "chromakey", "edge", "none"), default="chromakey")
    parser.add_argument(
        "--rembg-model",
        default=DEFAULT_REMBG_MODEL,
        help="rembg model for --mode auto; use auto to route from target/subject type",
    )
    parser.add_argument(
        "--subject-type",
        choices=("auto", "effects", "anime", "portrait", "object", "general"),
        default="auto",
        help="target category for rembg model routing",
    )
    parser.add_argument(
        "--rembg-model-pool",
        default="auto",
        help="comma-separated candidate rembg models, or auto",
    )
    parser.add_argument("--compare-rembg-models", action="store_true", help="preview a checkerboard comparison for the rembg model pool")
    parser.add_argument("--rembg-alpha-matting", action="store_true", default=True)
    parser.add_argument("--no-rembg-alpha-matting", action="store_false", dest="rembg_alpha_matting")
    parser.add_argument("--rembg-post-process-mask", action="store_true", default=True)
    parser.add_argument("--no-rembg-post-process-mask", action="store_false", dest="rembg_post_process_mask")
    parser.add_argument("--alpha-matte", choices=("none", "feather", "pymatting"), default="none")
    parser.add_argument("--matte-erode", type=int, default=3)
    parser.add_argument("--matte-dilate", type=int, default=8)
    parser.add_argument("--key-color", default="00ff00", help="chromakey color as RRGGBB")
    parser.add_argument("--key-threshold", type=int, default=80)
    parser.add_argument("--width", type=int, default=480)
    parser.add_argument("--fps", type=int, default=12)
    parser.add_argument("--max-frames", type=int, default=12, help="maximum frames to process during --render")
    parser.add_argument("--max-duration", type=float, default=8)
    parser.add_argument("--padding", type=int, default=16)
    parser.add_argument("--work-dir", type=Path)
    parser.add_argument("--cookie-file", type=Path)
    parser.add_argument("--cookies-from-browser", help="browser name for yt-dlp cookies, e.g. chrome")
    parser.add_argument("--keep-frames", action="store_true", default=True)
    parser.add_argument("--no-keep-frames", action="store_false", dest="keep_frames")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.fps <= 0:
        parser.error("--fps must be positive")
    if args.width <= 0:
        parser.error("--width must be positive")
    if args.max_frames <= 0:
        parser.error("--max-frames must be positive")
    if args.matte_erode < 0 or args.matte_dilate < 0:
        parser.error("--matte-erode and --matte-dilate cannot be negative")

    start, duration = parse_time_range(args.start, args.end, args.max_duration)
    source = parse_source(args.source)
    crop = parse_crop(args.crop)
    target_hint = parse_target_hint(args.target)
    rembg_models = resolve_rembg_model_pool(args.rembg_model_pool, args.subject_type, target_hint, args.rembg_model)
    rembg_model = rembg_models[0] if rembg_models else FALLBACK_REMBG_MODEL

    if target_hint and crop == "auto":
        print(
            f"[bilibili-gif] target hint parsed: subject={target_hint.subject} "
            f"position={target_hint.position} attributes={','.join(target_hint.attributes) or '-'}",
            file=sys.stderr,
        )
        print(
            "[bilibili-gif] visual-language target detection is optional and not bundled; "
            "use --crop x,y,w,h for deterministic selection in this prototype.",
            file=sys.stderr,
        )

    created_temp_dir = None
    try:
        work_dir = args.work_dir
        if work_dir is None:
            work_dir = Path(tempfile.mkdtemp(prefix="bilibili-gif-"))
            created_temp_dir = work_dir
        layout = build_work_layout(work_dir, args.output)
        ensure_work_layout(layout)

        if args.output is not None and not args.render:
            print("[bilibili-gif] note: --output is only used with --render; preview mode will not write a GIF.")

        video = source_to_video(source, start, duration, layout.video_dir, args.cookie_file, args.cookies_from_browser)
        clear_generated_frames(layout.frames_raw_dir)
        clear_generated_frames(layout.frames_rgba_dir)

        raw_start = 0.0 if source.kind == "bilibili" else start
        raw_frames = extract_frames(
            video,
            raw_start,
            duration,
            layout.frames_raw_dir,
            args.fps,
            args.width,
            crop,
        )
        timeline_frames = select_frames_for_budget(raw_frames, min(20, len(raw_frames)))
        create_contact_sheet(timeline_frames, layout.timeline_sheet)

        if args.mode == "auto" and args.compare_rembg_models:
            compare = create_rembg_model_compare(
                select_preview_frame(raw_frames),
                layout,
                rembg_models[:4],
                args.rembg_alpha_matting,
                args.rembg_post_process_mask,
            )
            print(f"[bilibili-gif] rembg model comparison: {compare}")

        if args.render:
            frames_to_process = select_frames_for_budget(raw_frames, args.max_frames)
        else:
            frames_to_process = [select_preview_frame(raw_frames)]

        transparent_frames = make_transparent_frames(
            frames_to_process,
            layout.frames_rgba_dir,
            args.mode,
            args.key_color,
            args.key_threshold,
            rembg_model,
            args.rembg_alpha_matting,
            args.rembg_post_process_mask,
        )
        refine_alpha_frames(frames_to_process, transparent_frames, args.alpha_matte, args.matte_erode, args.matte_dilate)
        if crop == "auto":
            crop_frames_to_alpha(transparent_frames, args.padding)

        write_preview_processed(select_preview_frame(transparent_frames), layout.preview_processed)

        if not args.render:
            print(f"[bilibili-gif] preview frame: {layout.preview_processed}")
            print(f"[bilibili-gif] timeline sheet: {layout.timeline_sheet}")
            print(f"[bilibili-gif] work dir: {layout.root}")
            print("[bilibili-gif] confirm the time range/crop, then rerun with --render for the final GIF.")
            return 0

        create_quality_check_sheets(transparent_frames, layout)
        encode_webp(transparent_frames, layout.transparent_webp, args.fps, duration=duration)
        encode_gif_from_webp(layout.transparent_webp, layout.output_gif, args.fps, duration=duration)
        if layout.transparent_gif != layout.output_gif:
            encode_gif_from_webp(layout.transparent_webp, layout.transparent_gif, args.fps, duration=duration)
        encode_gif_from_webp(layout.transparent_webp, layout.white_gif, args.fps, duration=duration, white_background=True)
        print(f"[bilibili-gif] wrote {layout.output_gif}")
        print(f"[bilibili-gif] transparent GIF: {layout.transparent_gif}")
        print(f"[bilibili-gif] white GIF: {layout.white_gif}")
        print(f"[bilibili-gif] transparent WebP: {layout.transparent_webp}")
        print(f"[bilibili-gif] frames processed: {len(transparent_frames)}")
        print(f"[bilibili-gif] preview frame: {layout.preview_processed}")
        print(f"[bilibili-gif] quality checks: {layout.white_check}, {layout.checker_sheet}")
        print(f"[bilibili-gif] work dir: {layout.root}")
        return 0
    except Exception as exc:
        print(f"[bilibili-gif] error: {exc}", file=sys.stderr)
        return 1
    finally:
        if created_temp_dir is not None and not args.keep_frames:
            shutil.rmtree(created_temp_dir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
