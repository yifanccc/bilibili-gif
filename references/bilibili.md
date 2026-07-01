# Bilibili Input Notes

## Source policy

Only process content the user has the right to use. Do not bypass paid, member-only, region-locked, deleted, or otherwise restricted content. Do not print cookies or store them in generated artifacts.

## Best-effort URL handling

The script accepts:

- Full Bilibili video URLs.
- Bare BV IDs.
- Local video files.

For Bilibili URL/BV input, the prototype uses `yt-dlp` as a best-effort fetcher because it can hand off the clip to the normal local-video pipeline. Bilibili anti-scraping changes often cause `401`, `403`, `412`, empty streams, or missing quality formats. When this happens, ask the user to export/download the clip manually and pass the local file.

Cookie usage:

```bash
python scripts/bili_gif.py "https://www.bilibili.com/video/BV..." \
  --cookie-file ./cookies.txt \
  --start 00:00:10 --end 00:00:13 \
  --work-dir ./example-work
```

Browser cookie usage:

```bash
python scripts/bili_gif.py "https://www.bilibili.com/video/BV..." \
  --cookies-from-browser chrome \
  --start 00:00:10 --end 00:00:13 \
  --work-dir ./example-work
```

Bilibili downloads are stored as `<work-dir>/video/source.mp4`. The first run should stay in preview mode so the user can confirm `preview/timeline_contact_sheet.png` and `preview/preview_processed.png`; rerun with `--render` only after the exact time range, crop, and removal model are settled.

For hard clips, add `--compare-rembg-models` in preview mode. The comparison can be slow because each candidate model runs once on the preview frame, but it prevents wasting a full multi-frame render on the wrong model.

## Practical recommendation

For reliable work:

1. Ask the user for the Bilibili URL and time range.
2. Try URL/BV only if `yt-dlp` and `ffmpeg` are available.
3. On failure, preserve the error summary and request a local clip.
4. Continue with local-video processing once a clip exists.
5. Render final outputs as `gif_white.gif`, `gif_transparent.gif`, and `webp_transparent.webp` under `<work-dir>/output/`.
6. Wait for long `yt-dlp`/`rembg` commands with a longer tool timeout; do not spend extra turns asking for progress confirmation.
