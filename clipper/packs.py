"""Импорт пака монтажёра из общей папки Google Диска или Яндекс Диска: файлы раскладываются по разделам материалов.

Zip-архивы внутри пака распаковываются; программы, пресеты After Effects и файлы крупнее 80 МБ пропускаются."""
from __future__ import annotations

import os
import re
from pathlib import Path

AUDIO = (".mp3", ".wav", ".m4a", ".aac", ".ogg")
VIDEO = (".mp4", ".mov", ".webm")
IMAGE = (".png", ".jpg", ".jpeg", ".webp")
FONT = (".ttf", ".otf")
KIND_NAMES = {"memes": "Мем-вставки", "sounds": "Звуки", "music": "Музыка", "fonts": "Шрифты",
              "fillers": "Фоны и залипалки", "emoji": "Иконки и стикеры",
              "effects": "Эффекты и переходы", "luts": "Цветокор (LUT)"}
MAX_FILE = 80 << 20  # файлы крупнее не качаем: это исходники для ручного монтажа
CAPS = {"luts": 60, "effects": 150, "fillers": 60}  # сколько файлов раздела брать из одного пака
YANDEX_API = "https://cloud-api.yandex.net/v1/disk/public/resources"


def is_yandex(url: str) -> bool:
    return bool(re.search(r"(disk\.yandex\.\w+|yadi\.sk)/", url))


def check_url(url: str) -> None:
    if not is_yandex(url):
        folder_id(url)


def folder_id(url: str) -> str:
    m = re.search(r"/folders/([\w-]+)", url) or re.search(r"[?&]id=([\w-]+)", url)
    if not m:
        raise RuntimeError("Нужна ссылка на общую папку: drive.google.com/drive/folders/… или disk.yandex.ru/d/…")
    return m.group(1)


def classify(path: str) -> str | None:
    """В какой раздел материалов положить файл пака. None — файл не нужен панели."""
    low, ext = path.lower(), os.path.splitext(path)[1].lower()
    name = os.path.basename(path)
    if name.startswith("._") or name == ".DS_Store":
        return None
    if any(skip in low for skip in ("софт", "плагин", "инструмент", "3d ", "/3d")):
        return None  # программы и пресеты After Effects панели не нужны
    if ext == ".zip":
        return "zip"  # архив распаковывается при импорте, его файлы раскладываются по тем же правилам
    if ext in FONT:
        return "fonts"
    if ext == ".cube":
        return "luts"
    if "эмодзи" in low or "emoji" in low:
        # берём эмодзи с понятным именем: по имени они подбираются к смыслу момента
        named = not re.match(r"(animatedemoji|vid_|img_|\d+$)", os.path.splitext(name)[0].lower())
        return "emoji" if named and ext in (".mov", ".png", ".webp", ".webm") else None  # gif — без прозрачности
    if ext in VIDEO and any(w in low for w in ("эффект", "переход", "глитч", "вспышк", "overlay", "transition", "glitch")):
        return "effects"
    if any(w in low for w in ("футаж", "анимаци", "графика")):
        return None  # заготовки для ручного монтажа
    if ext in AUDIO:
        return "music" if "музык" in low or "music" in low or "фонк" in low else "sounds"
    if ext in VIDEO:
        return "fillers" if "фон" in low or "background" in low else "memes"
    if ext in IMAGE:
        if ext in (".jpg", ".jpeg") or os.path.splitext(name)[0].strip("() _-").isdigit():
            return None  # без прозрачности или без названия — в авто-вставки не годится
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


def _yandex_json(url: str, **params) -> dict:
    import json
    import urllib.parse
    import urllib.request
    tail = params.pop("_tail", "")
    query = urllib.parse.urlencode({"public_key": url, **params})
    with urllib.request.urlopen(f"{YANDEX_API}{tail}?{query}", timeout=60) as resp:
        return json.load(resp)


def list_yandex(url: str) -> list[dict]:
    """Все файлы общей папки Яндекс Диска (обход подпапок)."""
    rows, todo = [], ["/"]
    while todo:
        folder, offset = todo.pop(), 0
        while True:
            data = _yandex_json(url, path=folder, limit=1000, offset=offset)
            if data.get("type") == "file":  # ссылка на один файл
                return [{"path": data["name"], "size": data.get("size", 0), "id": "/"}]
            items = data.get("_embedded", {}).get("items", [])
            for it in items:
                if it["type"] == "dir":
                    if classify(it["path"] + "/x.mp4") or classify(it["path"] + "/x.png") or classify(it["path"] + "/x.cube"):
                        todo.append(it["path"])  # папки с софтом и заготовками не обходим
                else:
                    rows.append({"path": it["path"].lstrip("/"), "size": it.get("size", 0), "id": it["path"]})
            offset += len(items)
            if len(items) < 1000:
                break
    return rows


def list_pack(url: str) -> list[dict]:
    if is_yandex(url):
        rows, seen, used = [], set(), {}
        for f in sorted(list_yandex(url), key=lambda r: r["path"]):
            kind = classify(f["path"])
            name = target_name(f["path"]) if kind else ""
            if not kind or f["size"] > MAX_FILE or (kind, name) in seen:
                continue
            if used.get(kind, 0) >= CAPS.get(kind, 10 ** 6):
                continue
            seen.add((kind, name))
            used[kind] = used.get(kind, 0) + 1
            rows.append({"id": f["id"], "path": f["path"], "kind": kind, "name": name, "yandex": url})
        return rows
    import gdown
    files = gdown.download_folder(id=folder_id(url), output="pack", quiet=True, skip_download=True)
    rows = []
    for f in files:
        path = str(getattr(f, "path", f)).replace("pack/", "", 1)
        kind = classify(path)
        if kind:
            rows.append({"id": getattr(f, "id", ""), "path": path, "kind": kind, "name": target_name(path)})
    return rows


