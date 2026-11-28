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


AUDIO = (".mp3", ".wav", ".ogg", ".m4a", ".aac")
IMAGE = (".png", ".jpg", ".jpeg", ".webp")
PLACE_Y = {"top": 0.56, "center": 0.0, "bottom": -0.56, "full": 0.0}


def _length(path: Path) -> float:
    import subprocess
    try:
        return float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                                     str(path)], capture_output=True, text=True, timeout=30).stdout.strip())
    except (OSError, ValueError, subprocess.SubprocessError):
        return 0.0


def export_draft(source: Path, moment: Moment, preset: Preset, transcript: Transcript | None,
                 drafts_dir: Path, name: str | None = None, resolve=None, music: Path | None = None) -> Path:
    """Создаёт черновик: видео-отрезок, надпись-хук, субтитры, вставки и звуки — всё отдельными дорожками."""
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
    # вставки момента: звуки — на звуковые дорожки, картинки и ролики — поверх видео; каждая на своей дорожке
    notes = []
    for k, ins in enumerate(moment.inserts):
        path = Path(resolve(ins.file)) if resolve else Path(ins.file)
        ext = path.suffix.lower()
        if not path.is_file() or ins.at >= length:
            continue
        if ext in (".webm", ".gif"):  # CapCut такие файлы не открывает — кладём неподвижную картинку
            still = drafts_dir / name / f"insert{k}.png"
            import subprocess
            decoder = ["-c:v", "libvpx-vp9"] if ext == ".webm" else []
            done = subprocess.run(["ffmpeg", "-y", "-v", "error", *decoder, "-ss", "0.5", "-i", str(path),
                                   "-frames:v", "1", str(still)], capture_output=True, timeout=60)
            if done.returncode != 0 or not still.exists():
                notes.append(path.name)
                continue
            path, ext = still, ".png"
        dur = min(ins.duration, length - ins.at)
        if ext not in IMAGE:
            dur = min(dur, (_length(path) or dur) - 0.05)  # отрезок не может быть длиннее самого файла
        if dur < 0.1:
            continue
        try:
            if ext in AUDIO:
                script.add_track(cc.TrackType.audio, f"sound{k}")
                script.add_segment(cc.AudioSegment(str(path.resolve()), cc.Timerange(us(ins.at), us(dur))), f"sound{k}")
            else:
                script.add_track(cc.TrackType.video, f"insert{k}", relative_index=k + 1)
                scale = 1.0 if ins.place == "full" else max(0.1, min(1.0, ins.scale))
                script.add_segment(cc.VideoSegment(
                    str(path.resolve()), cc.Timerange(us(ins.at), us(dur)),
                    clip_settings=cc.ClipSettings(scale_x=scale, scale_y=scale,
                                                  transform_y=PLACE_Y.get(ins.place, 0.0))), f"insert{k}")
        except Exception as exc:  # одна неудачная вставка не должна срывать весь черновик
            notes.append(f"{path.name}: {str(exc)[:80]}")
    if music and Path(music).is_file():
        try:
            dur = min(length, _length(Path(music)) or length)
            script.add_track(cc.TrackType.audio, "music")
            script.add_segment(cc.AudioSegment(str(Path(music).resolve()), cc.Timerange(0, us(dur)), volume=0.2), "music")
        except Exception:
            notes.append(Path(music).name)
    script.save()
    if notes:
        (drafts_dir / name / "не_добавлено.txt").write_text("\n".join(notes), encoding="utf-8")
    return drafts_dir / name


