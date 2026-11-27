"""Смех и крик по звуку: громкие места с характерным «дробным» ритмом (смех) или резким всплеском (крик)."""
from __future__ import annotations

import subprocess
from pathlib import Path

import numpy as np

HOP = 0.05  # шаг огибающей громкости, с


def _envelope(video: Path, start: float | None, length: float | None, rate: int = 8000) -> np.ndarray:
    seek = ["-ss", f"{start:.3f}", "-t", f"{length:.3f}"] if start is not None else []
    raw = subprocess.run(["ffmpeg", "-v", "error", *seek, "-i", str(video), "-vn", "-ac", "1", "-ar", str(rate),
                          "-f", "s16le", "-"], capture_output=True, check=True).stdout
    audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32)
    size = int(rate * HOP)
    n = len(audio) // size
    return np.sqrt((audio[: n * size].reshape(n, size) ** 2).mean(axis=1)) if n else np.zeros(0, dtype=np.float32)


def find_bursts(video: Path, start: float | None = None, length: float | None = None, count: int = 8,
                gap: float = 4.0) -> list[dict]:
    """Места, похожие на смех или крик. Время — в секундах от начала отрезка.

    Смех — это серия коротких выдохов 3-8 раз в секунду, поэтому в огибающей громкости у него сильная
    пульсация на этих частотах. Крик — резкий скачок громкости намного выше обычного уровня.
    Оценка по звуку приблизительная: музыка с быстрым ритмом тоже может сработать.
    """
    env = _envelope(video, start, length)
    win = int(2.0 / HOP)
    if len(env) < win * 2:
        return []
    mean, std = float(env.mean()), float(env.std()) + 1e-6
    freqs = np.fft.rfftfreq(win, HOP)
    band = (freqs >= 3) & (freqs <= 8)
    rows = []
    for i in range(0, len(env) - win, win // 4):
        chunk = env[i:i + win]
        loud = (float(chunk.mean()) - mean) / std
        if loud < 0.6:
            continue
        spec = np.abs(np.fft.rfft(chunk - chunk.mean())) ** 2
        pulse = float(spec[band].sum() / (spec[1:].sum() + 1e-6))
        peak = (float(chunk.max()) - mean) / std
        kind = "смех" if pulse > 0.45 else "крик" if peak > 3.2 else None
        if kind:
            rows.append({"t": round(i * HOP + 1.0, 1), "kind": kind, "strength": round(loud + pulse * 2 + peak * 0.2, 2)})
    rows.sort(key=lambda r: -r["strength"])
    kept: list[dict] = []
    for r in rows:
        if len(kept) < count and all(abs(r["t"] - k["t"]) >= gap for k in kept):
            kept.append(r)
    return sorted(kept, key=lambda r: r["t"])
