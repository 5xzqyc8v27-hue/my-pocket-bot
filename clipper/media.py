from __future__ import annotations

import json
import subprocess
from pathlib import Path


def run(cmd: list[str], cwd: Path | None = None) -> None:
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"Команда упала: {' '.join(cmd[:3])} ...\n{proc.stderr[-2000:]}")


def probe(path: Path) -> dict:
    """Ширина, высота и длительность видео."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=width,height:format=duration", "-of", "json", str(path)],
        capture_output=True, text=True, check=True,
    ).stdout
    data = json.loads(out)
    stream = data["streams"][0]
    return {"width": int(stream["width"]), "height": int(stream["height"]),
            "duration": float(data["format"]["duration"])}


def is_url(source: str) -> bool:
    return source.startswith(("http://", "https://"))


def fetch(source: str, workdir: Path) -> Path:
    """Локальный файл возвращаем как есть, ссылку качаем через yt-dlp."""
    if not is_url(source):
        path = Path(source).expanduser()
        if not path.exists():
            raise FileNotFoundError(f"Файл не найден: {path}")
        return path
    workdir.mkdir(parents=True, exist_ok=True)
    template = str(workdir / "%(id)s.%(ext)s")
    cmd = ["yt-dlp", "-f", "bv*[height<=1080]+ba/b[height<=1080]/b", "--merge-output-format", "mp4",
           "--no-playlist", "-o", template, "--print", "after_move:filepath", source]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"yt-dlp не смог скачать {source}:\n{proc.stderr[-1500:]}")
    return Path(proc.stdout.strip().splitlines()[-1])
