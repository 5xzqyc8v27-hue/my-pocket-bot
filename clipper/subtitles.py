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


import re

MAT = re.compile(r"(ху[йяеёию]|пизд|бля|[её]б[аеиоуыл]|[её]бн|сук[аиу]|муда|залуп|пид[оа]р|гандон|шлюх|долбо[её]б)", re.I)


def censor_word(word: str) -> str:
    """Матерное слово -> первая буква и звёздочки, знаки препинания сохраняются."""
    if not MAT.search(word.replace("ё", "е").replace("Ё", "Е")) and not MAT.search(word):
        return word
    core = re.sub(r"[^\w]", "", word)
    return word.replace(core, core[0] + "*" * (len(core) - 1), 1) if len(core) > 1 else word


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


def chat_lines(user: str, text: str, preset: Preset, size: int = 44) -> list[str]:
    """Сообщение чата, разбитое на строки под ширину плашки (не больше трёх строк)."""
    shown = "Зритель" if preset.chat_hide_nick or not user else user
    per_line = max(12, int(preset.width * 0.86 / (size * 0.56)))
    lines = textwrap.wrap(f"{shown}: {text}", per_line)[:3]
    return lines or [shown]


def build_ass(words: list[Word], clip_start: float, clip_len: float, title: str,
              preset: Preset, chat: tuple[str, str] | None = None) -> str:
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
    if chat and preset.layout == "chat":
        rows, size = chat_lines(chat[0], chat[1], preset), 44
        nick_len = rows[0].find(":")
        seam = int(h * preset.webcam_share)
        top = seam - (len(rows) - 1) * size * 0.62
        for n, row in enumerate(rows):
            text = _escape(row)
            if n == 0 and nick_len > 0:
                text = (f"{{\\c{ass_color(preset.chat_nick_color)}&}}{_escape(row[:nick_len])}"
                        f"{{\\c&H00FFFFFF&}}{_escape(row[nick_len:])}")
            lines.append(f"Dialogue: 4,{ass_time(0)},{ass_time(clip_len)},Sub,"
                         f"{{\\an4\\pos({int(w * 0.06)},{int(top + n * size * 1.24)})\\fs{size}\\bord0\\shad0}}{text}")
    if preset.title and title.strip() and not (chat and preset.layout == "chat"):
        max_chars = max(8, int(w * 0.9 / (preset.title_size * 0.6)))
        wrapped = "\\N".join(textwrap.wrap(_escape(title.upper()), max_chars)[:3])
        tags = f"\\pos({w // 2},{int(h * preset.title_y)})"
        if preset.title_anim:
            tags += "\\fad(200,0)\\fscx60\\fscy60\\t(0,250,\\fscx100\\fscy100)"
        span = f"{ass_time(0)},{ass_time(clip_len)}"
        if preset.title_glow:
            glow = ass_color(preset.title_glow)
            lines.append(f"Dialogue: 1,{span},Title,{{{tags}\\1c{glow}&\\3c{glow}&\\bord10\\blur14\\shad0}}{wrapped}")
            tags += f"\\3c{glow}&\\bord2\\blur1\\shad0"
        lines.append(f"Dialogue: 2,{span},Title,{{{tags}}}{wrapped}")
    if preset.watermark.strip():
        lines.append(f"Dialogue: 3,{ass_time(0)},{ass_time(clip_len)},Sub,"
                     f"{{\\pos({w // 2},{int(h * preset.watermark_y)})\\fs30\\bord2\\alpha&H50&}}{_escape(preset.watermark)}")

    if preset.subtitles:
        pop = "\\fscx82\\fscy82\\t(0,90,\\fscx100\\fscy100)" if preset.subtitle_pop else ""
        pos = f"{{\\pos({w // 2},{int(h * preset.subtitle_y)}){pop}}}"
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
                # «ааааааааа» на всю ширину кадра сжимаем до трёх букв
                shown = [re.sub(r"(.)\1{3,}", r"\1\1\1", censor_word(g.text) if preset.censor else g.text) for g in group]
                texts = [_escape(t.upper() if preset.subtitle_upper else t) for t in shown]
                parts = [f"{{\\c{hi}&}}{t}{{\\c{base}&}}" if k == j else t for k, t in enumerate(texts)]
                lines.append(f"Dialogue: 0,{ass_time(start)},{ass_time(end)},Sub,{pos}{' '.join(parts)}")
    return out + "\n".join(lines) + "\n"
