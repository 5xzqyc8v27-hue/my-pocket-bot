"""История трансляций канала и чат прошедших эфиров."""
from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

from .trends import parse_entries


def history_url(platform: str, channel: str) -> str:
    if platform == "twitch":
        return f"https://www.twitch.tv/{channel}/videos?filter=archives&sort=time"
    if platform == "youtube":
        base = channel if channel.startswith("http") else f"https://www.youtube.com/{channel}"
        return base.rstrip("/") + "/streams"
    raise RuntimeError("История трансляций доступна для Twitch и YouTube")


def list_vods(platform: str, channel: str, limit: int = 30) -> list[dict]:
    """Прошедшие трансляции: название, дата, длительность, просмотры, ссылка."""
    proc = subprocess.run(["yt-dlp", "--flat-playlist", "--dump-single-json", "--playlist-end", str(limit),
                           "--extractor-args", "youtubetab:approximate_date", history_url(platform, channel)],
                          capture_output=True, text=True, timeout=180)
    if proc.returncode != 0:
        raise RuntimeError(f"Не удалось получить историю: {proc.stderr.strip()[-300:]}")
    return parse_entries(json.loads(proc.stdout))


def parse_rechat(data) -> list[dict]:
    """Чат прошедшей трансляции Twitch (формат rechat из yt-dlp) -> наши сообщения."""
    out = []
    for c in data if isinstance(data, list) else data.get("comments", []):
        try:
            out.append({"t": float(c["content_offset_seconds"]),
                        "user": c.get("commenter", {}).get("display_name") or c.get("commenter", {}).get("name", ""),
                        "text": c["message"]["body"]})
        except (KeyError, TypeError, ValueError):
            continue
    return out


def fetch_vod_chat(url: str, video: Path) -> int:
    """Скачивает чат прошедшей трансляции и кладёт рядом с видео. Возвращает число сообщений."""
    base = video.with_suffix("")
    subprocess.run(["yt-dlp", "--skip-download", "--write-subs", "--sub-langs", "rechat",
                    "-o", str(base) + ".%(ext)s", url], capture_output=True, timeout=1800)
    found = list(video.parent.glob(base.name + "*rechat*.json"))
    if not found:
        return 0
    chat = parse_rechat(json.loads(found[0].read_text(encoding="utf-8")))
    video.with_suffix(".chat.jsonl").write_text(
        "\n".join(json.dumps(m, ensure_ascii=False) for m in chat), encoding="utf-8")
    return len(chat)
