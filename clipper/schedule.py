"""Расписание публикаций: равномерные слоты по дням для каждого аккаунта."""
from __future__ import annotations

import time


def day_slots(day_start: float, per_day: int, from_hour: int, to_hour: int) -> list[float]:
    """Времена публикаций в течение дня, равномерно между from_hour и to_hour (местное время)."""
    per_day = max(1, per_day)
    span = max(1, to_hour - from_hour) * 3600
    step = span / per_day
    return [day_start + from_hour * 3600 + step * (i + 0.5) for i in range(per_day)]


def next_slots(taken: list[float], count: int, per_day: int = 2, from_hour: int = 10, to_hour: int = 22,
               now: float | None = None) -> list[float]:
    """Ближайшие свободные слоты для одного аккаунта. taken — уже занятые времена."""
    now = now or time.time()
    lt = time.localtime(now)
    day = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
    busy = {round(t) for t in taken}
    out: list[float] = []
    for _ in range(366):
        for slot in day_slots(day, per_day, from_hour, to_hour):
            if slot > now + 300 and round(slot) not in busy:
                out.append(slot)
                if len(out) == count:
                    return out
        day += 86400
    return out
