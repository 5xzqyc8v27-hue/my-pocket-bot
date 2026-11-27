from __future__ import annotations

import tempfile
from pathlib import Path

from .media import run
from .top import has_audio
from .models import Moment, Preset, Transcript
from .subtitles import build_ass


def _even(x: float) -> int:
    return int(x) // 2 * 2


def _cover(w: int, h: int) -> str:
    return f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h}"


def camera_filter(preset: Preset, length: float, peaks: list[float]) -> str | None:
    """Движение камеры по готовому кадру: медленный наезд и резкие наезды на громких местах."""
    k, fps = preset.camera_strength, preset.fps
    terms = []
    if preset.camera in ("push", "both"):
        terms.append(f"{k * 0.6:.4f}*on/{max(1, int(length * fps))}")
    if preset.camera in ("punch", "both"):
        terms += [f"{k:.4f}*max(0,1-abs(on/{fps}-{c:.2f})/0.35)" for c in peaks]
    if not terms:
        return None
    return (f"zoompan=z='1+{'+'.join(terms)}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
            f":d=1:s={preset.width}x{preset.height}:fps={fps}")


IMAGE_EXT = (".png", ".jpg", ".jpeg", ".webp")
AUDIO_EXT = (".mp3", ".wav", ".ogg", ".m4a", ".aac")
PLACE_Y = {"top": "H*0.22-h/2", "center": "(H-h)/2", "bottom": "H*0.78-h/2", "full": "0"}


def insert_filters(inserts: list, first_input: int, preset: Preset, last: str) -> tuple[list[str], str]:
    """Видеочасть вставок: каждая показывается в своё окно времени поверх кадра."""
    parts = []
    for n, ins in enumerate(i for i in inserts if not i.file.lower().endswith(AUDIO_EXT)):
        idx, W, H = first_input + ins.index, preset.width, preset.height
        size = (f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H}" if ins.place == "full"
                else f"scale={int(W * ins.scale) // 2 * 2}:-2")
        parts += [f"[{idx}:v]{size},format=rgba,setpts=PTS-STARTPTS+{ins.at:.3f}/TB[in{n}]",
                  f"[{last}][in{n}]overlay=(W-w)/2:{PLACE_Y.get(ins.place, PLACE_Y['center'])}"
                  f":enable='between(t,{ins.at:.3f},{ins.at + ins.duration:.3f})':eof_action=pass[vi{n}]"]
        last = f"vi{n}"
    return parts, last


def build_filter(preset: Preset, has_filler: bool, has_banner: bool, ass_name: str | None,
                 camera: str | None = None, inserts: list | None = None, first_insert: int = 0) -> str:
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
    if camera:
        parts.append(f"[v]fps={preset.fps},{camera}[vc]")
        last = "vc"
    if has_banner:
        idx = 2 if has_filler else 1
        bw = _even(W * preset.banner_width)
        parts += [f"[{idx}:v]scale={bw}:-2[bn]",
                  f"[{last}][bn]overlay=(W-w)/2:H*{preset.banner_y}-h/2[vb]"]
        last = "vb"
    if inserts:
        more, last = insert_filters(inserts, first_insert, preset, last)
        parts += more
    tail = f"fps={preset.fps},setsar=1"
    if preset.border > 0:
        tail += f",drawbox=0:0:iw:ih:color=0x{preset.border_color}@1:t={preset.border}"
    if ass_name:
        tail += f",ass={ass_name}"
    parts.append(f"[{last}]{tail},format=yuv420p[out]")
    return ";".join(parts)


