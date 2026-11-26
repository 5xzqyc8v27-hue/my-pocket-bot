from __future__ import annotations

import tempfile
from pathlib import Path

from .media import run
from .models import Moment, Preset, Transcript
from .subtitles import build_ass


def _even(x: float) -> int:
    return int(x) // 2 * 2


def _cover(w: int, h: int) -> str:
    return f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h}"


def build_filter(preset: Preset, has_filler: bool, has_banner: bool, ass_name: str | None) -> str:
    """Собирает filter_complex под раскладку. Входы: 0 — исходник, 1 — залипалка, далее баннер."""
    W, H = preset.width, preset.height
    layout = preset.layout
    if layout == "split" and not has_filler:
        layout = "center"  # без залипалки сплит не собрать
    zoom_w = _even(W * preset.content_zoom)
    blur = f"boxblur={preset.background_blur}:2"
    parts: list[str] = []

    if layout in ("center", "square"):
        fg = (f"scale={zoom_w}:-2" if layout == "center"
              else f"crop='min(iw,ih)':'min(iw,ih)',scale={zoom_w}:{zoom_w}")
        parts += [
            "[0:v]split=2[a][b]",
            f"[a]{_cover(W, H)},{blur}[bg]",
            f"[b]{fg}[fg]",
            "[bg][fg]overlay=(W-w)/2:(H-h)/2[v]",
        ]
    elif layout == "split":
        top = _even(H * preset.split_share)
        parts += [
            f"[0:v]{_cover(W, top)}[top]",
            f"[1:v]{_cover(W, H - top)},setsar=1[bot]",
            "[top][bot]vstack[v]",
        ]
    elif layout == "reaction":
        cam_h = _even(H * preset.webcam_share)
        x, y, w, h = preset.webcam
        cam = f"crop=iw*{w}:ih*{h}:iw*{x}:ih*{y},{_cover(W, cam_h)}"
        game = f"scale=iw*{preset.content_zoom}:-2,{_cover(W, H - cam_h)}"
        order = "[cam][game]" if preset.webcam_position == "top" else "[game][cam]"
        parts += ["[0:v]split=2[a][b]", f"[a]{cam}[cam]", f"[b]{game}[game]", f"{order}vstack[v]"]
    else:
        raise ValueError(f"Неизвестная раскладка: {preset.layout}")

    last = "v"
    if has_banner:
        idx = 2 if has_filler else 1
        bw = _even(W * preset.banner_width)
        parts += [f"[{idx}:v]scale={bw}:-2[bn]",
                  f"[{last}][bn]overlay=(W-w)/2:H*{preset.banner_y}-h/2[vb]"]
        last = "vb"
    tail = f"fps={preset.fps},setsar=1"
    if ass_name:
        tail += f",ass={ass_name}"
    parts.append(f"[{last}]{tail},format=yuv420p[out]")
    return ";".join(parts)


def render_clip(source: Path, moment: Moment, preset: Preset, out_path: Path,
                transcript: Transcript | None = None, filler: Path | None = None) -> Path:
    """Вырезает момент и монтирует его в вертикальный ролик."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    use_filler = bool(filler) and preset.layout == "split"
    banner = Path(preset.banner) if preset.banner else None
    words = transcript.words_between(moment.start, moment.end) if transcript else []
    need_ass = (preset.subtitles and words) or (preset.title and moment.title.strip())

    with tempfile.TemporaryDirectory() as tmp:
        ass_name = None
        if need_ass:
            ass_name = "subs.ass"
            (Path(tmp) / ass_name).write_text(
                build_ass(words, moment.start, moment.duration, moment.title, preset), encoding="utf-8")
        cmd = ["ffmpeg", "-y", "-v", "error", "-ss", f"{moment.start:.3f}", "-t", f"{moment.duration:.3f}",
               "-i", str(source.resolve())]
        if use_filler:
            cmd += ["-stream_loop", "-1", "-i", str(Path(filler).resolve())]
        if banner:
            cmd += ["-i", str(banner.resolve())]
        cmd += ["-filter_complex", build_filter(preset, use_filler, bool(banner), ass_name),
                "-map", "[out]", "-map", "0:a?", "-t", f"{moment.duration:.3f}",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(out_path.resolve())]
        run(cmd, cwd=Path(tmp))  # cwd = tmp, чтобы не экранировать путь к субтитрам
    return out_path
