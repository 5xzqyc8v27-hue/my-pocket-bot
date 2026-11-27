"""Правила конкурса стримера — подсказки, а не запреты.

Панель ничего не блокирует и не переделывает: она делает, что просили, и предупреждает, если ролик
может не пройти по правилам. Сама дописывает только обязательную ссылку в подпись.
"""
from __future__ import annotations

import time

ZLOY = {
    "rule_link": "twitch: ZLOYn",
    "rule_no_split": True,   # п. 2.3: лицо + отвлекающее видео во второй части экрана не принимаются
    "rule_no_ads": True,     # пп. 3.2, 4.5, 4.10: никакой рекламы в ролике и подписи
    "rule_avoid": ("запрещённые вещества, политика, религия, дискриминация, реклама казино, букмекеров и "
                   "сторонних проектов, всё, что очерняет или искажает слова стримера, реакции на фильмы и аниме"),
    "rule_days": 14,         # принимаются нарезки не старше 14 дней
    "rule_tiers": "100000:25:7.5, 500000:115:20, 1000000:225:45",  # просмотры:нарезка $:мем-видео $
}


_UPG = {
    "rule_link": "", "rule_no_split": False, "rule_no_ads": False, "rule_days": 0, "rule_tiers": "",
    "rule_avoid": "контент не Олега Калатая (исключение — Гамос и Вамос на проекте Inkmate)",
    "rule_ad_len": "15-60",  # ролик с баннером: от 15 секунд до минуты, баннер посередине, видео на паузе
}
# оффер UPG под нарезки Kalatay: формат:$ за 1000 просмотров:минимум просмотров для выплаты
UPG_L1 = {**_UPG, "rule_rates": "вебка:0.7:6000, банер:0.2:10000"}
UPG_L2 = {**_UPG, "rule_rates": "вебка:1:6000, банер:0.3:10000"}
TEMPLATES = {"zloy": ZLOY, "upg1": UPG_L1, "upg2": UPG_L2}


def parse_rates(text: str) -> dict[str, tuple[float, int]]:
    out = {}
    for part in (text or "").split(","):
        bits = [b.strip() for b in part.split(":")]
        try:
            out[bits[0].lower()] = (float(bits[1]), int(bits[2]) if len(bits) > 2 else 0)
        except (ValueError, IndexError):
            continue
    return out


def payout_rate(views: int, rates_text: str, banner: bool) -> dict:
    """Оплата за просмотры: ставка за 1000 и минимальный порог, отдельно для роликов с баннером и без."""
    rates = parse_rates(rates_text)
    if not rates:
        return {"amount": 0, "advice": "Ставки не заданы"}
    names = list(rates)
    pick = next((n for n in names if ("бан" in n or "ban" in n) == banner), names[0])
    rate, floor = rates[pick]
    amount = round(views / 1000 * rate, 2)
    if views < floor:
        return {"amount": 0, "format": pick, "next": floor, "level": "",
                "advice": f"формат «{pick}»: до порога {floor} просмотров ролик не оплачивается, осталось {floor - views}"}
    return {"amount": amount, "format": pick, "level": "good",
            "advice": f"формат «{pick}»: {rate:g} $ за 1000 просмотров — можно подавать на выплату"}


def ad_len_note(total: float, limits: str) -> str:
    """Предупреждение, если ролик с баннером не укладывается в длину из правил («15-60»)."""
    try:
        low, high = (float(x) for x in limits.split("-"))
    except ValueError:
        return ""
    if total < low:
        return f"ролик с баннером короче {low:g} с ({total:.0f} с) — по правилам оффера могут не принять"
    if total > high:
        return f"ролик с баннером длиннее {high:g} с ({total:.0f} с) — по правилам оффера могут не принять"
    return ""


def parse_tiers(text: str) -> list[tuple[int, float, float]]:
    out = []
    for part in (text or "").split(","):
        bits = part.strip().split(":")
        try:
            out.append((int(bits[0]), float(bits[1]), float(bits[2]) if len(bits) > 2 else float(bits[1])))
        except (ValueError, IndexError):
            continue
    return sorted(out)


def with_link(caption: str, link: str) -> str:
    """Обязательная ссылка на стримера в подписи — добавляем, если её ещё нет."""
    link = (link or "").strip()
    if not link or link.lower() in caption.lower():
        return caption
    return (caption.strip() + " " + link).strip()


def payout(views: int, published_at: float, tiers_text: str, days: int, meme: bool = False,
           now: float | None = None) -> dict:
    """Какая выплата положена сейчас и стоит ли ждать следующего порога.

    Выплата одноразовая, поэтому если ролик по текущему темпу успевает дорасти до следующего порога
    до конца срока приёма — выгоднее подождать.
    """
    now = now or time.time()
    tiers = parse_tiers(tiers_text)
    if not tiers:
        return {"amount": 0, "advice": "Пороги выплат не заданы"}
    age_days = max((now - published_at) / 86400, 0.02)
    left = (days - age_days) if days else None
    col = 2 if meme else 1
    reached = [t for t in tiers if views >= t[0]]
    upcoming = [t for t in tiers if views < t[0]]
    amount = reached[-1][col] if reached else 0
    info = {"amount": amount, "tier": reached[-1][0] if reached else 0, "days_left": None if left is None else round(left, 1),
            "next": upcoming[0][0] if upcoming else None, "next_amount": upcoming[0][col] if upcoming else None}
    if left is not None and left <= 0:
        return {**info, "advice": "Срок приёма истёк" + (" — выплата могла сгореть" if amount else ""), "level": "bad"}
    if not upcoming:
        return {**info, "advice": "Максимальный порог взят — подавайте на выплату", "level": "good"}
    per_day = views / age_days
    forecast = views + per_day * left if left is not None else None
    will_reach = forecast is not None and forecast >= upcoming[0][0]
    if not reached:
        text = ("по текущему темпу дойдёт до первого порога" if will_reach or left is None
                else "по текущему темпу до первого порога не дойдёт")
        return {**info, "advice": f"Порог ещё не взят; {text}", "level": "" if will_reach or left is None else "warn"}
    if left is not None and left <= 2:
        return {**info, "advice": "Срок приёма заканчивается — подавайте сейчас", "level": "warn"}
    if will_reach:
        goal = f"{upcoming[0][0]:,}".replace(",", " ")
        return {**info, "advice": f"Порог взят, но ролик успевает дорасти до {goal} — выгоднее подождать",
                "level": "good"}
    return {**info, "advice": "Порог взят, до следующего не дотянет — подавайте на выплату", "level": "good"}
