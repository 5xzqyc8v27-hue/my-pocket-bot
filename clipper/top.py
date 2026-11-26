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
LIST_TOP, LIST_STEP = 0.235, 0.043


def has_audio(path: Path) -> bool:
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries",
                          "stream=index", "-of", "csv=p=0", str(path)], capture_output=True, text=True).stdout
    return bool(out.strip())


def _even(x: float) -> int:
    return int(x) // 2 * 2


def _title_lines(title: str, width: int, size: int) -> list[str]:
    """Заголовок в 2-3 строки, слова раскрашены как в популярных нарезках."""
    lines = textwrap.wrap(title.upper(), max(8, int(width * 0.92 / (size * 0.62))))[:3]
    out = []
    for i, line in enumerate(lines):
        first, _, rest = line.partition(" ")
        rest_color = LAST_REST if i == len(lines) - 1 and len(lines) > 1 else "FFFFFF"
        out.append(f"{{\\c{ass_color(LINE_COLORS[i % 3])}&}}{_escape(first)}"
                   f"{{\\c{ass_color(rest_color)}&}} {_escape(rest)}")
    return out


def build_top_ass(title: str, labels: list[str], starts: list[float], total: float, preset: Preset) -> str:
    """labels[k] — подпись места k+1; starts[k] — когда это место начинает играть."""
    w, h = preset.width, preset.height
    size = 66
    out = HEADER.format(w=w, h=h, font=preset.font, sub_size=44, sub_color=ass_color("FFFFFF"),
                        sub_outline=4, title_size=size, title_color=ass_color("FFFFFF"), title_outline=5)
    span = f"{ass_time(0)},{ass_time(total)}"
    lines = []
    for i, line in enumerate(_title_lines(title, w, size)):
        lines.append(f"Dialogue: 2,{span},Title,{{\\pos({w // 2},{int(h * 0.045 + i * size * 1.12 + size / 2)})}}{line}")
    for k, label in enumerate(labels):
        y = int(h * (LIST_TOP + k * LIST_STEP))
        lines.append(f"Dialogue: 2,{span},Sub,{{\\an4\\pos({int(w * 0.07)},{y})}}{k + 1}.")
        lines.append(f"Dialogue: 2,{ass_time(starts[k])},{ass_time(total)},Sub,"
                     f"{{\\an4\\pos({int(w * 0.18)},{y})\\c{ass_color('3DDC4A')}&\\fad(200,0)}}{_escape(label)}")
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
    box_top = _even(H * (LIST_TOP + n * LIST_STEP))
    box_bottom = _even(H * (0.82 if photo else 0.96))
    bw, bh = _even(W * 0.82), box_bottom - box_top
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
                       f"[fb{j}]scale={bw}:{bh - cam_h}:force_original_aspect_ratio=increase,crop={bw}:{bh - cam_h}[cb{j}];"
                       f"[ca{j}][cb{j}]vstack[c{j}]")
        else:
            content = f"[f{j}]scale={bw}:{bh}:force_original_aspect_ratio=decrease[c{j}]"
        parts += [
            f"[{j}:v]split=2[b{j}][f{j}]",
            f"[b{j}]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},boxblur=30:2[bg{j}]",
            content,
            f"[bg{j}][c{j}]overlay=(W-w)/2:{box_top}+({bh}-h)/2,fps={preset.fps},setsar=1[s{j}]",
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
