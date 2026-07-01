# Transparent GIF and Target Selection

## GIF transparency limits

GIF supports one transparent palette entry, not smooth alpha. Expect hard edges, possible halos, and weaker results on complex backgrounds. If the user needs smooth edges on arbitrary backgrounds, suggest WebP or APNG as an additional output, but still provide GIF when requested.

The script reserves a dedicated transparent palette index when encoding GIFs. Use `output/white_check.png` and `output/checker_sheet.png` to verify the result; if the transparent area appears black there, inspect the RGBA frames before rerunning expensive rembg work.

Final renders write:

- `output/gif_white.gif`: safest for chat apps; no transparent edge.
- `output/gif_transparent.gif` and `output/sticker.gif`: transparent GIF; edges become hard because GIF cannot keep real alpha.
- `output/webp_transparent.webp`: preferred when the destination supports WebP because smooth alpha survives.

The script now encodes `webp_transparent.webp` first and then decodes that WebP to generate the GIF files. This does not remove GIF's transparency limit, but it ensures GIF conversion starts from the same high-quality alpha intermediate.

## Alpha matte workflow

Avoid using a binary mask directly as alpha for final frames. Prefer:

```text
coarse mask -> erode/dilate trimap -> alpha matte -> foreground estimate -> RGBA
```

Use `--alpha-matte pymatting` when a coarse alpha already exists from chromakey, edge removal, SAM/SAM2/Cutie, or rembg. The script builds a trimap from the coarse alpha and uses PyMatting to estimate alpha and foreground colors. Use `--alpha-matte feather` only as a quick fallback.

## Target selection from natural language

Natural language does not directly identify pixels. Convert it into this chain:

```text
user phrase -> TargetHint(subject, position, attributes)
-> candidate boxes -> selected box -> segmentation mask
-> transparent PNG frames -> GIF
```

The bundled script implements `TargetHint` parsing and candidate scoring. It does not bundle large vision models.

Use this fallback order:

1. If `--crop x,y,w,h` is provided, trust it.
2. If a vision backend is installed, run text-prompt detection:
   - Grounding DINO: text prompt -> candidate boxes.
   - Florence-2: phrase grounding or open-vocabulary detection -> candidate boxes.
3. Use SAM/SAM2 with the chosen box to create masks.
4. Track the same subject across frames with SAM2 video mode when available. Otherwise compare boxes by label, overlap, position, and size.
5. If confidence is low or multiple similar candidates exist, generate a preview contact sheet and ask the user to choose.

Always preview before rendering when the target was described in natural language. The preview's `preview/preview_processed.png` must be a processed transparent frame, not just the raw frame.

## Background removal modes

- `chromakey`: fastest and most deterministic. Use for green/blue/solid backgrounds. Tune `--key-color` and `--key-threshold`.
- `edge`: remove a smooth background connected to the frame edges. Good for simple gradients behind stickers; tune `--key-threshold` down if the subject is eaten.
- `auto`: use rembg. Defaults to `--rembg-model auto`; route by `--subject-type` and target hint, then use preview comparison when uncertain.
- `none`: preserve the visible frame. Use for testing, manual masks, or non-transparent GIFs.

## rembg model routing

Use the model pool instead of assuming one default model:

- Effects/anime stickers with aura, bubbles, flowers, or glow: `u2net`, `birefnet-general-lite`, `isnet-anime`, `isnet-general-use`.
- Anime/game characters: `isnet-anime`, `birefnet-general`, `birefnet-massive`, `u2net`.
- Real portraits: `birefnet-portrait`, `birefnet-general`, `u2net_human_seg`, `u2net`.
- General objects: `birefnet-general`, `isnet-general-use`, `u2net`.
- Specified subject in a busy scene: prefer GroundingDINO + SAM2/Cutie for the coarse mask, then PyMatting.

Use `--compare-rembg-models` during preview to write `preview/rembg_model_compare.png`. Pick the model that preserves the requested target, not merely the cleanest-looking mask.

BRIA/RMBG weights can have non-commercial license restrictions. Do not choose `bria-rmbg` for commercial user work unless the user confirms licensing.

## Quality tips

- Keep clips short: 2-4 seconds is usually best for stickers.
- Prefer 8-12 fps and 320-480 px width.
- Keep final renders to 12 frames by default. Increase `--max-frames` only when the user explicitly asks for smoother motion.
- Use `--crop x,y,w,h` for deterministic results when the subject is small.
- Keep intermediate frames until the user approves the result.
- If rembg causes flicker, lower fps or switch to manual crop plus chromakey/manual masks.
- If GIF edges look harsh but WebP looks good, explain the GIF format limit instead of over-tuning the mask.
