"""Kick: записи эфиров и клипы зрителей канала — что набрало просмотры и откуда резать."""
from __future__ import annotations

import json
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

API = "https://kick.com/api/v2/channels/"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/126.0 Safari/537.36")


def slug_of(channel: str) -> str:
    """Ник канала из ссылки вида kick.com/ник или просто ник."""
    m = re.search(r"kick\.com/([\w-]+)", channel)
    return (m.group(1) if m else channel.strip().strip("/@")).lower()


def _get(url: str, start: int | None = None, length: int | None = None) -> bytes:
    headers = {"User-Agent": UA, "Accept": "application/json, */*"}
    if start is not None:
        headers["Range"] = f"bytes={start}-{start + length - 1}"
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=120) as resp:
        return resp.read()


def _json(path: str):
    try:
        return json.loads(_get(API + path))
    except (urllib.error.URLError, ValueError, OSError) as exc:
        raise RuntimeError(f"Kick не ответил: {str(exc)[:120]}") from exc


def list_videos(channel: str) -> list[dict]:
    """Записи эфиров канала, самые просматриваемые первыми."""
    rows = [{"title": v.get("session_title") or "", "views": v.get("views") or 0, "date": (v.get("created_at") or "")[:10],
             "minutes": (v.get("duration") or 0) // 60000, "url": v.get("source") or "",
             "id": (v.get("video") or {}).get("uuid", "")} for v in _json(f"{slug_of(channel)}/videos")]
    return sorted(rows, key=lambda r: -r["views"])


def list_clips(channel: str, period: str = "all", pages: int = 1) -> list[dict]:
    """Клипы зрителей по просмотрам. period: all, month, week, day."""
    out, cursor = [], ""
    for _ in range(max(1, pages)):
        data = _json(f"{slug_of(channel)}/clips?sort=view&time={period}" + (f"&cursor={cursor}" if cursor else ""))
        for c in data.get("clips", []):
            out.append({"id": c["id"], "title": c.get("title") or "", "views": c.get("views") or c.get("view_count") or 0,
                        "seconds": c.get("duration") or 0, "date": (c.get("created_at") or "")[:10],
                        "url": c.get("video_url") or c.get("clip_url") or "",
                        "page": f"https://kick.com/{slug_of(channel)}/clips/{c['id']}"})
        nxt = data.get("nextCursor")
        if not nxt:
            break
        cursor = urllib.parse.quote(json.dumps(nxt) if isinstance(nxt, dict) else str(nxt))
    return out


def download_clip(url: str, target: Path) -> Path:
    """Скачивает клип по его плейлисту (куски лежат диапазонами внутри общих файлов) и собирает в mp4."""
    if not url.endswith(".m3u8"):
        target.write_bytes(_get(url))
        return target
    base, offset, rng = url.rsplit("/", 1)[0] + "/", 0, None
    raw = target.with_suffix(".ts")
    with open(raw, "wb") as out:
        for line in _get(url).decode("utf-8", "replace").splitlines():
            line = line.strip()
            if line.startswith("#EXT-X-BYTERANGE:"):
                size, _, at = line.split(":", 1)[1].partition("@")
                rng = (int(at) if at else offset, int(size))
                offset = rng[0] + rng[1]
            elif line and not line.startswith("#"):
                out.write(_get(urllib.parse.urljoin(base, line), *rng) if rng else _get(urllib.parse.urljoin(base, line)))
                rng = None
    try:
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(raw), "-c", "copy", "-movflags", "+faststart",
                        str(target)], check=True, timeout=300)
    finally:
        raw.unlink(missing_ok=True)
    return target


PERIODS = {"24hr": "day", "7d": "week", "30d": "month", "all": "all"}


def _ts(date: str) -> int:
    import calendar
    import time
    try:
        return calendar.timegm(time.strptime(date[:10], "%Y-%m-%d"))
    except ValueError:
        return 0


def history_entries(channel: str) -> list[dict]:
    """Записи эфиров в том же виде, что и история Twitch в панели."""
    slug = slug_of(channel)
    return [{"title": v["title"], "url": f"https://kick.com/{slug}/videos/{v['id']}" if v["id"] else v["url"],
             "ts": _ts(v["date"]), "duration": v["minutes"] * 60, "views": v["views"]} for v in list_videos(channel)]


def clip_entries(channel: str, period: str = "7d") -> list[dict]:
    return [{"title": c["title"], "url": c["page"], "ts": _ts(c["date"]), "duration": c["seconds"], "views": c["views"]}
            for c in list_clips(channel, PERIODS.get(period, "week"), pages=2)]


def is_clip_url(url: str) -> bool:
    return bool(re.search(r"kick\.com/[\w-]+/clips/clip_\w+", url))


def fetch_clip(page_url: str, workdir: Path) -> Path:
    """Скачивает клип по ссылке на его страницу."""
    clip_id = re.search(r"/clips/(clip_\w+)", page_url).group(1)
    try:
        data = json.loads(_get(f"https://kick.com/api/v2/clips/{clip_id}"))
    except (urllib.error.URLError, ValueError, OSError) as exc:
        raise RuntimeError(f"Kick не отдал клип: {str(exc)[:120]}") from exc
    clip = data.get("clip") or data
    url = clip.get("video_url") or clip.get("clip_url")
    if not url:
        raise RuntimeError("У клипа нет видео — возможно, он удалён")
    workdir.mkdir(parents=True, exist_ok=True)
    return download_clip(url, workdir / f"{clip_id}.mp4")