def fetch_file(file_id: str, target: Path, yandex: str = "", limit: int = MAX_FILE) -> bool:
    """Скачивает один файл с Диска по прямому адресу. False — не удалось (нет доступа, лимит Диска)."""
    import urllib.error
    import urllib.request
    url = f"https://drive.usercontent.google.com/download?id={file_id}&export=download&confirm=t"
    tmp = target.with_suffix(target.suffix + ".part")
    try:
        if yandex:
            url = _yandex_json(yandex, path=file_id, _tail="/download")["href"]
        with urllib.request.urlopen(url, timeout=600) as resp, open(tmp, "wb") as out:
            if "text/html" in resp.headers.get("Content-Type", ""):
                return False  # вместо файла пришла страница: доступ закрыт или превышен лимит скачиваний
            if int(resp.headers.get("Content-Length") or 0) > limit:
                return False  # слишком большой файл — это исходник, а не материал для вставок
            size = 0
            while chunk := resp.read(1 << 20):
                out.write(chunk)
                size += len(chunk)
                if size > limit:
                    return False
        if tmp.stat().st_size == 0:
            return False
        tmp.replace(target)
        return True
    except (urllib.error.URLError, OSError, KeyError, ValueError):
        return False
    finally:
        tmp.unlink(missing_ok=True)


def compact_emoji(src: Path, target: Path) -> bool:
    """Анимированный эмодзи с прозрачностью (ProRes .mov, десятки МБ) -> лёгкий .webm с тем же альфа-каналом."""
    import subprocess
    try:
        ok = subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(src), "-vf", "scale=360:-2,fps=30", "-an",
                             "-c:v", "libvpx-vp9", "-pix_fmt", "yuva420p", "-b:v", "0", "-crf", "32",
                             str(target)], timeout=300).returncode == 0
    except (OSError, subprocess.SubprocessError):
        ok = False
    if not ok:
        target.unlink(missing_ok=True)
    return ok


def unpack_zip(archive: Path, assets_dir: Path, owner: str, kinds: list[str]) -> int:
    """Раскладывает файлы архива по разделам материалов. Программы и прочее лишнее пропускаются."""
    import zipfile
    added = 0
    with zipfile.ZipFile(archive) as z:
        for item in z.infolist():
            if item.is_dir() or item.file_size > MAX_FILE:
                continue
            name = item.filename
            try:  # русские имена в архивах часто записаны в старой кодировке
                name = name.encode("cp437").decode("utf-8")
            except (UnicodeEncodeError, UnicodeDecodeError):
                pass
            kind = classify(name)
            if kind in (None, "zip"):
                continue
            target = assets_dir / owner / kind / target_name(name)
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                with z.open(item) as src, open(target, "wb") as dst:
                    while chunk := src.read(1 << 20):
                        dst.write(chunk)
            if kind == "memes" and is_sticker(target):  # короткий квадратный ролик на зелёном фоне — это эмодзи
                kind, moved = "emoji", assets_dir / owner / "emoji" / target.name
                moved.parent.mkdir(parents=True, exist_ok=True)
                target.replace(moved)
            if kind in kinds:
                added += 1
            else:
                (assets_dir / owner / kind / target_name(name)).unlink(missing_ok=True)
    return added


def is_sticker(video: Path) -> bool:
    """Анимированный эмодзи: квадратный ролик до 6 секунд на сплошном зелёном или синем фоне."""
    import subprocess
    from .render import screen_key
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                              "stream=width,height:format=duration", "-of", "csv=p=0", str(video)],
                             capture_output=True, text=True, timeout=30).stdout.split()
        w, h = (int(x) for x in out[0].split(",")[:2])
        return abs(w - h) <= 8 and float(out[1]) <= 6 and bool(screen_key(video))
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        return False


def import_pack(url: str, assets_dir: Path, owner: str, kinds: list[str], progress=None, limit: int = 1500) -> dict:
    """Скачивает выбранные разделы пака в материалы. Уже скачанные файлы пропускаются."""
    rows = [r for r in list_pack(url) if r["kind"] in kinds or r["kind"] == "zip"][:limit]
    done = skipped = failed = 0
    for n, r in enumerate(rows, 1):
        if r["kind"] == "zip":
            import tempfile
            with tempfile.TemporaryDirectory() as tmp:
                archive = Path(tmp) / "pack.zip"
                if progress:
                    progress(n, len(rows), f"архив {r['name']} — скачиваю и распаковываю")
                if fetch_file(r["id"], archive, r.get("yandex", ""), limit=3 << 30):
                    try:
                        done += unpack_zip(archive, assets_dir, owner, kinds)
                    except Exception:
                        failed += 1
                else:
                    failed += 1
            continue
        target = assets_dir / owner / r["kind"] / r["name"]
        shrink = r["kind"] == "emoji" and target.suffix == ".mov"
        final = target.with_suffix(".webm") if shrink else target
        if final.exists() and final.stat().st_size > 0:
            skipped += 1
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            ok = fetch_file(r["id"], target, r.get("yandex", ""))
            if ok and shrink:
                ok = compact_emoji(target, final)
                target.unlink(missing_ok=True)
            done, failed = done + int(ok), failed + int(not ok)
        if progress:
            progress(n, len(rows), r["name"])
    return {"total": len(rows), "downloaded": done, "skipped": skipped, "failed": failed}
