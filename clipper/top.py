"""Формат «ТОП N моментов»: несколько моментов подряд со списком, который заполняется по ходу."""
from __future__ import annotations

import subprocess
import tempfile
import textwrap
from pathlib import Path

from .media import run
from .models import Moment, Preset
from .subtitles import HEADER, _escape, ass_color, ass_time

LINE_COLORS = ["3DDC4A", "FFD400", "FF3DD4"]  # цвет первого слова в каждой строке заголовка
LAST_REST = "35D0FF"  # цвет остатка последней строки
LIST_TOP, LIST_STEP = 0.235, 0.046
TITLE_SIZE, LIST_SIZE = 80, 58


def has_audio(path: Path) -> bool:
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries",
                          "stream=index", "-of", "csv=p=0", str(path)], capture_output=True, text=True).stdout
    return bool(out.strip())


def _even(x: float) -> int:
    return int(x) // 2 * 2


def _title_lines(title: str, width: int, size: int) -> list[str]:
    """Заголовок в 2-3 строки, слова раскрашены как в популярных нарезках."""
    lines = textwrap.wrap(title.upper(), max(8, int(width * 0.92 / (size * 0.74))))[:3]
    out = []
    for i, line in enumerate(lines):
        first, _, rest = line.partition(" ")
        rest_color = LAST_REST if i == len(lines) - 1 and len(lines) > 1 else "FFFFFF"
        out.append(f"{{\\c{ass_color(LINE_COLORS[i % 3])}&}}{_escape(first)}"
                   f"{{\\c{ass_color(rest_color)}&}} {_escape(rest)}")
    return out


def list_top(title: str, width: int, height: int) -> float:
    """Где начинается список (доля высоты): сразу под заголовком, сколько бы строк он ни занял."""
    rows = len(_title_lines(title, width, TITLE_SIZE))
    return (height * 0.04 + rows * TITLE_SIZE * 1.14 + 46) / height


def build_top_ass(title: str, labels: list[str], starts: list[float], total: float, preset: Preset) -> str:
    """labels[k] — подпись места k+1; starts[k] — когда это место начинает играть."""
    w, h = preset.width, preset.height
    size, top = TITLE_SIZE, list_top(title, preset.width, preset.height)
    out = HEADER.format(box_color="&H00000000", box_pad=8, w=w, h=h, font=preset.font, sub_size=LIST_SIZE,
                        sub_color=ass_color("FFFFFF"), sub_outline=5, title_size=size,
                        title_color=ass_color("FFFFFF"), title_outline=6)
    span = f"{ass_time(0)},{ass_time(total)}"
    lines = []
    for i, line in enumerate(_title_lines(title, w, size)):
        lines.append(f"Dialogue: 2,{span},Title,{{\\pos({w // 2},{int(h * 0.04 + i * size * 1.14 + size / 2)})}}{line}")
    for k, label in enumerate(labels):
        y = int(h * (top + k * LIST_STEP) + LIST_SIZE / 2)
        lines.append(f"Dialogue: 2,{span},Sub,{{\\an4\\pos({int(w * 0.06)},{y})}}{k + 1}.")
        lines.append(f"Dialogue: 2,{ass_time(starts[k])},{ass_time(total)},Sub,"
                     f"{{\\an4\\pos({int(w * 0.16)},{y})\\c{ass_color('3DDC4A')}&\\fad(200,0)"
                     f"\\fscx70\\fscy70\\t(0,220,\\fscx100\\fscy100)}}{_escape(label)}")
    if preset.watermark.strip():
        lines.append(f"Dialogue: 3,{span},Sub,{{\\pos({w // 2},{int(h * 0.985)})\\fs26\\alpha&H60&}}{_escape(preset.watermark)}")
    return out + "\n".join(lines) + "\n"


