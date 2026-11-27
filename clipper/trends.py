"""Мониторинг: что набирает просмотры у игровых блогеров и по темам за 24 часа, 48 часов и месяц."""
from __future__ import annotations

import json
import subprocess
import time

WINDOWS = {"24h": 86400, "48h": 172800, "30d": 2592000}


def source_url(kind: str, value: str) -> str:
    """kind: channel — ссылка на канал или его вкладку; topic — поисковый запрос по YouTube."""
    if kind == "topic":
        return f"ytsearchdate40:{value}"
    url = value.strip()
    if url.startswith("@"):
        url = "https://www.youtube.com/" + url
    if "youtube.com" in url and not url.rstrip("/").endswith(("/shorts", "/videos", "/streams")):
        url = url.rstrip("/") + "/shorts"
    return url


def fetch_entries(url: str, limit: int = 40) -> list[dict]:
    """Список последних роликов источника с просмотрами. Нужен yt-dlp и доступ в интернет."""
    proc = subprocess.run(
        ["yt-dlp", "--flat-playlist", "--dump-single-json", "--playlist-end", str(limit),
         "--extractor-args", "youtubetab:approximate_date", url],
        capture_output=True, text=True, timeout=180)
    if proc.returncode != 0:
        raise RuntimeError(f"yt-dlp не смог прочитать источник: {proc.stderr.strip()[-300:]}")
    return parse_entries(json.loads(proc.stdout))


def parse_entries(data: dict) -> list[dict]:
    out = []
    for e in data.get("entries") or []:
        if not e or not e.get("id"):
            continue
        ts = e.get("timestamp") or e.get("release_timestamp")
        if not ts and e.get("upload_date"):
            ts = time.mktime(time.strptime(e["upload_date"], "%Y%m%d"))
        out.append({"id": e["id"], "title": e.get("title") or "", "views": int(e.get("view_count") or 0),
                    "ts": float(ts) if ts else None, "duration": e.get("duration"),
                    "url": e.get("url") or e.get("webpage_url") or f"https://www.youtube.com/watch?v={e['id']}",
                    "channel": e.get("channel") or e.get("uploader") or data.get("channel") or data.get("title") or ""})
    return out


def merge(videos: dict, entries: list[dict], source: str, now: float | None = None) -> None:
    """Обновляет базу роликов и добавляет снимок просмотров — по снимкам считается рост."""
    now = now or time.time()
    for e in entries:
        v = videos.setdefault(e["id"], {**e, "source": source, "first_seen": now, "snaps": []})
        v.update(title=e["title"] or v["title"], views=e["views"], ts=e["ts"] or v.get("ts"))
        v["snaps"] = [*v["snaps"], [now, e["views"]]][-60:]


def ranking(videos: dict, window: str, now: float | None = None, limit: int = 50) -> list[dict]:
    """Ролики, вышедшие за окно, по просмотрам; плюс прирост за окно, если есть два снимка."""
    now = now or time.time()
    span = WINDOWS[window]
    rows = []
    for v in videos.values():
        born = v.get("ts") or v["first_seen"]
        age = max(now - born, 600)
        old = [s for s in v["snaps"] if s[0] >= now - span]
        growth = v["views"] - old[0][1] if len(old) >= 2 else None
        if age <= span or (growth or 0) > 0:
            rows.append({**{k: v[k] for k in ("id", "title", "views", "url", "channel", "source", "duration")},
                         "age_hours": round(age / 3600, 1), "per_hour": round(v["views"] / (age / 3600)),
                         "growth": growth, "fresh": age <= span, "date_known": bool(v.get("ts"))})
    rows.sort(key=lambda r: (r["growth"] or 0, r["views"] if r["fresh"] else 0), reverse=True)
    if not any(r["growth"] for r in rows):
        rows.sort(key=lambda r: r["views"], reverse=True)
    return rows[:limit]