def _prepare(source: Path, moment: Moment, preset: Preset, transcript: Transcript | None,
             filler: Path | None, tmp: Path, camera: str | None = None,
             inserts: list | None = None) -> list[str]:
    """Общая часть команды ffmpeg для ролика и для кадра предпросмотра."""
    use_filler = bool(filler) and preset.layout == "split"
    banner = Path(preset.banner) if preset.banner else None
    if banner and not banner.exists():
        banner = None
    words = transcript.words_between(moment.start, moment.end) if transcript else []
    ass_name = None
    if (preset.subtitles and words) or (preset.title and moment.title.strip()) or preset.watermark.strip():
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
    first = 1 + int(use_filler) + int(bool(banner))
    for k, ins in enumerate(inserts or []):
        ins.index = k
        if ins.file.lower().endswith(IMAGE_EXT):
            cmd += ["-loop", "1", "-t", f"{ins.duration:.3f}"]
        elif not ins.file.lower().endswith(AUDIO_EXT):
            cmd += ["-t", f"{ins.duration:.3f}"]
        cmd += ["-i", ins.file]
    return cmd + ["-filter_complex", build_filter(preset, use_filler, bool(banner), ass_name, camera,
                                                  inserts, first)]


class _Ins:
    """Вставка с уже найденным файлом и временем в готовом ролике."""

    def __init__(self, file: str, at: float, duration: float, place: str, scale: float):
        self.file, self.at, self.duration, self.place, self.scale, self.index = file, at, duration, place, scale, 0


def render_clip(source: Path, moment: Moment, preset: Preset, out_path: Path,
                transcript: Transcript | None = None, filler: Path | None = None,
                resolve=None) -> Path:
    """Вырезает момент и монтирует его в вертикальный ролик."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_len = moment.duration / preset.speed
    peaks = []
    if preset.camera in ("punch", "both"):
        from .moments import loud_peaks
        peaks = [p / preset.speed for p in loud_peaks(source, moment.start, moment.duration)]
    camera = camera_filter(preset, out_len, peaks)
    inserts = []
    for ins in moment.inserts:
        path = Path(resolve(ins.file)) if resolve else Path(ins.file)
        if path.exists() and 0 <= ins.at < out_len:
            inserts.append(_Ins(str(path.resolve()), ins.at, min(ins.duration, out_len - ins.at), ins.place, ins.scale))
    with tempfile.TemporaryDirectory() as tmp:
        cmd = _prepare(source, moment, preset, transcript, filler, Path(tmp), camera, inserts)
        first = 1 + int(bool(filler) and preset.layout == "split") + int(bool(preset.banner and Path(preset.banner).exists()))
        sounds = [i for i in inserts if not i.file.lower().endswith(IMAGE_EXT) and has_audio(Path(i.file))]
        main_audio = has_audio(source)
        if sounds:  # звук вставок подмешиваем к основной дорожке в нужный момент
            fc = cmd.index("-filter_complex") + 1
            mix = [f"[0:a]atempo={preset.speed}[a0]"] if main_audio else []
            for i in sounds:
                ms = int(i.at * 1000)
                mix.append(f"[{first + i.index}:a]atrim=0:{i.duration:.3f},adelay={ms}:all=1,volume=0.9[s{i.index}]")
            labels = ("[a0]" if main_audio else "") + "".join(f"[s{i.index}]" for i in sounds)
            mix.append(f"{labels}amix=inputs={len(sounds) + int(main_audio)}:normalize=0:duration=longest[aout]")
            cmd[fc] += ";" + ";".join(mix)
            cmd += ["-map", "[out]", "-map", "[aout]", "-t", f"{out_len:.3f}"]
        else:
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
    # Кадр берётся в нулевой момент — анимации появления в нём ещё не видно, отключаем их.
    preset = preset.model_copy(update={"title_anim": False, "subtitle_pop": False})
    with tempfile.TemporaryDirectory() as tmp:
        cmd = _prepare(source, shifted, preset, transcript, filler, Path(tmp))
        idx = cmd.index("-filter_complex") + 1
        cmd[idx] = cmd[idx].replace("[out]", "[full]") + ";[full]scale=540:-2[out]"
        cmd += ["-map", "[out]", "-frames:v", "1", "-q:v", "4", str(out_path.resolve())]
        run(cmd, cwd=Path(tmp))
    return out_path
