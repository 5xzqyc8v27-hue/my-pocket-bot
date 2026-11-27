"""Импорт пака монтажёра из общей папки Google Диска: файлы раскладываются по разделам материалов."""
from __future__ import annotations

import os
import re
from pathlib import Path

AUDIO = (".mp3", ".wav", ".m4a", ".aac", ".ogg")
VIDEO = (".mp4", ".mov", ".webm")
IMAGE = (".png", ".jpg", ".jpeg", ".webp")
FONT = (".ttf", ".otf")
KIND_NAMES = {"memes": "Мем-вставки", "sounds": "Звуки", "music": "Музыка", "fonts": "Шрифты",
              "fillers": "Фоны и залипалки", "emoji": "Иконки и стикеры"}


def folder_id(url: str) -> str:
    m = re.search(r"/folders/([\w-]+)", url) or re.search(r"[?&]id=([\w-]+)", url)
    if not m:
        raise RuntimeError("Нужна ссылка на папку Google Диска вида drive.google.com/drive/folders/…")
    return m.group(1)


def classify(path: str) -> str | None:
    """В какой раздел материалов положить файл пака. None — файл не нужен панели."""
    low, ext = path.lower(), os.path.splitext(path)[1].lower()
    name = os.path.basename(path)
    if name.startswith("._") or name == ".DS_Store":
        return None
    if ext in FONT:
        return "fonts"
    if ext in AUDIO:
        return "music" if "музык" in low or "music" in low or "фонк" in low else "sounds"
    if ext in VIDEO:
        return "fillers" if "фон" in low or "background" in low else "memes"
    if ext in IMAGE:
        if any(skip in low for skip in ("буквы", "текстур", "дым", "молни", "картон", "фоны для моушна")):
            return None  # заготовки для ручного моушн-дизайна, в автомонтаже не используются
        return "emoji"
    return None  # 3D-модели, пресеты After Effects, документы


def target_name(path: str) -> str:
    """Имя файла в материалах: подпапка + название, без служебных меток — по нему подбираются вставки."""
    parts = path.split("/")
    stem, ext = os.path.splitext(parts[-1])
    stem = re.sub(r"\[[^\]]*\]", " ", stem)
    sub = parts[-2] if len(parts) >= 3 else ""
    full = f"{sub} {stem}" if sub and sub.lower() not in stem.lower() else stem
    full = re.sub(r"[^\w]+", "_", full, flags=re.UNICODE).strip("_")[:70] or "file"
    return full + ext.lower()


def list_pack(url: str) -> list[dict]:
    import gdown
    files = gdown.download_folder(id=folder_id(url), output="pack", quiet=True, skip_download=True)
    rows = []
    for f in files:
        path = str(getattr(f, "path", f)).replace("pack/", "", 1)
        kind = classify(path)
        if kind:
            rows.append({"id": getattr(f, "id", ""), "path": path, "kind": kind, "name": target_name(path)})
    return rows


def fetch_file(file_id: str, target: Path) -> bool:
    """Скачивает один файл с Диска по прямому адресу. False — не удалось (нет доступа, лимит Диска)."""
    import urllib.error
    import urllib.request
    url = f"https://drive.usercontent.google.com/download?id={file_id}&export=download&confirm=t"
    tmp = target.with_suffix(target.suffix + ".part")
    try:
        with urllib.request.urlopen(url, timeout=600) as resp, open(tmp, "wb") as out:
            if "text/html" in resp.headers.get("Content-Type", ""):
                return False  # вместо файла пришла страница: доступ закрыт или превышен лимит скачиваний
            while chunk := resp.read(1 << 20):
                out.write(chunk)
        if tmp.stat().st_size == 0:
            return False
        tmp.replace(target)
        return True
    except (urllib.error.URLError, OSError):
        return False
    finally:
        tmp.unlink(missing_ok=True)


def import_pack(url: str, assets_dir: Path, owner: str, kinds: list[str], progress=None, limit: int = 1500) -> dict:
    """Скачивает выбранные разделы пака в материалы. Уже скачанные файлы пропускаются."""
    rows = [r for r in list_pack(url) if r["kind"] in kinds][:limit]
    done = skipped = failed = 0
    for n, r in enumerate(rows, 1):
        target = assets_dir / owner / r["kind"] / r["name"]
        if target.exists() and target.stat().st_size > 0:
            skipped += 1
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            if fetch_file(r["id"], target):
                done += 1
            else:
                failed += 1
        if progress:
            progress(n, len(rows), r["name"])
    return {"total": len(rows), "downloaded": done, "skipped": skipped, "failed": failed}
