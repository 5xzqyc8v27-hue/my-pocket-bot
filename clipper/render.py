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
    pre = []
    if preset.mirror:
        pre.append("hflip")
    if (preset.brightness, preset.contrast, preset.saturation) != (0.0, 1.0, 1.0):
        pre.append(f"eq=brightness={preset.brightness}:contrast={preset.contrast}:saturation={preset.saturation}")
    if preset.speed != 1.0:
        pre.append(f"setpts=PTS/{preset.speed}")
    parts: list[str] = [f"[0:v]{','.join(pre) or 'null'}[src]"]

    if layout in ("center", "square"):
        fg = (f"scale={zoom_w}:-2" if layout == "center"
              else f"crop='min(iw,ih)':'min(iw,ih)',scale={zoom_w}:{zoom_w}")
        parts += [
            "[src]split=2[a][b]",
            f"[a]{_cover(W, H)},{blur}[bg]",
            f"[b]{fg}[fg]",
            "[bg][fg]overlay=(W-w)/2:(H-h)/2[v]",
        ]
    elif layout == "split":
        top = _even(H * preset.split_share)
        parts += [
            f"[src]{_cover(W, top)}[top]",
            f"[1:v]{_cover(W, H - top)},setsar=1[bot]",
            "[top][bot]vstack[v]",
        ]
    elif layout == "reaction":
        cam_h = _even(H * preset.webcam_share)
        x, y, w, h = preset.webcam
        cam = f"crop=iw*{w}:ih*{h}:iw*{x}:ih*{y},{_cover(W, cam_h)}"
        game = f"scale=iw*{preset.content_zoom}:-2,{_cover(W, H - cam_h)}"
        order = "[cam][game]" if preset.webcam_position == "top" else "[game][cam]"
        parts += ["[src]split=2[a][b]", f"[a]{cam}[cam]", f"[b]{game}[game]", f"{order}vstack[v]"]
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
    if preset.border > 0:
        tail += f",drawbox=0:0:iw:ih:color=0x{preset.border_color}@1:t={preset.border}"
    if ass_name:
        tail += f",ass={ass_name}"
    parts.append(f"[{last}]{tail},format=yuv420p[out]")
    return ";".join(parts)


def _prepare(source: Path, moment: Moment, preset: Preset, transcript: Transcript | None,
             filler: Path | None, tmp: Path, seek: float = 0.0) -> list[str]:
    """Общая часть команды ffmpeg для ролика и для кадра предпросмотра."""
    use_filler = bool(filler) and preset.layout == "split"
    banner = Path(preset.banner) if preset.banner else None
    if banner and not banner.exists():
        banner = None
    words = transcript.words_between(moment.start, moment.end) if transcript else []
    ass_name = None
    if (preset.subtitles and words) or (preset.title and moment.title.strip()):
        ass_name = "subs.ass"
        (tmp / ass_name).write_text(
            build_ass(words, moment.start, moment.duration / preset.speed, moment.title, preset),
            encoding="utf-8")
    cmd = ["ffmpeg", "-y", "-v", "error", "-ss", f"{moment.start:.3f}", "-t", f"{moment.duration:.3f}",
           "-i", str(source.resolve())]
    if use_filler:
        cmd += ["-stream_loop", "-1", "-i", str(Path(filler).resolve())]
    if banner:
        cmd += ["-i", str(banner.resolve())]
    return cmd + ["-filter_complex", build_filter(preset, use_filler, bool(banner), ass_name)]


def render_clip(source: Path, moment: Moment, preset: Preset, out_path: Path,
                transcript: Transcript | None = None, filler: Path | None = None) -> Path:
    """Вырезает момент и монтирует его в вертикальный ролик."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_len = moment.duration / preset.speed
    with tempfile.TemporaryDirectory() as tmp:
        cmd = _prepare(source, moment, preset, transcript, filler, Path(tmp))
        cmd += ["-map", "[out]", "-map", "0:a?", "-t", f"{out_len:.3f}"]
        if preset.speed != 1.0:
            cmd += ["-af", f"atempo={preset.speed}"]
        cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(out_path.resolve())]
        run(cmd, cwd=Path(tmp))  # cwd = tmp, чтобы не экранировать путь к субтитрам
    return out_path


def render_frame(source: Path, moment: Moment, preset: Preset, out_path: Path,
                 transcript: Transcript | None = None, filler: Path | None = None,
                 at: float = 0.5) -> Path:
    """Один кадр ролика (доля at от длины) — для живого предпросмотра раскладки."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # Прыгаем сразу к нужному месту исходника, чтобы не декодировать весь отрезок.
    shifted = moment.model_copy(update={"start": moment.start + max(0.0, min(0.98, at)) * moment.duration})
    with tempfile.TemporaryDirectory() as tmp:
        cmd = _prepare(source, shifted, preset, transcript, filler, Path(tmp))
        idx = cmd.index("-filter_complex") + 1
        cmd[idx] = cmd[idx].replace("[out]", "[full]") + ";[full]scale=540:-2[out]"
        cmd += ["-map", "[out]", "-frames:v", "1", "-q:v", "4", str(out_path.resolve())]
        run(cmd, cwd=Path(tmp))
    return out_path
