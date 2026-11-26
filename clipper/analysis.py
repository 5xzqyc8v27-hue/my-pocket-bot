from __future__ import annotations

import re
import subprocess
from pathlib import Path

import numpy as np

from .media import probe


def detect_scenes(video: Path, threshold: float = 0.35) -> list[float]:
    """Таймкоды склеек (смен сцены) в секундах."""
    proc = subprocess.run(
        ["ffmpeg", "-v", "info", "-i", str(video), "-an",
         "-vf", f"fps=5,scale=320:-2,select='gt(scene,{threshold})',showinfo", "-f", "null", "-"],
        capture_output=True, text=True)
    return [round(float(t), 2) for t in re.findall(r"pts_time:([\d.]+)", proc.stderr)]


def _hashes(video: Path, times: list[float], crop: str = "") -> np.ndarray:
    """dHash 64 бита для кадров в заданные моменты времени."""
    out = []
    for t in times:
        raw = subprocess.run(
            ["ffmpeg", "-v", "error", "-ss", f"{t:.3f}", "-i", str(video), "-frames:v", "1",
             "-vf", f"{crop}scale=9:8,format=gray", "-f", "rawvideo", "-"],
            capture_output=True).stdout
        if len(raw) != 72:
            continue
        g = np.frombuffer(raw, dtype=np.uint8).reshape(8, 9).astype(np.int16)
        out.append((g[:, 1:] > g[:, :-1]).flatten())
    return np.array(out, dtype=bool)


def uniqueness(source: Path, clip: Path, start: float, speed: float = 1.0, samples: int = 12) -> dict:
    """Насколько готовый ролик визуально отличается от исходного отрезка.

    Сравниваем перцептивные хэши кадров: 0% — копия, ~50% — совершенно другая картинка.
    Это наша собственная оценка, а не проверка алгоритмами площадок.
    """
    info = probe(clip)
    length, w, hgt = info["duration"], info["width"], info["height"]
    clip_times = [length * (i + 0.5) / samples for i in range(samples)]
    src = _hashes(source, [start + t * speed for t in clip_times])
    band = int(w * 9 / 16)
    # Исходник может лежать в любой части кадра, поэтому сравниваем с несколькими
    # областями ролика и берём самую похожую — оценка по худшему случаю.
    regions = ["", f"crop={w}:{band}:0:{(hgt - band) // 2},", f"crop={w}:{w}:0:{(hgt - w) // 2},",
               f"crop={w}:{hgt // 2}:0:0,", f"crop={w}:{hgt // 2}:0:{hgt // 2},"]
    diffs = []
    for crop in regions:
        cur = _hashes(clip, clip_times, crop)
        n = min(len(cur), len(src))
        if n:
            diffs.append(float((cur[:n] != src[:n]).mean()))
    if not diffs:
        raise RuntimeError("Не удалось прочитать кадры для сравнения")
    diff, n = min(diffs), len(src)
    percent = round(min(diff / 0.5, 1.0) * 100)
    if percent < 30:
        verdict = "почти копия исходника"
    elif percent < 60:
        verdict = "заметно изменён"
    else:
        verdict = "сильно отличается от исходника"
    return {"percent": percent, "verdict": verdict, "frames": n}