def export_clean_draft(source: Path, moment: Moment, preset: Preset, transcript: Transcript | None,
                       drafts_dir: Path, name: str | None = None, resolve=None, fonts: list[Path] | None = None,
                       target_dir: str = "", stickers: list[Path] | None = None) -> Path:
    """Черновик для правки: готовый вертикальный ролик без надписей, а хук и субтитры — отдельными дорожками.

    Видео лежит внутри папки черновика. target_dir — где эта папка окажется на другом компьютере
    (пути внутри черновика абсолютные); пусто — черновик остаётся там, где создан.
    """
    import json

    import pycapcut as cc

    from .render import add_ad_break, render_clip
    from .subtitles import word_groups

    drafts_dir.mkdir(parents=True, exist_ok=True)
    name = name or re.sub(r"[^\w]+", "_", f"clip_{int(moment.start)}_{moment.title}", flags=re.UNICODE)[:50]
    script = cc.DraftFolder(str(drafts_dir)).create_draft(name, preset.width, preset.height,
                                                          fps=preset.fps, allow_replace=True)
    folder = drafts_dir / name
    video = folder / "video.mp4"
    bare = preset.model_copy(update={"title": False, "subtitles": False, "watermark": "", "ad_break": "", "teaser": 0.0})
    render_clip(source, moment, bare, video, transcript, None, resolve, fonts, stickers)
    pause_at, pause_len = None, 0.0
    banner = (Path(resolve(preset.ad_break)) if resolve else Path(preset.ad_break)) if preset.ad_break else None
    if banner and banner.is_file():  # рекламная пауза остаётся в ролике, текст после неё сдвигается
        words = transcript.words_between(moment.start, moment.end) if transcript else []
        before = _length(video)
        body = folder / "body.mp4"
        video.rename(body)
        pause_at = add_ad_break(body, banner, preset, video, [(w.end - moment.start) / preset.speed for w in words])
        body.unlink()
        pause_len = _length(video) - before
    material = cc.VideoMaterial(str(video.resolve()))
    length = material.duration / cc.SEC  # длину берём такой, какой её видит CapCut
    us = lambda sec: int(round(sec * cc.SEC))  # noqa: E731
    shift = lambda t: t + pause_len if pause_at is not None and t >= pause_at else t  # noqa: E731
    script.add_track(cc.TrackType.video, "video")
    script.add_track(cc.TrackType.text, "hook")
    script.add_track(cc.TrackType.text, "subs")
    script.add_segment(cc.VideoSegment(material, cc.Timerange(0, material.duration)), "video")

    def text(content: str, start: float, dur: float, size: float, color: str, y: float, track: str) -> None:
        script.add_segment(cc.TextSegment(
            content, cc.Timerange(us(start), us(dur)),
            style=cc.TextStyle(size=size, bold=True, color=_rgb(color), align=1, auto_wrapping=True),
            border=cc.TextBorder(color=(0.0, 0.0, 0.0)),
            clip_settings=cc.ClipSettings(transform_y=1 - 2 * y)), track)

    if preset.title and moment.title.strip():
        hook = moment.title.replace("*", "")
        until = min(length, preset.title_seconds) if preset.title_seconds > 0 else length
        text(hook.upper() if preset.title_upper else hook, 0, until - 0.01, preset.title_size / 6, preset.title_color,
             preset.title_y, "hook")
    if preset.subtitles and transcript:
        cursor = 0.0
        from .subtitles import line_chars
        for group in word_groups(transcript.words_between(moment.start, moment.end), max(1, preset.subtitle_words),
                                 line_chars(preset)):
            start = (group[0].start - moment.start) / preset.speed
            end = (group[-1].end - moment.start) / preset.speed
            if pause_at is not None and start < pause_at < end:
                end = pause_at  # фраза не должна висеть поверх баннера
            start, end = max(cursor, shift(start)), min(length, shift(end) if end != pause_at else end)
            if end - start < 0.08:
                continue
            cursor = end + 0.002
            line = " ".join(w.text for w in group)
            if preset.subtitle_lower:
                drop = '-–—"«»' if preset.subtitle_punct else '.,!;:…-–—"«»'
                line = " ".join(w.text.lower().strip(drop) for w in group)
            text(line.upper() if preset.subtitle_upper else line, round(start, 3), round(end - start, 3) - 0.001,
                 preset.subtitle_size / 6, preset.subtitle_color, preset.subtitle_y, "subs")
    script.save()
    if target_dir:  # переписываем пути под папку CapCut на другом компьютере
        content = folder / "draft_content.json"
        data = content.read_text(encoding="utf-8")
        here = json.dumps(str(folder.resolve()), ensure_ascii=False)[1:-1]
        there = json.dumps(target_dir.rstrip("/") + "/" + name, ensure_ascii=False)[1:-1]
        content.write_text(data.replace(here, there), encoding="utf-8")
    return folder


def zip_draft(folder: Path, out: Path) -> Path:
    """Папка черновика одним архивом — чтобы передать на другой компьютер."""
    import zipfile
    with zipfile.ZipFile(out, "w", zipfile.ZIP_STORED) as z:
        for f in sorted(folder.rglob("*")):
            if f.is_file():
                z.write(f, f"{folder.name}/{f.relative_to(folder)}")
    return out
