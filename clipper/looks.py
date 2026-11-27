"""Готовые стили картинки и копии готового ролика: всё на фильтрах ffmpeg, без нейросетей."""
from __future__ import annotations

import random
from pathlib import Path

from .media import run

LOOKS = {"cinematic": "Кино", "warm": "Тёплый", "cold": "Холодный", "vintage": "Винтаж", "bw": "Чёрно-белый",
         "neon": "Неон", "vhs": "VHS", "grain": "Плёночное зерно", "hdr": "HDR", "sharp": "Резкий",
         "soft": "Мягкий", "sunset": "Закат", "matrix": "Зелёный"}


def look_filter(name: str, strength: float = 0.5) -> str:
    """Цепочка фильтров стиля; пустая строка, если стиль не задан."""
    k = max(0.0, min(1.0, strength))
    return {
        "cinematic": f"eq=contrast={1 + 0.18 * k:.3f}:saturation={1 - 0.10 * k:.3f}:brightness={-0.025 * k:.4f},"
                     f"colorbalance=bs={0.08 * k:.3f}:rm={0.05 * k:.3f}",
        "warm": f"eq=contrast={1 + 0.08 * k:.3f}:saturation={1 + 0.18 * k:.3f}:brightness={0.02 * k:.4f},"
                f"colorbalance=rs={0.10 * k:.3f}:bs={-0.06 * k:.3f}",
        "cold": f"eq=contrast={1 + 0.08 * k:.3f}:saturation={1 - 0.03 * k:.3f},colorbalance=bs={0.14 * k:.3f}:rs={-0.07 * k:.3f}",
        "vintage": f"curves=vintage,eq=saturation={1 - 0.22 * k:.3f}:contrast={1 - 0.06 * k:.3f}",
        "bw": f"hue=s=0,eq=contrast={1 + 0.14 * k:.3f}",
        "neon": f"eq=contrast={1 + 0.22 * k:.3f}:saturation={1 + 0.45 * k:.3f},unsharp=5:5:{0.5 + k:.2f}:5:5:0",
        "vhs": f"noise=alls={4 + 18 * k:.0f}:allf=t+u,eq=contrast={1 - 0.08 * k:.3f}:saturation={1 - 0.15 * k:.3f}",
        "grain": f"noise=alls={3 + 22 * k:.0f}:allf=t+u",
        "hdr": f"eq=contrast={1 + 0.24 * k:.3f}:saturation={1 + 0.12 * k:.3f},unsharp=7:7:{0.7 + 1.1 * k:.2f}:7:7:0",
        "sharp": f"unsharp=7:7:{0.8 + 1.4 * k:.2f}:7:7:0,eq=contrast={1 + 0.06 * k:.3f}",
        "soft": f"gblur=sigma={0.3 + 1.2 * k:.2f},eq=contrast={1 - 0.05 * k:.3f}:brightness={0.015 * k:.4f}",
        "sunset": f"colorbalance=rs={0.16 * k:.3f}:gs={0.04 * k:.3f}:bs={-0.13 * k:.3f},eq=saturation={1 + 0.18 * k:.3f}",
        "matrix": f"colorchannelmixer=rr={1 - 0.65 * k:.3f}:gg={1 + 0.30 * k:.3f}:bb={1 - 0.75 * k:.3f}",
    }.get(name, "")


def clean_filters(denoise: float, sharpen: float) -> list[str]:
    """Шумоподавление и резкость (0..1) — для мыльных исходников."""
    out = []
    if denoise > 0:
        s, t = 1 + 5 * min(denoise, 1.0), 1.5 + 6 * min(denoise, 1.0)
        out.append(f"hqdn3d={s:.2f}:{s:.2f}:{t:.2f}:{t:.2f}")
    if sharpen > 0:
        out.append(f"unsharp=7:7:{0.25 + 1.55 * min(sharpen, 1.0):.2f}:7:7:0")
    return out


def copy_params(seed: int, look: str = "") -> dict:
    """Случайные отличия одной копии: приближение, сдвиг, скорость, цвет."""
    rnd = random.Random(seed)
    return {"zoom": round(rnd.uniform(1.01, 1.05), 4), "speed": round(rnd.uniform(0.98, 1.03), 4),
            "brightness": round(rnd.uniform(-0.02, 0.02), 4), "contrast": round(1 + rnd.uniform(-0.04, 0.04), 4),
            "saturation": round(1 + rnd.uniform(-0.05, 0.05), 4), "x": round(rnd.random(), 3), "y": round(rnd.random(), 3),
            "mirror": False, "look": look, "look_strength": round(rnd.uniform(0.25, 0.5), 2)}


def copy_filter(p: dict) -> str:
    parts = [f"scale=ceil(iw*{p['zoom']}/2)*2:ceil(ih*{p['zoom']}/2)*2",
             f"crop=iw/{p['zoom']}:ih/{p['zoom']}:(iw-ow)*{p['x']}:(ih-oh)*{p['y']}", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
             f"eq=brightness={p['brightness']}:contrast={p['contrast']}:saturation={p['saturation']}"]
    look = look_filter(p["look"], p["look_strength"])
    if look:
        parts.append(look)
    parts.append(f"setpts=PTS/{p['speed']}")
    return ",".join(parts)


