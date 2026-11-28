"""Раскладка «говорящий»: кадр на весь экран держит крупный план того, кто сейчас говорит.

Находим лица на кадрах, собираем их в «людей» (кто где сидит), по движению в области рта решаем,
кто говорит в каждый момент, и переводим кадр с человека на человека прямыми склейками.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import numpy as np

FPS = 5          # кадров в секунду для анализа
MIN_HOLD = 1.6   # минимум секунд на одном человеке — чаще переключать нельзя, рябит
WIDTH = 640
MODEL = Path(__file__).parent / "data" / "face_yunet.onnx"


def read_frames(video: Path, start: float, length: float) -> list[np.ndarray]:
    """Цветные кадры отрезка шириной WIDTH, FPS штук в секунду, одним запуском ffmpeg."""
    probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
                            "-of", "csv=p=0", str(video)], capture_output=True, text=True).stdout.strip().split(",")
    try:
        height = int(int(probe[1]) * WIDTH / int(probe[0])) // 2 * 2
    except (ValueError, IndexError):
        return []
    raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{start:.2f}", "-t", f"{length:.2f}", "-i", str(video),
                          "-vf", f"fps={FPS},scale={WIDTH}:{height}", "-f", "rawvideo", "-pix_fmt", "bgr24", "-"],
                         capture_output=True).stdout
    size = WIDTH * height * 3
    return [np.frombuffer(raw, dtype=np.uint8, count=size, offset=i * size).reshape(height, WIDTH, 3)
            for i in range(len(raw) // size)]


def _detector(width: int, height: int):
    """Функция кадр -> [(x, y, w, h)]. Точный детектор YuNet, а без его файла — старый каскад."""
    import cv2
    if MODEL.exists() and hasattr(cv2, "FaceDetectorYN"):
        net = cv2.FaceDetectorYN.create(str(MODEL), "", (width, height), 0.6, 0.3, 50)

        def find(frame):
            faces = net.detect(frame)[1]
            return [] if faces is None else [tuple(float(v) for v in f[:4]) for f in faces]
        return find
    cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    return lambda frame: [tuple(float(v) for v in f) for f in
                          cascade.detectMultiScale(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), 1.15, 5, minSize=(36, 36))]


def find_people(frames: list[np.ndarray]) -> list[dict]:
    """Люди в кадре: для каждого — рамка лица на каждом кадре (или None) и средние центр и размер."""
    h, w = frames[0].shape[:2]
    find = _detector(w, h)
    people: list[dict] = []
    for i, frame in enumerate(frames):
        taken = set()
        for x, y, fw, fh in sorted(find(frame), key=lambda f: -f[2]):
            if fw < w * 0.035:
                continue
            cx, cy = x + fw / 2, y + fh / 2
            best = None
            for k, p in enumerate(people):
                reach = max(p["w"], fw) * 0.9
                dist = abs(p["cx"] - cx) + abs(p["cy"] - cy)
                if k not in taken and dist < reach and (best is None or dist < best[0]):
                    best = (dist, k)
            if best:
                p = people[best[1]]
                taken.add(best[1])
                p.update(cx=p["cx"] * 0.7 + cx * 0.3, cy=p["cy"] * 0.7 + cy * 0.3, w=p["w"] * 0.7 + fw * 0.3, h=p["h"] * 0.7 + fh * 0.3)
            else:
                p = {"cx": cx, "cy": cy, "w": fw, "h": fh, "boxes": [None] * len(frames)}
                people.append(p)
                taken.add(len(people) - 1)
            p["boxes"][i] = (x, y, fw, fh)
    out = []
    for p in people:
        seen = [b for b in p["boxes"] if b]
        if len(seen) >= max(4, len(frames) * 0.2):
            p.update(share=len(seen) / len(frames), w=float(np.median([b[2] for b in seen])), h=float(np.median([b[3] for b in seen])))
            out.append(p)
    return sorted(out, key=lambda p: p["cx"])


def _mouth(frame: np.ndarray, box: tuple) -> np.ndarray | None:
    """Нижняя треть лица в одном размере — чтобы сравнивать кадры без учёта сдвига головы."""
    import cv2
    x, y, w, h = box
    x0, x1 = int(max(0, x + w * 0.15)), int(min(frame.shape[1], x + w * 0.85))
    y0, y1 = int(max(0, y + h * 0.58)), int(min(frame.shape[0], y + h * 1.05))
    if x1 - x0 < 6 or y1 - y0 < 4:
        return None
    return cv2.resize(cv2.cvtColor(frame[y0:y1, x0:x1], cv2.COLOR_BGR2GRAY), (32, 16)).astype(np.int16)


def activity(frames: list[np.ndarray], people: list[dict]) -> np.ndarray:
    """Движение рта каждого человека по кадрам: строки — люди, столбцы — время."""
    out = np.zeros((len(people), len(frames)))
    biggest = max(p["w"] for p in people)
    for k, p in enumerate(people):
        prev = None
        for i, box in enumerate(p["boxes"]):
            patch = _mouth(frames[i], box) if box else None
            if patch is not None and prev is not None and i - prev[0] <= 2:
                out[k, i] = np.abs(patch - prev[1]).mean()
            if patch is not None:
                prev = (i, patch)
        moving = out[k][out[k] > 0]
        out[k] /= max(float(np.median(moving)) if moving.size else 1.0, 1.0)  # у каждого своя «норма»
        out[k] *= (p["w"] / biggest) ** 0.5  # кто ближе к камере, тот чаще и есть главный
    win = max(1, int(FPS * 1.2))
    kernel = np.ones(win) / win
    return np.array([np.convolve(row, kernel, mode="same") for row in out])


def timeline(act: np.ndarray) -> list[tuple[float, float, int]]:
    """Кто в кадре по времени: [(начало, конец, номер человека)], без слишком частых переключений."""
    if act.shape[0] == 0:
        return []
    steps, cur, since, out = act.shape[1], int(np.argmax(act[:, : FPS * 2].mean(axis=1))), 0, []
    for i in range(steps):
        ahead = act[:, i:i + FPS].mean(axis=1)  # смотрим на секунду вперёд, чтобы не дёргаться на всплесках
        best = int(np.argmax(ahead))
        if best != cur and i - since >= MIN_HOLD * FPS and ahead[best] > ahead[cur] * 1.35 + 0.05:
            out.append((since / FPS, i / FPS, cur))
            cur, since = best, i
    out.append((since / FPS, steps / FPS, cur))
    return out


def plan(video: Path, start: float, length: float, aspect: float = 9 / 16) -> dict | None:
    """План кадрирования отрезка. None — людей не нашли, тогда кадр берётся по центру.

    Возвращает размер окна в долях исходного кадра и список (начало, конец, x, y) — левый верхний угол окна.
    """
    frames = read_frames(video, start, length)
    if len(frames) < 8:
        return None
    fh, fw = frames[0].shape[:2]
    people = find_people(frames)
    if not people:
        return None
    cuts = []
    for a, b, who in timeline(activity(frames, people)):
        boxes = [bx for bx in people[who]["boxes"][int(a * FPS):int(b * FPS) + 1] if bx] or [bx for bx in people[who]["boxes"] if bx]
        cx = float(np.median([bx[0] + bx[2] / 2 for bx in boxes]))
        cy = float(np.median([bx[1] + bx[3] / 2 for bx in boxes]))
        face_h = float(np.median([bx[3] for bx in boxes]))
        # у каждого свой масштаб: лицо занимает около трети высоты; ближе 0.5 кадра нельзя — картинка рассыплется
        crop_h = min(fh, max(fh * 0.5, face_h * 3.1))
        crop_w = min(fw, crop_h * aspect)
        crop_h = crop_w / aspect
        x = min(max(0.0, cx - crop_w / 2), fw - crop_w)
        y = min(max(0.0, cy - crop_h * 0.40), fh - crop_h)
        cuts.append((round(a, 2), round(b, 2), round(x / fw, 4), round(y / fh, 4), round(crop_w / fw, 4), round(crop_h / fh, 4)))
    return {"cuts": cuts, "people": len(people)}


def crop_graph(p: dict, src: str, out: str, width: int, height: int, speed: float = 1.0) -> list[str]:
    """Части filter_complex: исходник режется на отрезки, каждый кадрируется на своего человека и склеивается."""
    cuts = p["cuts"]
    if len(cuts) == 1:
        _, _, x, y, w, h = cuts[0]
        return [f"[{src}]crop=iw*{w}:ih*{h}:iw*{x}:ih*{y},scale={width}:{height}:flags=lanczos,setsar=1[{out}]"]
    parts = [f"[{src}]split={len(cuts)}" + "".join(f"[sp{i}]" for i in range(len(cuts)))]
    for i, (a, b, x, y, w, h) in enumerate(cuts):
        end = f":end={b / speed:.3f}" if i < len(cuts) - 1 else ""
        parts.append(f"[sp{i}]trim=start={a / speed:.3f}{end},setpts=PTS-STARTPTS,crop=iw*{w}:ih*{h}:iw*{x}:ih*{y},"
                     f"scale={width}:{height}:flags=lanczos,setsar=1[sc{i}]")
    parts.append("".join(f"[sc{i}]" for i in range(len(cuts))) + f"concat=n={len(cuts)}:v=1:a=0[{out}]")
    return parts
