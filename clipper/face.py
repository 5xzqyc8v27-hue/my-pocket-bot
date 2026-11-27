"""Умный кроп: находим лицо стримера и по нему определяем вебку или часть кадра."""
from __future__ import annotations

import subprocess
from pathlib import Path

import numpy as np


def _frames(video: Path, start: float, length: float, count: int, width: int = 640) -> list[np.ndarray]:
    """Несколько кадров отрезка в оттенках серого."""
    out = []
    for i in range(count):
        t = start + length * (i + 0.5) / count
        proc = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{t:.2f}", "-i", str(video), "-frames:v", "1",
                               "-vf", f"scale={width}:-2,format=gray", "-f", "image2pipe", "-vcodec", "pgm", "-"],
                              capture_output=True)
        if proc.stdout[:2] == b"P5":
            parts = proc.stdout.split(b"\n", 3)
            w, h = map(int, parts[1].split())
            out.append(np.frombuffer(parts[3], dtype=np.uint8, count=w * h).reshape(h, w))
    return out


def find_face(video: Path, start: float = 0.0, length: float = 60.0, samples: int = 10) -> dict | None:
    """Самое устойчивое лицо в кадре: вебка стоит на месте, а лица в игре двигаются.

    Возвращает центр и размер лица в долях кадра и долю кадров, где оно нашлось.
    """
    try:
        import cv2
    except ImportError as exc:
        raise RuntimeError("Для умного кропа нужен OpenCV: pip install opencv-python-headless") from exc
    cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    frames = _frames(video, start, length, samples)
    if not frames:
        return None
    hits = []  # (номер кадра, cx, cy, w, h) в долях
    for n, gray in enumerate(frames):
        H, W = gray.shape
        for (x, y, w, h) in cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=6, minSize=(W // 22, W // 22)):
            hits.append((n, (x + w / 2) / W, (y + h / 2) / H, w / W, h / H))
    best = None
    for seed in hits:  # группируем находки, стоящие на одном месте
        group = [x for x in hits if abs(x[1] - seed[1]) < seed[3] * 0.7 and abs(x[2] - seed[2]) < seed[4] * 0.7]
        seen = len({x[0] for x in group})
        if best is None or seen > best[0] or (seen == best[0] and np.mean([x[3] for x in group]) > best[1][2]):
            best = (seen, tuple(float(np.median([x[k] for x in group])) for k in (1, 2, 3, 4)))
    if not best or best[0] < max(2, len(frames) * 0.4):
        return None
    cx, cy, w, h = best[1]
    return {"cx": round(cx, 3), "cy": round(cy, 3), "w": round(w, 3), "h": round(h, 3),
            "stable": round(best[0] / len(frames), 2)}


def webcam_rect(face: dict) -> list[float]:
    """Прямоугольник вебки вокруг лица; края, почти дошедшие до границы кадра, прижимаем к ней."""
    w, h = min(1.0, face["w"] * 3.4), min(1.0, face["h"] * 2.6)
    x, y = face["cx"] - w / 2, face["cy"] - h * 0.45
    x, y = max(0.0, min(x, 1 - w)), max(0.0, min(y, 1 - h))
    if x < 0.05:
        w, x = w + x, 0.0
    if y < 0.05:
        h, y = h + y, 0.0
    if x + w > 0.95:
        w = 1 - x
    if y + h > 0.95:
        h = 1 - y
    return [round(v, 3) for v in (x, y, w, h)]


def apply_face(preset, face: dict | None):
    """Подставляет в пресет вебку или положение кадра по найденному лицу."""
    if not face or not preset.auto_face:
        return preset
    if preset.layout in ("reaction", "chat"):
        return preset.model_copy(update={"webcam": tuple(webcam_rect(face))})
    if preset.layout == "full":
        return preset.model_copy(update={"crop_x": round(min(1.0, max(0.0, (face["cx"] - 0.16) / 0.68)), 2)})
    return preset
