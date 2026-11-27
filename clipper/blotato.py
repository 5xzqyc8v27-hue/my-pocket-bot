"""Автовыкладка через Blotato.

Адреса и поля запросов взяты из кода официального пакета @blotato/n8n-nodes-blotato (версия 1.0.10).
Ключ читается из переменной окружения BLOTATO_API_KEY и нигде не сохраняется.
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

SERVER = os.environ.get("BLOTATO_SERVER", "https://backend.blotato.com")
UPLOAD_LIMIT = 15 * 1024 * 1024  # предел загрузки файла напрямую, из кода пакета


def api_key() -> str:
    return os.environ.get("BLOTATO_API_KEY", "").strip()


def enabled() -> bool:
    return bool(api_key())


def _send(method: str, path: str, body: dict | None = None, query: str = "") -> dict:
    req = urllib.request.Request(
        SERVER + path + (("?" + query) if query else ""), method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"blotato-api-key": api_key(), "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        raise RuntimeError(f"Blotato ответил ошибкой {exc.code}: {detail}") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise RuntimeError(f"Нет связи с Blotato: {exc}") from exc


def list_accounts(platform: str, send=_send) -> list[dict]:
    """Аккаунты, подключённые в кабинете Blotato."""
    data = send("GET", "/v2/users/me/accounts", None, f"platform={platform}")
    return [{"id": str(a["id"]), "name": a.get("fullname") or a.get("username") or str(a["id"])}
            for a in data.get("items", [])]


def target(platform: str, title: str) -> dict:
    """Настройки площадки для поста. Значения по умолчанию — обычная публичная публикация."""
    if platform == "tiktok":
        return {"targetType": "tiktok", "privacyLevel": "PUBLIC_TO_EVERYONE", "disabledComments": False,
                "disabledDuet": False, "disabledStitch": False, "isBrandedContent": False, "isYourBrand": False,
                "isAiGenerated": False, "autoAddMusic": False, "isDraft": False}
    if platform == "youtube":
        return {"targetType": "youtube", "title": (title or "Shorts")[:100], "privacyStatus": "public",
                "shouldNotifySubscribers": False, "isMadeForKids": False, "containsSyntheticMedia": False}
    if platform == "instagram":
        return {"targetType": "instagram", "mediaType": "reel"}
    raise RuntimeError(f"Площадка {platform} не поддерживается")


def post_body(platform: str, account_id: str, text: str, media_url: str, title: str = "") -> dict:
    return {"post": {"target": target(platform, title),
                     "content": {"platform": platform, "text": text, "mediaUrls": [media_url]},
                     "accountId": account_id},
            "useNextFreeSlot": False}


def _fit(video: Path, tmp: Path) -> Path:
    """Если ролик больше предела загрузки — пережимаем копию под нужный размер."""
    if video.stat().st_size <= UPLOAD_LIMIT * 0.95:
        return video
    dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                                str(video)], capture_output=True, text=True, check=True).stdout.strip())
    kbps = int(UPLOAD_LIMIT * 0.85 * 8 / dur / 1000) - 128
    small = tmp / "upload.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(video), "-c:v", "libx264", "-preset", "veryfast",
                    "-b:v", f"{max(kbps, 800)}k", "-maxrate", f"{max(kbps, 800)}k", "-bufsize", "4M",
                    "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(small)], check=True)
    return small


def upload(video: Path, send=_send) -> str:
    """Загружает ролик в Blotato и возвращает его адрес для поста."""
    with tempfile.TemporaryDirectory() as tmp:
        data = _fit(video, Path(tmp)).read_bytes()
    if len(data) > UPLOAD_LIMIT:
        raise RuntimeError("Ролик слишком большой для загрузки в Blotato даже после сжатия")
    reply = send("POST", "/v2/media", {"url": "data:video/mp4;base64," + base64.b64encode(data).decode()})
    url = reply.get("url")
    if not url:
        raise RuntimeError(f"Blotato не вернул адрес загруженного ролика: {str(reply)[:200]}")
    return url


def publish(video: Path, platform: str, account_id: str, text: str, title: str = "", send=_send) -> str:
    """Загружает ролик и создаёт пост. Возвращает номер заявки, по которому проверяется статус."""
    reply = send("POST", "/v2/posts", post_body(platform, account_id, text, upload(video, send), title))
    sub = reply.get("postSubmissionId") or reply.get("id")
    if not sub:
        raise RuntimeError(f"Blotato не вернул номер заявки: {str(reply)[:200]}")
    return str(sub)


def status(submission_id: str, send=_send) -> dict:
    """Статус заявки: published, failed или ещё в работе."""
    reply = send("GET", f"/v2/posts/{submission_id}")
    raw = str(reply.get("status", "")).lower()
    state = "published" if "publish" in raw and "fail" not in raw else "failed" if "fail" in raw or "error" in raw else "working"
    return {"state": state, "url": reply.get("publicUrl") or reply.get("url") or "",
            "error": str(reply.get("errorMessage") or reply.get("error") or "")[:300], "raw": raw}
