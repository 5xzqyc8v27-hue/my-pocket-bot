from __future__ import annotations

import os
import subprocess
from pathlib import Path

import numpy as np
from pydantic import BaseModel

from .models import Moment, Transcript

DEFAULT_MODEL = os.environ.get("CLIPPER_MODEL", "claude-opus-5-5")

SYSTEM = """Ты — редактор коротких вертикальных видео (TikTok, Shorts, Reels).
Тебе дают расшифровку длинного видео с таймкодами. Найди самые сильные моменты,
которые работают как самостоятельные ролики: смешное, эмоциональное, неожиданное,
конфликт, сильная мысль, законченная история.

Правила:
- Момент должен быть понятен без контекста, начинаться с зацепки и заканчиваться
  на завершённой фразе или реакции. Границы ставь по таймкодам строк расшифровки.
- title — надпись на экране, до 6 слов, без кавычек и эмодзи, на языке видео.
- score — честная оценка 0-100; слабые моменты лучше не включать совсем.
- profanity=true, если в моменте есть мат.
- Моменты не должны пересекаться."""


class _Picked(BaseModel):
    moments: list[Moment]


def _format_transcript(tr: Transcript) -> str:
    return "\n".join(f"[{s.start:.1f}-{s.end:.1f}] {s.text}" for s in tr.segments)


def pick_with_claude(tr: Transcript, count: int, min_len: float, max_len: float,
                     model: str = DEFAULT_MODEL) -> list[Moment]:
    """Выбор моментов через Claude API. Нужен ANTHROPIC_API_KEY (или `ant auth login`)."""
    import anthropic

    client = anthropic.Anthropic()
    prompt = (
        f"Нужно до {count} моментов длиной от {min_len:.0f} до {max_len:.0f} секунд.\n"
        f"Длительность видео: {tr.duration:.0f} с.\n\nРасшифровка:\n{_format_transcript(tr)}"
    )
    response = client.messages.parse(
        model=model,
        max_tokens=16000,
        system=SYSTEM,
        messages=[{"role": "user", "content": prompt}],
        output_format=_Picked,
    )
    if response.stop_reason == "refusal" or response.parsed_output is None:
        raise RuntimeError(f"Claude не вернул моменты (stop_reason={response.stop_reason})")
    return _clean(response.parsed_output.moments, tr.duration, min_len, max_len, count)


def _loudness(video: Path, rate: int = 8000) -> np.ndarray:
    """Громкость (RMS) по секундам."""
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(video), "-vn", "-ac", "1", "-ar", str(rate),
         "-f", "s16le", "-"], capture_output=True, check=True).stdout
    audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32)
    n = len(audio) // rate
    if n == 0:
        return np.zeros(1, dtype=np.float32)
    return np.sqrt((audio[: n * rate].reshape(n, rate) ** 2).mean(axis=1))


def pick_heuristic(video: Path, tr: Transcript | None, count: int, min_len: float,
                   max_len: float) -> list[Moment]:
    """Запасной выбор без нейросети: всплески громкости плюс плотность речи."""
    loud = _loudness(video)
    n = len(loud)
    score = (loud - loud.mean()) / (loud.std() + 1e-6)
    if tr:
        density = np.zeros(n, dtype=np.float32)
        for w in tr.words_between(0, n):
            density[min(int(w.start), n - 1)] += 1
        score = score + (density - density.mean()) / (density.std() + 1e-6)
    win = int(min(max((min_len + max_len) / 2, min_len), n))
    if win <= 0:
        return []
    sums = np.convolve(score, np.ones(win), mode="valid")
    moments: list[Moment] = []
    taken = np.zeros(len(sums), dtype=bool)
    for idx in np.argsort(-sums):
        if len(moments) >= count:
            break
        if taken[idx]:
            continue
        start, end = float(idx), float(idx + win)
        if tr:
            start, end = _snap(tr, start, end, min_len, max_len)
        rank = float(sums[idx] / win)
        moments.append(Moment(start=start, end=end, title="", reason="всплеск громкости и речи",
                              score=int(np.clip(50 + rank * 20, 0, 100))))
        taken[max(0, idx - win): idx + win] = True
    return sorted(moments, key=lambda m: m.start)


def _snap(tr: Transcript, start: float, end: float, min_len: float, max_len: float) -> tuple[float, float]:
    """Подтягиваем границы к началу и концу фраз, чтобы не резать на полуслове."""
    starts = [s.start for s in tr.segments if abs(s.start - start) <= 5]
    ends = [s.end for s in tr.segments if abs(s.end - end) <= 5]
    new_start = min(starts, key=lambda t: abs(t - start)) if starts else start
    new_end = min(ends, key=lambda t: abs(t - end)) if ends else end
    if min_len <= new_end - new_start <= max_len:
        return new_start, new_end
    return start, end


def _clean(moments: list[Moment], duration: float, min_len: float, max_len: float,
           count: int) -> list[Moment]:
    """Отбрасываем кривые и пересекающиеся моменты, оставляем лучшие по оценке."""
    kept: list[Moment] = []
    for m in sorted(moments, key=lambda m: -m.score):
        m.start = max(0.0, m.start)
        m.end = min(duration, m.end) if duration else m.end
        if not (min_len * 0.7 <= m.duration <= max_len * 1.3):
            continue
        if any(m.start < k.end and k.start < m.end for k in kept):
            continue
        kept.append(m)
        if len(kept) >= count:
            break
    return sorted(kept, key=lambda m: m.start)
