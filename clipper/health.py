"""Проверки: не режет ли площадка охваты аккаунту и насколько «чистый» текущий IP-адрес."""
from __future__ import annotations

import json
import statistics
import time
import urllib.error
import urllib.request


def account_url(platform: str, handle: str) -> str:
    name = handle.strip().lstrip("@")
    if platform == "youtube":
        return f"https://www.youtube.com/@{name}/shorts"
    if platform == "tiktok":
        return f"https://www.tiktok.com/@{name}"
    raise RuntimeError("Проверка охватов доступна для YouTube и TikTok")


def reach_verdict(entries: list[dict], now: float | None = None) -> dict:
    """Сравнивает просмотры последних роликов с обычным уровнем аккаунта.

    Площадки не сообщают о теневом бане — это оценка по косвенному признаку (резкий провал просмотров).
    """
    now = now or time.time()
    posts = sorted((e for e in entries if e.get("views") is not None), key=lambda e: -(e.get("ts") or 0))
    settled = [e for e in posts if not e.get("ts") or now - e["ts"] > 6 * 3600]  # свежим роликам рано судить
    views = [e["views"] for e in settled]
    base = {"posts": len(posts), "recent": views[:3], "checked_at": now}
    if len(views) < 6:
        return {**base, "level": "unknown", "verdict": "Мало роликов для оценки — нужно хотя бы 6 вышедших"}
    recent, usual = statistics.median(views[:3]), statistics.median(views[3:20])
    base.update(recent_median=int(recent), usual_median=int(usual))
    if max(views[:3]) < 20 and usual >= 100:
        return {**base, "level": "bad", "verdict": "Последние ролики почти не показываются — похоже на ограничение"}
    if usual >= 300 and recent < usual * 0.2:
        return {**base, "level": "warn",
                "verdict": f"Просмотры упали в {usual / max(recent, 1):.0f} раз против обычного — возможно ограничение охватов"}
    if usual < 50 and recent < 50:
        return {**base, "level": "warn", "verdict": "Аккаунт стабильно набирает очень мало — ролики могут не попадать в рекомендации"}
    return {**base, "level": "ok", "verdict": "Просмотры в обычном диапазоне, признаков ограничения нет"}


def parse_ip(data: dict) -> dict:
    """Ответ сервиса ip-api.com -> понятная оценка адреса."""
    if data.get("status") != "success":
        raise RuntimeError(f"Сервис проверки IP ответил ошибкой: {data.get('message', 'неизвестно')}")
    flags = []
    if data.get("hosting"):
        flags.append("адрес дата-центра (хостинг)")
    if data.get("proxy"):
        flags.append("определяется как VPN, прокси или Tor")
    kind = "мобильный оператор" if data.get("mobile") else "хостинг" if data.get("hosting") else "домашний или офисный провайдер"
    level = "bad" if data.get("proxy") or data.get("hosting") else "ok"
    verdict = ("Адрес выглядит как обычный пользовательский" if level == "ok"
               else "Площадки относятся к таким адресам с подозрением: " + ", ".join(flags))
    return {"ip": data.get("query", ""), "country": data.get("country", ""), "city": data.get("city", ""),
            "provider": data.get("isp") or data.get("org", ""), "kind": kind, "level": level, "verdict": verdict}


def check_ip() -> dict:
    """Проверяет текущий внешний IP-адрес компьютера через открытый сервис ip-api.com."""
    url = "http://ip-api.com/json/?fields=status,message,country,city,isp,org,mobile,proxy,hosting,query&lang=ru"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            return parse_ip(json.loads(resp.read()))
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Не удалось проверить IP: {exc}") from exc
