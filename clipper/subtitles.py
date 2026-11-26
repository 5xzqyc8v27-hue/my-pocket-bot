from __future__ import annotations

import textwrap

from .models import Preset, Word

HEADER = """[Script Info]
ScriptType: v4.00+
PlayResX: {w}
PlayResY: {h}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, BackColour, Bold, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV
Style: Sub,{font},{sub_size},{sub_color},&H00000000,&H80000000,1,1,{sub_outline},2,5,40,40,40
Style: Title,{font},{title_size},{title_color},&H00000000,&H80000000,1,1,{title_outline},2,5,40,40,40

[Events]
Format: Layer, Start, End, Style, Text
"""


def ass_color(rgb: str) -> str:
    """RRGGBB -> формат ASS (&H00BBGGRR)."""
    rgb = rgb.lstrip("#")
    return f"&H00{rgb[4:6]}{rgb[2:4]}{rgb[0:2]}".upper()


def ass_time(t: float) -> str:
    t = max(0.0, t)
    cs = int(round(t * 100))
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def _escape(text: str) -> str:
    return text.replace("\\", "").replace("{", "(").replace("}", ")").replace("\n", " ")


def build_ass(words: list[Word], clip_start: float, clip_len: float, title: str,
              preset: Preset) -> str:
    """Собирает ASS: надпись-хук на весь ролик и субтитры с подсветкой текущего слова."""
    w, h = preset.width, preset.height
    speed = preset.speed or 1.0
    out = HEADER.format(
        w=w, h=h, font=preset.font,
        sub_size=preset.subtitle_size, sub_color=ass_color(preset.subtitle_color),
        sub_outline=max(3, preset.subtitle_size // 14),
        title_size=preset.title_size, title_color=ass_color(preset.title_color),
        title_outline=max(3, preset.title_size // 14),
    )
    lines: list[str] = []
    if preset.title and title.strip():
        max_chars = max(8, int(w * 0.9 / (preset.title_size * 0.6)))
        wrapped = "\\N".join(textwrap.wrap(_escape(title.upper()), max_chars)[:3])
        pos = f"{{\\pos({w // 2},{int(h * preset.title_y)})}}"
        lines.append(f"Dialogue: 1,{ass_time(0)},{ass_time(clip_len)},Title,{pos}{wrapped}")

    if preset.subtitles:
        pos = f"{{\\pos({w // 2},{int(h * preset.subtitle_y)})}}"
        hi, base = ass_color(preset.subtitle_highlight), ass_color(preset.subtitle_color)
        n = max(1, preset.subtitle_words)
        for i in range(0, len(words), n):
            group = words[i:i + n]
            for j, word in enumerate(group):
                start = (word.start - clip_start) / speed
                end = ((group[j + 1].start if j + 1 < len(group) else word.end) - clip_start) / speed
                start, end = max(0.0, start), min(clip_len, end)
                if end - start < 0.02:
                    continue
                texts = [_escape(g.text.upper() if preset.subtitle_upper else g.text) for g in group]
                parts = [f"{{\\c{hi}&}}{t}{{\\c{base}&}}" if k == j else t for k, t in enumerate(texts)]
                lines.append(f"Dialogue: 0,{ass_time(start)},{ass_time(end)},Sub,{pos}{' '.join(parts)}")
    return out + "\n".join(lines) + "\n"
