"""Экспорт момента в проект CapCut (черновик), чтобы доделать монтаж руками.

У CapCut нет официального API: черновик собирается неофициальной библиотекой pycapcut
в виде файлов проекта. Новые версии CapCut могут менять формат.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from .models import Moment, Preset, Transcript

MAC_DRAFTS = Path.home() / "Movies/CapCut/User Data/Projects/com.lveditor.draft"
WIN_DRAFTS = Path(os.environ.get("LOCALAPPDATA", "")) / "CapCut/User Data/Projects/com.lveditor.draft"


def default_drafts_dir() -> Path | None:
    for path in (MAC_DRAFTS, WIN_DRAFTS):
        if path.is_dir():
            return path
    return None


def _rgb(hex_color: str) -> tuple[float, float, float]:
    h = hex_color.lstrip("#")
    return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))


def export_draft(source: Path, moment: Moment, preset: Preset, transcript: Transcript | None,
                 drafts_dir: Path, name: str | None = None) -> Path:
    """Создаёт черновик: видео-отрезок, надпись-хук и субтитры отдельными дорожками."""
    import pycapcut as cc

    drafts_dir.mkdir(parents=True, exist_ok=True)
    name = name or re.sub(r"[^\w]+", "_", f"clip_{int(moment.start)}_{moment.title}", flags=re.UNICODE)[:50]
    script = cc.DraftFolder(str(drafts_dir)).create_draft(name, preset.width, preset.height,
                                                          fps=preset.fps, allow_replace=True)
    script.add_track(cc.TrackType.video, "video")
    script.add_track(cc.TrackType.text, "title")
    script.add_track(cc.TrackType.text, "subs")
    us = lambda sec: int(round(sec * cc.SEC))  # noqa: E731 — CapCut считает в микросекундах
    length = moment.duration

    script.add_segment(cc.VideoSegment(str(source.resolve()), cc.Timerange(0, us(length)),
                                       source_timerange=cc.Timerange(us(moment.start), us(length))), "video")

    def text(content: str, start: float, dur: float, size: float, color: str, y: float, track: str) -> None:
        script.add_segment(cc.TextSegment(
            content, cc.Timerange(us(start), us(dur)),
            style=cc.TextStyle(size=size, bold=True, color=_rgb(color), align=1, auto_wrapping=True),
            border=cc.TextBorder(color=(0.0, 0.0, 0.0)),
            clip_settings=cc.ClipSettings(transform_y=1 - 2 * y)), track)

    if preset.title and moment.title.strip():
        text(moment.title.upper(), 0, length, preset.title_size / 6, preset.title_color, preset.title_y, "title")
    if preset.subtitles and transcript:
        words = transcript.words_between(moment.start, moment.end)
        n = max(1, preset.subtitle_words)
        cursor = 0.0  # сегменты на одной дорожке не должны пересекаться
        for i in range(0, len(words), n):
            group = words[i:i + n]
            start = max(cursor, group[0].start - moment.start)
            end = min(length, (words[i + n].start if i + n < len(words) else group[-1].end) - moment.start)
            if end - start < 0.05:
                continue
            start, end = round(start, 3), round(end, 3) - 0.001
            cursor = end + 0.001
            line = " ".join(w.text for w in group)
            text(line.upper() if preset.subtitle_upper else line, start, end - start,
                 preset.subtitle_size / 6, preset.subtitle_color, preset.subtitle_y, "subs")
    script.save()
    return drafts_dir / name
