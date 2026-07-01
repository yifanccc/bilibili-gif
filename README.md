# bilibili-gif

`bilibili-gif` 是一个 Codex Skill，也可以作为独立 Python 脚本使用。它用于把 Bilibili 视频或本地视频片段转成适合表情包使用的 GIF，并支持预览、裁剪、抠图、透明背景和多格式输出。

适合这些场景：

- 从 B 站视频里截取某个时间段。
- 先快速生成 1 帧预览，确认时间范围和裁剪区域。
- 按自然语言目标选择画面，例如“左上角两个 Q 版人物”。
- 用 `rembg`、色键、边缘移除或 alpha matte 做抠图。
- 输出微信可用的 GIF 表情。

## 输出内容

完整渲染后，文件会写入 `<work-dir>/output/`：

- `sticker.gif`：默认透明 GIF。
- `gif_transparent.gif`：透明 GIF，和 `sticker.gif` 一致，只是名字更明确。
- `gif_white.gif`：白底 GIF，兼容性最好，边缘也最稳定。
- `webp_transparent.webp`：透明 WebP 中间文件，质量最好。
- `checker_sheet.png`：棋盘格检查图，用来检查透明边缘。
- `white_check.png`：白底检查图。

当前流程会先生成 `webp_transparent.webp`，再从这个 WebP 生成 GIF。这样可以让 GIF 转换从高质量 alpha 中间文件开始。不过 GIF 本身只支持二值透明，透明边缘仍然会比 WebP 硬。

## 依赖安装

推荐一次装齐：

```bash
brew install ffmpeg
python -m pip install -U pillow yt-dlp "rembg[cpu]" pymatting numpy scipy onnxruntime
```

如果只处理本地视频且不自动抠图，`ffmpeg` + `pillow` 就够；其他功能按需使用：

- `yt-dlp`：处理 Bilibili URL/BV 输入时需要。
- `rembg[cpu]` + `onnxruntime`：使用 `--mode auto` 自动抠图时需要。
- `pymatting` + `numpy` + `scipy`：使用 `--alpha-matte pymatting` 优化边缘时需要。
- Bilibili 有时需要登录态，可以使用 `--cookies-from-browser chrome`。
- 非 macOS 环境用系统包管理器安装 `ffmpeg`。

## 作为 Codex Skill 安装

克隆到 Codex skills 目录：

```bash
mkdir -p ~/.codex/skills
git clone https://github.com/yifanccc/bilibili-gif.git ~/.codex/skills/bilibili-gif
```

然后可以在 Codex 里这样使用：

```text
使用 $bilibili-gif 生成 https://www.bilibili.com/video/BV... 中 00:10-00:13 中间弹出的角色 gif
```

## 作为独立脚本使用

查看参数：

```bash
python scripts/bili_gif.py --help
```

### 第一步：生成预览

默认就是预览模式，不会生成最终 GIF。它会下载/抽帧，生成时间轴预览图，并处理 1 张透明预览帧。

```bash
python scripts/bili_gif.py "https://www.bilibili.com/video/BV..." \
  --start 00:00:10 --end 00:00:13 \
  --cookies-from-browser chrome \
  --target "中间弹出的角色，包含文字气泡和动效线" \
  --crop 0,0,1280,720 \
  --mode auto --rembg-model auto --subject-type effects \
  --compare-rembg-models \
  --width 560 \
  --work-dir ./example-work
```

重点检查：

- `example-work/preview/timeline_contact_sheet.png`
- `example-work/preview/preview_processed.png`
- `example-work/preview/rembg_model_compare.png`

如果时间范围不准，先改 `--start` / `--end`；如果目标范围不准，先改 `--crop x,y,w,h`。

### 第二步：完整渲染

确认预览无误后，加 `--render` 生成最终文件：

```bash
python scripts/bili_gif.py ./example-work/video/source.mp4 \
  --start 0 --end 3.25 \
  --target "中间弹出的角色，包含文字气泡和动效线" \
  --crop 0,0,1280,720 \
  --mode auto --rembg-model auto --subject-type effects \
  --rembg-alpha-matting --rembg-post-process-mask \
  --alpha-matte pymatting \
  --render --max-frames 12 \
  --width 560 \
  --work-dir ./example-work
```

默认最多处理 12 帧，避免 `rembg` 太慢。如果确实需要更流畅，可以显式提高 `--max-frames`。

## 工作目录结构

```text
<work-dir>/
  video/        # 视频源
  frames_raw/   # 原始帧
  frames_rgba/  # 抠图后的透明帧
  inspect/      # 可选，人工复核用的拼图和解码帧检查
  preview/      # 预览帧、时间轴、模型对比
  output/       # 最终 GIF/WebP 和检查图
```

## 通用优化流程

遇到边缘粗糙、黑块、残留碎片或动作不明显时，按层定位，不要直接重跑全流程：

1. 先看 `frames_rgba/`，确认抠图后的 PNG 帧是否干净。
2. 再看 `webp_transparent.webp`，确认高质量透明中间文件是否干净。
3. 最后把 `sticker.gif` 解码成帧检查，确认 GIF 编码没有带入黑块或残留。
4. 如果是独立的非目标碎片，用 alpha 连通域做清理；不要直接用矩形擦除，容易误伤头发、气泡、动效线。
5. 清理后重新生成 WebP，再从 WebP 生成 GIF，并复查 `checker_sheet.png`。

如果要保留“弹出来”的动态感觉，时间范围要包含出现前后的缓冲帧；需要更顺滑时再显式提高 `--max-frames`。

## 抠图模式

- `--mode auto`：使用 `rembg`，支持 `--rembg-model auto` 自动路由模型。
- `--mode chromakey`：适合绿幕、纯色背景。
- `--mode edge`：适合从画面边缘连通的简单背景。
- `--mode none`：不做抠图，只做抽帧/格式转换。

遇到复杂画面时，建议先加：

```bash
--compare-rembg-models
```

它会输出模型对比图，方便选择“最能保留目标主体”的模型，而不是盲目用默认模型。

## 注意事项

- 微信表情通常使用 GIF，所以工具会输出 `gif_transparent.gif` 和 `gif_white.gif`。
- GIF 只有一个透明色，不支持真正的半透明 alpha，所以透明边缘可能比 WebP 硬。
- Bilibili 下载是 best-effort，可能因为登录、地区、版权或反爬限制失败。
- 只处理你有权使用的视频内容。
- BRIA/RMBG 模型权重可能有非商业授权限制，商业用途前需要确认授权。
