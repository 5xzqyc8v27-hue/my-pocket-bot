"""Речевые события внутри ролика: запинки, повторы, оговорки — места для наезда камеры и мем-вставки."""
from __future__ import annotations

import re
from difflib import SequenceMatcher

from .models import Word


def _norm(text: str) -> str:
    return re.sub(r"[^\w]", "", text.lower().replace("ё", "е"))


def _inner_repeat(word: str) -> bool:
    """Слог повторился внутри слова: «рабоработория», «по-по-пошли»."""
    return len(word) >= 6 and re.search(r"(\w{2,4})\1", word) is not None


def find_stutters(words: list[Word], clip_start: float = 0.0, min_gap: float = 2.0) -> list[dict]:
    """Запинки и повторы. Возвращает время от начала ролика и вид события."""
    norm = [_norm(w.text) for w in words]
    found: list[dict] = []

    def add(i: int, kind: str) -> None:
        t = round(words[i].start - clip_start, 2)
        if t >= 0.3 and all(abs(t - f["t"]) >= min_gap for f in found):
            found.append({"t": t, "kind": kind, "text": words[i].text.strip()})

    for i, w in enumerate(norm):
        if len(w) < 2:
            continue
        if _inner_repeat(w):
            add(i, "оговорка")
        # то же слово или его начало повторяется сразу: «я я я», «секу- секунду»
        if i and len(w) >= 3 and norm[i - 1] and (w == norm[i - 1] or (len(norm[i - 1]) >= 3 and w.startswith(norm[i - 1]))):
            add(i, "повтор слова")
        # оборвал слово и начал заново: «раб… раборатория»
        if 3 <= len(w) <= 5:
            for j in range(i + 1, min(i + 4, len(norm))):
                if len(norm[j]) > len(w) + 2 and norm[j].startswith(w[:3]):
                    add(j, "запинка")
                    break
        # оговорился и поправился: «раборатория… лаборатория»
        if len(w) >= 6:
            for j in range(i + 1, min(i + 7, len(norm))):
                if norm[j] != w and len(norm[j]) >= 6 and SequenceMatcher(None, w, norm[j]).ratio() >= 0.72:
                    add(i, "оговорка")
                    break
        # повтор фразы из двух слов чуть позже: «сейчас, ребята ... сейчас, ребята»
        if i >= 3 and len(w) >= 3 and len(norm[i - 1]) >= 3:
            pair = (norm[i - 1], w)
            for j in range(max(1, i - 14), i - 1):
                if (norm[j - 1], norm[j]) == pair:
                    add(i - 1, "повтор фразы")
                    break
    return sorted(found, key=lambda f: f["t"])