def make_copy(src: Path, out: Path, seed: int, look: str = "") -> dict:
    """Копия готового ролика с небольшими отличиями и без служебных данных файла."""
    from .top import has_audio
    p = copy_params(seed, look)
    cmd = ["ffmpeg", "-y", "-v", "error", "-i", str(src), "-vf", copy_filter(p)]
    if has_audio(src):
        cmd += ["-af", f"atempo={p['speed']}", "-c:a", "aac", "-b:a", "160k"]
    run(cmd + ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", "-map_metadata", "-1",
               "-movflags", "+faststart", str(out)])
    return p


VIDEO_EXT = (".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v")
UNIQ_DEFAULTS = {
    "copies": 3, "size": "keep",  # keep — как в исходнике, vertical — 1080x1920, square — 1080x1080
    "zoom_min": 1.0, "zoom_max": 1.04, "speed_min": 0.98, "speed_max": 1.02,
    "brightness": 0.02, "contrast": 0.04, "saturation": 0.05,
    "look": "", "look_strength": 0.5, "denoise": 0.0, "sharpen": 0.0,
    "strip_metadata": True, "keep_audio": True,
    "background": "", "opacity": 1.0, "scale": 0.85, "position": "center",  # ролик поверх своего фона
}
SIZES = {"vertical": (1080, 1920), "square": (1080, 1080), "wide": (1920, 1080)}


def uniq_params(cfg: dict, rnd: random.Random) -> dict:
    """Случайные значения одной копии в заданных пределах."""
    return {"zoom": round(rnd.uniform(cfg["zoom_min"], max(cfg["zoom_min"], cfg["zoom_max"])), 4),
            "speed": round(rnd.uniform(cfg["speed_min"], max(cfg["speed_min"], cfg["speed_max"])), 4),
            "brightness": round(rnd.uniform(-cfg["brightness"], cfg["brightness"]), 4),
            "contrast": round(1 + rnd.uniform(-cfg["contrast"], cfg["contrast"]), 4),
            "saturation": round(1 + rnd.uniform(-cfg["saturation"], cfg["saturation"]), 4),
            "x": round(rnd.random(), 3), "y": round(rnd.random(), 3)}


def uniq_command(src: Path, out: Path, cfg: dict, p: dict, audio: bool) -> list[str]:
    """Команда ffmpeg для одной копии."""
    size = SIZES.get(cfg["size"])
    tone = [f"eq=brightness={p['brightness']}:contrast={p['contrast']}:saturation={p['saturation']}"]
    tone += clean_filters(cfg["denoise"], cfg["sharpen"])
    if look_filter(cfg["look"], cfg["look_strength"]):
        tone.append(look_filter(cfg["look"], cfg["look_strength"]))
    tone.append(f"setpts=PTS/{p['speed']}")
    zoom = [f"scale=ceil(iw*{p['zoom']}/2)*2:ceil(ih*{p['zoom']}/2)*2",
            f"crop=trunc(iw/{p['zoom']}/2)*2:trunc(ih/{p['zoom']}/2)*2:(iw-ow)*{p['x']}:(ih-oh)*{p['y']}"]
    cmd = ["ffmpeg", "-y", "-v", "error", "-i", str(src)]
    background = Path(cfg["background"]) if cfg["background"] else None
    if background and background.is_file():
        W, H = size or (1080, 1920)
        still = background.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp")
        cmd += (["-loop", "1"] if still else ["-stream_loop", "-1"]) + ["-i", str(background)]
        fw, fh = int(W * cfg["scale"]) // 2 * 2, int(H * cfg["scale"]) // 2 * 2
        y = {"top": "0", "bottom": "H-h"}.get(cfg["position"], "(H-h)/2")
        graph = (f"[1:v]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},setsar=1[bg];"
                 f"[0:v]{','.join(zoom + tone)},scale={fw}:{fh}:force_original_aspect_ratio=decrease,format=rgba,"
                 f"colorchannelmixer=aa={max(0.1, min(1.0, cfg['opacity'])):.3f}[fg];"
                 f"[bg][fg]overlay=(W-w)/2:{y}:shortest=1,format=yuv420p[v]")
        cmd += ["-filter_complex", graph, "-map", "[v]"] + (["-map", "0:a?"] if cfg["keep_audio"] else [])
    else:
        fit = [f"scale={size[0]}:{size[1]}:force_original_aspect_ratio=increase", f"crop={size[0]}:{size[1]}"] if size else []
        cmd += ["-vf", ",".join(fit + zoom + tone + ["scale=trunc(iw/2)*2:trunc(ih/2)*2", "format=yuv420p"])]
    if cfg["keep_audio"] and audio:
        cmd += ["-af", f"atempo={p['speed']}", "-c:a", "aac", "-b:a", "160k"]
    else:
        cmd += ["-an"]
    cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-movflags", "+faststart"]
    if cfg["strip_metadata"]:
        cmd += ["-map_metadata", "-1"]
    return cmd + ["-shortest", str(out)]


def collect_videos(paths: list[str]) -> list[Path]:
    """Файлы и папки -> список видео (папки просматриваются на один уровень вглубь)."""
    out = []
    for raw in paths:
        path = Path(raw).expanduser()
        if path.is_dir():
            out += sorted(f for f in path.iterdir() if f.suffix.lower() in VIDEO_EXT and not f.name.startswith("."))
        elif path.is_file() and path.suffix.lower() in VIDEO_EXT:
            out.append(path)
    return list(dict.fromkeys(out))