def render_top(source: Path, moments: list[Moment], title: str, preset: Preset, out_path: Path,
               photo: Path | None = None, fonts_dir: Path | None = None) -> Path:
    """moments — от лучшего к худшему; играют в обратном порядке, лучший в конце."""
    if not 2 <= len(moments) <= 10:
        raise ValueError("Для топа нужно от 2 до 10 моментов")
    W, H, n = preset.width, preset.height, len(moments)
    order = list(range(n - 1, -1, -1))  # сначала последнее место
    box_top = _even(H * (list_top(title, W, H) + n * LIST_STEP) + 24)
    box_bottom = _even(H * (0.82 if photo else 0.965))
    bw, bh = _even(W * 0.94), box_bottom - box_top
    audio = has_audio(source)

    cmd = ["ffmpeg", "-y", "-v", "error"]
    parts, starts, t = [], [0.0] * n, 0.0
    for j, k in enumerate(order):
        m = moments[k]
        cmd += ["-ss", f"{m.start:.3f}", "-t", f"{m.duration:.3f}", "-i", str(source.resolve())]
        starts[k], t = t, t + m.duration
        if preset.layout in ("reaction", "neon"):
            x, y, w, h = preset.webcam
            cam_h = _even(bh * 0.46)
            content = (f"[f{j}]split=2[fa{j}][fb{j}];"
                       f"[fa{j}]crop=iw*{w}:ih*{h}:iw*{x}:ih*{y},scale={bw}:{cam_h}:force_original_aspect_ratio=increase,crop={bw}:{cam_h}[ca{j}];"
                       f"[fb{j}]scale={bw}:{bh - cam_h}:force_original_aspect_ratio=increase,"
                       f"crop={bw}:{bh - cam_h}:(iw-{bw})*{1.0 if x + w / 2 < 0.35 else 0.0 if x + w / 2 > 0.65 else 0.5}:(ih-{bh - cam_h})/2[cb{j}];"
                       f"[ca{j}][cb{j}]vstack[c{j}]")
        else:
            zoom = max(1.0, preset.content_zoom)  # крупнее кадр: края горизонтального видео обрезаются
            content = (f"[f{j}]scale=-2:{_even(min(bh - 10, (bw - 10) * 9 / 16 * zoom))},"
                       f"crop='min(iw,{bw - 10})':ih[c{j}]" if zoom > 1.0 else
                       f"[f{j}]scale={bw - 10}:{bh - 10}:force_original_aspect_ratio=decrease[c{j}]")
        parts += [
            f"[{j}:v]split=2[b{j}][f{j}]",
            f"[b{j}]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},boxblur=30:2,"
            f"eq=brightness=-0.16:saturation=1.15[bg{j}]",
            content,
            f"[c{j}]pad=iw+10:ih+10:5:5:white[cf{j}]",  # белая рамка вокруг кадра
            f"[bg{j}][cf{j}]overlay=(W-w)/2:{box_top}+({bh}-h)/4,fps={preset.fps},setsar=1[s{j}]",
        ]
    pads = "".join(f"[s{j}]" + (f"[{j}:a]" if audio else "") for j in range(n))
    parts.append(f"{pads}concat=n={n}:v=1:a={1 if audio else 0}[cv]" + ("[ca]" if audio else ""))
    last = "cv"
    if photo:
        cmd += ["-i", str(Path(photo).resolve())]
        ph = _even(H * 0.14)
        parts += [f"[{n}:v]scale=-2:{ph},pad=iw+8:ih+8:4:4:white[ph]",
                  f"[{last}][ph]overlay=(W-w)/2:{int(H * 0.905)}-h/2[cp]"]
        last = "cp"

    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "top.ass").write_text(
            build_top_ass(title, [m.title or f"Момент {i + 1}" for i, m in enumerate(moments)], starts, t, preset),
            encoding="utf-8")
        ass = "ass=top.ass" + (f":fontsdir={_link_fonts(fonts_dir, Path(tmp))}" if fonts_dir else "")
        parts.append(f"[{last}]{ass},format=yuv420p[out]")
        cmd += ["-filter_complex", ";".join(parts), "-map", "[out]"]
        if audio:
            cmd += ["-map", "[ca]", "-c:a", "aac", "-b:a", "160k"]
        out_path.parent.mkdir(parents=True, exist_ok=True)
        cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-movflags", "+faststart",
                str(out_path.resolve())]
        run(cmd, cwd=Path(tmp))
    return out_path


def _link_fonts(fonts_dir: Path, tmp: Path) -> str:
    """Кладём шрифты рядом с субтитрами под простым именем — без экранирования путей."""
    target = tmp / "fonts"
    target.mkdir(exist_ok=True)
    if fonts_dir and fonts_dir.is_dir():
        for f in fonts_dir.iterdir():
            if f.suffix.lower() in (".ttf", ".otf"):
                (target / f.name).write_bytes(f.read_bytes())
    return "fonts"


def join_pieces(pieces: list[tuple[Path, float, float]], out: Path) -> list[tuple[float, float]]:
    """Куски из разных видео -> один файл 1920x1080. Возвращает границы каждого куска в нём."""
    cmd, parts, spans, t = ["ffmpeg", "-y", "-v", "error"], [], [], 0.0
    for j, (path, start, end) in enumerate(pieces):
        cmd += ["-ss", f"{start:.3f}", "-t", f"{end - start:.3f}", "-i", str(Path(path).resolve())]
        parts.append(f"[{j}:v]scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2,"
                     f"fps=30,setsar=1,format=yuv420p[v{j}]")
        parts.append(f"[{j}:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo[a{j}]" if has_audio(path)
                     else f"anullsrc=r=48000:cl=stereo,atrim=0:{end - start:.3f}[a{j}]")
        spans.append((round(t, 3), round(t + end - start, 3)))
        t += end - start
    pads = "".join(f"[v{j}][a{j}]" for j in range(len(pieces)))
    parts.append(f"{pads}concat=n={len(pieces)}:v=1:a=1[v][a]")
    out.parent.mkdir(parents=True, exist_ok=True)
    run(cmd + ["-filter_complex", ";".join(parts), "-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-preset", "veryfast",
               "-crf", "18", "-c:a", "aac", "-b:a", "160k", str(out)])
    return spans


def best_window(video: Path, length: float, want: float = 12.0) -> tuple[float, float]:
    """Самый сильный отрезок клипа: вокруг самого громкого места, с запасом на подводку."""
    from .moments import loud_peaks
    if length <= want + 1:
        return 0.0, length
    peaks = loud_peaks(video, 0, length, count=1)
    peak = peaks[0] if peaks else length / 2
    start = max(0.0, min(length - want, peak - want * 0.62))
    return round(start, 2), round(start + want, 2)
