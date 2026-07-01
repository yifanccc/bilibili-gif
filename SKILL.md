---
name: bilibili-gif
description: Create transparent GIF stickers from Bilibili videos, BV IDs, or local video clips. Use when a user asks to turn a B站/Bilibili clip or a specific time range into a GIF 表情, 抽帧, 抠图, 透明背景 GIF, or asks to select part of a video by natural-language target such as "左边戴帽子的人".
---

# Bilibili GIF

## Workflow

Use `scripts/bili_gif.py` for deterministic clip extraction and GIF assembly.

1. Confirm the user has rights to process the clip. Do not bypass paid, member-only, region-locked, or copyright-protected content.
2. Prefer a local video file when available. Bilibili URL/BV input is best-effort because Bilibili frequently changes anti-scraping behavior.
3. Treat the user's time range as approximate. First run preview mode to produce a timeline sheet and one processed transparent preview frame. Confirm/adjust time and crop before rendering.
4. If the user names a target naturally, parse it into subject, position, and attributes. If no vision backend is available, use preview output to choose a manual crop rectangle.
5. Prefer alpha matte over binary masks for final assets. Use rembg alpha matting (`--rembg-alpha-matting --rembg-post-process-mask`) or refine an existing coarse alpha with `--alpha-matte pymatting`.
6. Use `--mode chromakey` for solid-color backgrounds, `--mode edge` for smooth connected backgrounds such as gradients, `--mode auto` for rembg background removal, and `--mode none` when frames are already transparent or only format conversion is needed.
   - `--mode auto` defaults to `--rembg-model auto`, which routes through a small candidate pool from `--subject-type` and the target hint. Use `--compare-rembg-models` in preview mode when quality is uncertain.
   - Do not use BRIA/RMBG model weights for commercial work unless the user confirms they have the needed license.
7. Render only after confirmation by passing `--render`. The script defaults to `--max-frames 12`; raise it only when the user explicitly asks for more frames.
8. During long downloads, rembg runs, or GIF encoding, wait for the command result with a longer tool timeout instead of repeatedly asking the user to confirm progress.

Default work layout:

```text
<work-dir>/
  video/
  frames_raw/
  frames_rgba/
  preview/
    timeline_contact_sheet.png
    preview_processed.png
  output/
    sticker.gif
    gif_transparent.gif
    gif_white.gif
    webp_transparent.webp
    white_check.png
    checker_sheet.png
```

## Quick Commands

Preview first. This writes a raw timeline sheet and one processed transparent frame, but no GIF:

```bash
python /Users/caoyifan/.codex/skills/bilibili-gif/scripts/bili_gif.py ./clip.mp4 \
  --start 00:00:01 --end 00:00:04 \
  --target "左边戴帽子的人" \
  --crop auto --mode auto --rembg-model auto \
  --subject-type auto --compare-rembg-models \
  --work-dir ./example-work
```

Render after the processed preview and timeline confirm the exact range/crop:

```bash
python /Users/caoyifan/.codex/skills/bilibili-gif/scripts/bili_gif.py ./clip.mp4 \
  --start 12.5 --end 15 \
  --crop 120,80,360,420 \
  --mode auto --rembg-model auto \
  --rembg-alpha-matting --rembg-post-process-mask \
  --render --max-frames 12 \
  --work-dir ./example-work
```

The render writes three final delivery formats:

- `output/gif_white.gif`: most compatible, no transparency edge artifacts.
- `output/gif_transparent.gif` and `output/sticker.gif`: transparent GIF, compatible but hard-edged.
- `output/webp_transparent.webp`: best transparency quality when the target platform supports WebP.

Render WebP first, then generate all GIF outputs from `output/webp_transparent.webp`. This keeps one high-quality alpha intermediate while still delivering WeChat-compatible GIF files.

Bilibili URL/BV input, best effort:

```bash
python /Users/caoyifan/.codex/skills/bilibili-gif/scripts/bili_gif.py "https://www.bilibili.com/video/BV..." \
  --start 00:01:23 --end 00:01:27 \
  --cookies-from-browser chrome \
  --crop auto --mode auto \
  --work-dir ./example-work
```

## Dependencies

- Required for video input: `ffmpeg`.
- Required for frame processing: Python with `Pillow`.
- Optional for Bilibili URL input: `yt-dlp`; failures are expected under Bilibili anti-scraping or login checks.
- Optional for `--mode auto`: `rembg`.
- Optional for `--alpha-matte pymatting`: `PyMatting`.
- Optional for natural-language object localization: Grounding DINO or Florence-2 for boxes, plus SAM/SAM2 for masks.

## References

- Read `references/transparent-gif.md` before handling transparency, natural-language targets, or mask quality issues.
- Read `references/bilibili.md` before using Bilibili URL/BV input or cookies.
