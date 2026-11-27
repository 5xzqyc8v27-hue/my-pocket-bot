"""Ролик-рассказ: сценарий + озвучка + видеоряд по фразам + субтитры по слову."""
from __future__ import annotations

import platform
import random
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from .media import probe, run
from .models import Preset, Segment, Transcript, Word
from .subtitles import build_ass

SCRIPT_SYSTEM = """Ты пишешь сценарии коротких вертикальных роликов-рассказов (30-45 секунд, 70-110 слов).
Структура: первая фраза — хук-вопрос или шокирующее утверждение; затем 3-4 коротких факта или
шаги истории; в конце — вопрос к зрителю. Пиши разговорно, короткими фразами, без эмодзи,
хэштегов и ремарок. Пиши только то, в чём уверен: не выдумывай цифры, имена и события.
Верни только текст для озвучки."""


def split_sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?…])\s+", text.strip()) if s.strip()]


def speak(text: str, out: Path, voice: str = "Milena") -> Path:
    """Озвучка встроенным голосом macOS (команда say). На других системах — загрузите свою озвучку."""
    if platform.system() != "Darwin" or not shutil.which("say"):
        raise RuntimeError("Встроенная озвучка есть только на macOS. Загрузите файл озвучки в «Материалы → Озвучка».")
    aiff = out.with_suffix(".aiff")
    run(["say", "-v", voice, "-o", str(aiff), text])
    run(["ffmpeg", "-y", "-v", "error", "-i", str(aiff), str(out)])
    aiff.unlink(missing_ok=True)
    return out


def timings(script: str, voice: Path, whisper: str | None = "small") -> Transcript:
    """Тайминги слов: распознаём озвучку, а если не вышло — раскладываем текст равномерно."""
    duration = probe_audio(voice)
    if whisper:
        try:
            from .transcribe import transcribe
            tr = transcribe(voice, whisper, None, cache=False)
            if tr.segments:
                tr.duration = duration
                return tr
        except Exception:
            pass
    sentences = split_sentences(script)
    total = sum(len(s) for s in sentences) or 1
    t, segs = 0.0, []
    for s in sentences:
        d = duration * len(s) / total
        segs.append(Segment(start=t, end=t + d, text=s))
        t += d
    return Transcript(duration=duration, segments=segs)


def probe_audio(path: Path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                         capture_output=True, text=True, check=True).stdout
    return float(out.strip())


def render_story(script: str, voice: Path, broll: list[Path], preset: Preset, out_path: Path,
                 title: str = "", whisper: str | None = "small", music: Path | None = None,
                 seed: int | None = None) -> Path:
    """Под каждую фразу — свой кусок видеоряда на весь кадр, поверх — субтитры по слову."""
    if not broll:
        raise ValueError("Нужен хотя бы один ролик для видеоряда")
    rnd = random.Random(seed)
    tr = timings(script, voice, whisper)
    W, H, total = preset.width, preset.height, tr.duration
    cuts = [s.start for s in tr.segments] or [0.0]
    cuts[0] = 0.0
    spans = [(a, b) for a, b in zip(cuts, [*cuts[1:], total]) if b - a > 0.2]
    lengths = {p: probe(p)["duration"] for p in broll}
    cmd, parts = ["ffmpeg", "-y", "-v", "error"], []
    for j, (a, b) in enumerate(spans):
        src = broll[j % len(broll)] if len(broll) > 1 else broll[0]
        need = b - a
        start = rnd.uniform(0, max(0.0, lengths[src] - need - 0.5))
        cmd += ["-ss", f"{start:.3f}", "-t", f"{need:.3f}", "-i", str(src.resolve())]
        zoom = "" if j % 2 == 0 else ",scale=iw*1.12:-2"
        parts.append(f"[{j}:v]scale={W}:{H}:force_original_aspect_ratio=increase{zoom},crop={W}:{H},"
                     f"fps={preset.fps},setsar=1[s{j}]")
    n = len(spans)
    cmd += ["-i", str(voice.resolve())]
    words = tr.words_between(0, total)
    style = preset.model_copy(update={"subtitles": True, "title": bool(title), "speed": 1.0})
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "story.ass").write_text(build_ass(words, 0.0, total, title, style), encoding="utf-8")
        parts.append("".join(f"[s{j}]" for j in range(n)) + f"concat=n={n}:v=1:a=0,ass=story.ass,format=yuv420p[out]")
        audio = f"{n}:a"
        if music and Path(music).exists():
            cmd += ["-stream_loop", "-1", "-i", str(Path(music).resolve())]
            parts.append(f"[{n + 1}:a]volume=0.12[m];[{n}:a][m]amix=inputs=2:normalize=0:duration=first[aout]")
            audio = "[aout]"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        cmd += ["-filter_complex", ";".join(parts), "-map", "[out]", "-map", audio, "-t", f"{total:.3f}",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-c:a", "aac", "-b:a", "160k",
                "-movflags", "+faststart", str(out_path.resolve())]
        run(cmd, cwd=Path(tmp))
    return out_path
