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
Style: TitleBox,{font},{title_size},{title_color},{box_color},{box_color},1,3,{box_pad},0,5,40,40,40

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


def title_lines(title: str, max_chars: int, color: str, highlight: str, upper: bool = True,
                shown: int | None = None, first_color: str = "") -> list[str]:
    """Хук в 1–3 строки; слова между звёздочками красятся в цвет выделения.

    shown — сколько первых слов видно (остальные занимают место, но прозрачны): так хук «печатается».
    """
    words, hot = [], False
    for raw in (title.upper() if upper else title).split():
        hot = hot or raw.startswith("*")
        word = raw.strip("*")
        if word:
            words.append((word, hot))
        if raw.endswith("*") and (word or len(raw) == 1 and hot):
            hot = False
    rows, row, size = [], [], 0
    for word, mark in words:
        if row and size + len(word) + 1 > max_chars:
            rows.append(row)
            row, size = [], 0
        row.append((word, mark))
        size += len(word) + 1
    if row:
        rows.append(row)
    base, hi = ass_color(color), ass_color(highlight)
    out, k = [], 0
    for n, r in enumerate(rows[:3]):
        if first_color:  # первая строка своим цветом, выделение работает в остальных
            base = ass_color(first_color) if n == 0 else ass_color(color)
            r = [(word, mark and n > 0) for word, mark in r]
        parts = []
        for word, mark in r:
            hidden = "\\alpha&HFF&" if shown is not None and k >= shown else "\\alpha&H00&"
            parts.append(f"{{\\c{hi if mark else base}&{hidden}}}{_escape(word)}")
            k += 1
        out.append(" ".join(parts))
    return out


def line_chars(preset: Preset) -> int:
    """Сколько знаков субтитров помещается в строку кадра при заданном размере шрифта."""
    return max(8, int(preset.width * 0.9 / (preset.subtitle_size * 0.6)))


def word_groups(words: list[Word], size: int, max_chars: int = 0) -> list[list[Word]]:
    """Слова по группам до size штук; группа не переходит через конец фразы и через паузу.

    max_chars — сколько знаков помещается в строку: длинные слова уходят в следующую группу."""
    groups, cur = [], []
    words = [w for w in words if w.text.strip("-–— ")]  # одиночные тире из диалогов в субтитры не идут
    for w in words:
        wide = max_chars and sum(len(x.text) + 1 for x in cur) + len(w.text) > max_chars
        if cur and (len(cur) >= size or wide or cur[-1].text.rstrip()[-1:] in ".!?…" or w.start - cur[-1].end > 0.6):
            groups.append(cur)
            cur = []
        cur.append(w)
    if cur:
        groups.append(cur)
    return groups


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
        box_color=ass_color(preset.title_box or "000000"), box_pad=max(8, preset.title_size // 5),
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
        tags = f"\\pos({w // 2},{int(h * preset.title_y)})"
        if preset.title_anim and not preset.title_typed:
            tags += "\\fad(200,0)\\fscx60\\fscy60\\t(0,250,\\fscx100\\fscy100)"
        until = min(clip_len, preset.title_seconds) if preset.title_seconds > 0 else clip_len
        total = len(title.split())
        # «печать»: каждые 0.13 с открывается ещё одно слово, затем хук стоит целиком
        steps = [(i * 0.13, (i + 1) * 0.13, i + 1) for i in range(total - 1)] if preset.title_typed else []
        steps.append((steps[-1][1] if steps else 0.0, until, None))
        for a, b, shown in steps:
            if b <= a:
                continue
            wrapped = "\\N".join(title_lines(title, max_chars, preset.title_color, preset.title_highlight,
                                             preset.title_upper, shown, preset.title_first_color))
            span = f"{ass_time(a)},{ass_time(b)}"
            if preset.title_box:  # хук на плашке — без свечения и обводки
                lines.append(f"Dialogue: 2,{span},TitleBox,{{{tags}}}{wrapped}")
                continue
            if preset.title_glow:
                glow = ass_color(preset.title_glow)
                lines.append(f"Dialogue: 1,{span},Title,{{{tags}\\1c{glow}&\\3c{glow}&\\bord10\\blur14\\shad0}}{wrapped}")
                lines.append(f"Dialogue: 2,{span},Title,{{{tags}\\3c{glow}&\\bord2\\blur1\\shad0}}{wrapped}")
            else:
                lines.append(f"Dialogue: 2,{span},Title,{{{tags}}}{wrapped}")
    if preset.watermark.strip():
        lines.append(f"Dialogue: 3,{ass_time(0)},{ass_time(clip_len)},Sub,"
                     f"{{\\pos({w // 2},{int(h * preset.watermark_y)})\\fs30\\bord2\\alpha&H50&}}{_escape(preset.watermark)}")

    if preset.subtitles:
        pop = "\\fscx82\\fscy82\\t(0,90,\\fscx100\\fscy100)" if preset.subtitle_pop else ""
        slant = "\\i1" if preset.subtitle_italic else ""
        pos = f"{{\\pos({w // 2},{int(h * preset.subtitle_y)}){pop}{slant}}}"
        hi, base = ass_color(preset.subtitle_highlight), ass_color(preset.subtitle_color)
        n = max(1, preset.subtitle_words)
        for group in word_groups(words, n, line_chars(preset)):
            for j, word in enumerate(group):
                start = (word.start - clip_start) / speed
                end = ((group[j + 1].start if j + 1 < len(group) else word.end) - clip_start) / speed
                start, end = max(0.0, start), min(clip_len, end)
                if end - start < 0.02:
                    continue
                # «ааааааааа» на всю ширину кадра сжимаем до трёх букв
                shown = [re.sub(r"(.)\1{3,}", r"\1\1\1", censor_word(g.text) if preset.censor else g.text) for g in group]
                if preset.subtitle_lower:
                    drop = "-–—\"«»" if preset.subtitle_punct else ".,!;:…-–—\"«»"
                    shown = [t.lower().strip(drop) for t in shown]
                texts = [_escape(t.upper() if preset.subtitle_upper else t) for t in shown]
                parts = [f"{{\\c{hi}&}}{t}{{\\c{base}&}}" if k == j else t for k, t in enumerate(texts)]
                lines.append(f"Dialogue: 0,{ass_time(start)},{ass_time(end)},Sub,{pos}{' '.join(parts)}")
    return out + "\n".join(lines) + "\n"
