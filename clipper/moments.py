from __future__ import annotations

import json
import os
import shutil
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

import numpy as np
from pydantic import BaseModel

from .models import Moment, Transcript

DEFAULT_MODEL = os.environ.get("CLIPPER_MODEL", "claude-opus-5-5")

SYSTEM = """Ты — редактор коротких вертикальных видео (TikTok, Shorts, Reels).
Тебе дают расшифровку длинного видео с таймкодами. Найди самые сильные моменты,
которые работают как самостоятельные ролики: смешное, эмоциональное, неожиданное,
конфликт, сильная мысль, законченная история.

Правила:
- Момент должен быть понятен без контекста, начинаться с зацепки и заканчиваться
  на завершённой фразе или реакции. Границы ставь по таймкодам строк расшифровки.
- title — надпись на экране, до 6 слов, без кавычек и эмодзи, на языке видео.
- category — одно слово из списка: смешное, мем, фейл, эмоция, конфликт, скилл, цитата, история.
- hooks — ещё 2-3 варианта надписи в другом стиле (вопрос, интрига, цитата).
- score — честная оценка 0-100; слабые моменты лучше не включать совсем.
- profanity=true, если в моменте есть мат.
- Моменты не должны пересекаться."""


class _Picked(BaseModel):
    moments: list[Moment]


def _format_transcript(tr: Transcript) -> str:
    return "\n".join(f"[{s.start:.1f}-{s.end:.1f}] {s.text}" for s in tr.segments)


def _prompt(tr: Transcript, count: int, min_len: float, max_len: float, focus: str = "") -> str:
    want = f"Особенно ищи моменты такого типа: {focus}.\n" if focus.strip() else ""
    return want + (
        f"Нужно до {count} моментов длиной от {min_len:.0f} до {max_len:.0f} секунд.\n"
        f"Длительность видео: {tr.duration:.0f} с.\n\nРасшифровка:\n{_format_transcript(tr)}"
    )


JSON_RULE = (
    "\n\nОтветь только JSON-объектом без пояснений и без markdown, вида: "
    '{"moments": [{"start": 0.0, "end": 0.0, "title": "", "caption": "", '
    '"hashtags": [], "score": 0, "reason": "", "profanity": false, "category": "", "hooks": []}]}'
)


def _parse_json_reply(text: str) -> list[Moment]:
    """Достаёт JSON с моментами из текстового ответа модели."""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise RuntimeError("Модель не вернула JSON с моментами")
    try:
        return _Picked.model_validate_json(text[start:end + 1]).moments
    except ValueError as exc:
        raise RuntimeError(f"Не удалось разобрать ответ модели: {exc}") from exc


def claude_cli_available() -> bool:
    return shutil.which("claude") is not None


def pick_with_claude_cli(tr: Transcript, count: int, min_len: float, max_len: float, focus: str = "") -> list[Moment]:
    """Выбор моментов через установленный Claude Code (`claude -p`).

    Работает от подписки Claude (Pro/Max), API-кредиты не тратит.
    Нужно один раз войти: запустить `claude` и выполнить /login.
    """
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}  # иначе спишет с API
    prompt = SYSTEM + "\n\n" + _prompt(tr, count, min_len, max_len, focus) + JSON_RULE
    try:
        proc = subprocess.run(["claude", "-p", "--output-format", "json"], input=prompt,
                              capture_output=True, text=True, timeout=900, env=env)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("Claude Code не ответил за 15 минут") from exc
    if proc.returncode != 0:
        raise RuntimeError(f"Claude Code завершился с ошибкой: {(proc.stderr or proc.stdout)[-500:]}")
    text = proc.stdout
    try:
        envelope = json.loads(text)
        if isinstance(envelope, dict) and "result" in envelope:
            if envelope.get("is_error"):
                raise RuntimeError(f"Claude Code вернул ошибку: {str(envelope['result'])[:500]}")
            text = str(envelope["result"])
    except json.JSONDecodeError:
        pass
    return _clean(_parse_json_reply(text), tr.duration, min_len, max_len, count)


OLLAMA_URL = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")
OLLAMA_MODEL = os.environ.get("CLIPPER_OLLAMA_MODEL", "qwen2.5:7b")


def ollama_available() -> bool:
    try:
        with urllib.request.urlopen(f"{OLLAMA_URL}/api/tags", timeout=2):
            return True
    except (urllib.error.URLError, OSError):
        return False


def pick_with_ollama(tr: Transcript, count: int, min_len: float, max_len: float,
                     model: str = OLLAMA_MODEL, focus: str = "") -> list[Moment]:
    """Выбор моментов локальной нейросетью через Ollama — бесплатно и без интернета."""
    body = json.dumps({
        "model": model, "stream": False, "format": _Picked.model_json_schema(),
        "options": {"num_ctx": 32768},
        "messages": [{"role": "system", "content": SYSTEM},
                     {"role": "user", "content": _prompt(tr, count, min_len, max_len, focus) + JSON_RULE}],
    }).encode()
    req = urllib.request.Request(f"{OLLAMA_URL}/api/chat", data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=1800) as resp:
            reply = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Ollama ответила ошибкой {exc.code}: возможно, модель не скачана "
                           f"(ollama pull {model})") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise RuntimeError(f"Ollama недоступна по адресу {OLLAMA_URL}: {exc}") from exc
    return _clean(_parse_json_reply(reply.get("message", {}).get("content", "")),
                  tr.duration, min_len, max_len, count)


def available_pickers() -> list[str]:
    """Доступные способы выбора моментов, от лучшего к запасному."""
    order = []
    if os.environ.get("ANTHROPIC_API_KEY"):
        order.append("claude")
    if claude_cli_available():
        order.append("claude-cli")
    if ollama_available():
        order.append("ollama")
    return order + ["heuristic"]


def pick_with_claude(tr: Transcript, count: int, min_len: float, max_len: float,
                     model: str = DEFAULT_MODEL, focus: str = "") -> list[Moment]:
    """Выбор моментов через Claude API. Нужен ANTHROPIC_API_KEY (или `ant auth login`)."""
    import anthropic

    client = anthropic.Anthropic()
    prompt = _prompt(tr, count, min_len, max_len, focus)
    response = client.messages.parse(
        model=model,
        max_tokens=16000,
        system=SYSTEM,
        messages=[{"role": "user", "content": prompt}],
        output_format=_Picked,
    )
    if response.stop_reason == "refusal" or response.parsed_output is None:
        raise RuntimeError(f"Claude не вернул моменты (stop_reason={response.stop_reason})")
    return _clean(response.parsed_output.moments, tr.duration, min_len, max_len, count)


def _loudness(video: Path, rate: int = 8000, start: float | None = None, length: float | None = None,
              step: float = 1.0) -> np.ndarray:
    """Громкость (RMS) по отрезкам длиной step секунд."""
    seek = ["-ss", f"{start:.3f}", "-t", f"{length:.3f}"] if start is not None else []
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", *seek, "-i", str(video), "-vn", "-ac", "1", "-ar", str(rate),
         "-f", "s16le", "-"], capture_output=True, check=True).stdout
    audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32)
    size = int(rate * step)
    n = len(audio) // size
    if n == 0:
        return np.zeros(1, dtype=np.float32)
    return np.sqrt((audio[: n * size].reshape(n, size) ** 2).mean(axis=1))


def loud_peaks(video: Path, start: float, length: float, count: int = 3, gap: float = 2.5) -> list[float]:
    """Самые громкие места отрезка (секунды от его начала) — для резких наездов камеры."""
    step = 0.25
    loud = _loudness(video, start=start, length=length, step=step)
    if len(loud) < 4 or loud.std() < 1e-6:
        return []
    peaks: list[float] = []
    for idx in np.argsort(-loud):
        t = float(idx) * step
        if loud[idx] < loud.mean() + loud.std() or len(peaks) >= count:
            break
        if 0.8 < t < length - 0.8 and all(abs(t - p) >= gap for p in peaks):
            peaks.append(round(t, 2))
    return sorted(peaks)


def pick_heuristic(video: Path, tr: Transcript | None, count: int, min_len: float,
                   max_len: float) -> list[Moment]:
    """Запасной выбор без нейросети: всплески громкости плюс плотность речи."""
    loud = _loudness(video)
    n = len(loud)
    score = (loud - loud.mean()) / (loud.std() + 1e-6)
    if tr:
        density = np.zeros(n, dtype=np.float32)
        for w in tr.words_between(0, n):
            density[min(int(w.start), n - 1)] += 1
        score = score + (density - density.mean()) / (density.std() + 1e-6)
    win = int(min(max((min_len + max_len) / 2, min_len), n))
    if win <= 0:
        return []
    sums = np.convolve(score, np.ones(win), mode="valid")
    moments: list[Moment] = []
    taken = np.zeros(len(sums), dtype=bool)
    for idx in np.argsort(-sums):
        if len(moments) >= count:
            break
        if taken[idx]:
            continue
        start, end = float(idx), float(idx + win)
        if tr:
            start, end = _snap(tr, start, end, min_len, max_len)
        rank = float(sums[idx] / win)
        moments.append(Moment(start=start, end=end, title="", reason="всплеск громкости и речи",
                              score=int(np.clip(50 + rank * 20, 0, 100))))
        taken[max(0, idx - win): idx + win] = True
    return sorted(moments, key=lambda m: m.start)


def _snap(tr: Transcript, start: float, end: float, min_len: float, max_len: float) -> tuple[float, float]:
    """Подтягиваем границы к началу и концу фраз, чтобы не резать на полуслове."""
    starts = [s.start for s in tr.segments if abs(s.start - start) <= 5]
    ends = [s.end for s in tr.segments if abs(s.end - end) <= 5]
    new_start = min(starts, key=lambda t: abs(t - start)) if starts else start
    new_end = min(ends, key=lambda t: abs(t - end)) if ends else end
    if min_len <= new_end - new_start <= max_len:
        return new_start, new_end
    return start, end


def _clean(moments: list[Moment], duration: float, min_len: float, max_len: float,
           count: int) -> list[Moment]:
    """Отбрасываем кривые и пересекающиеся моменты, оставляем лучшие по оценке."""
    kept: list[Moment] = []
    for m in sorted(moments, key=lambda m: -m.score):
        m.start = max(0.0, m.start)
        m.end = min(duration, m.end) if duration else m.end
        if not (min_len * 0.7 <= m.duration <= max_len * 1.3):
            continue
        if any(m.start < k.end and k.start < m.end for k in kept):
            continue
        kept.append(m)
        if len(kept) >= count:
            break
    return sorted(kept, key=lambda m: m.start)


def pick(video: Path, tr: Transcript | None, picker: str, count: int, min_len: float,
         max_len: float, log=print, focus: str = "") -> tuple[list[Moment], str]:
    """Выбирает моменты указанным способом; в режиме auto перебирает доступные по очереди."""
    has_text = bool(tr and tr.segments)
    chain = [p for p in available_pickers() if has_text or p == "heuristic"] if picker == "auto" else [picker]
    llm = {"claude": pick_with_claude, "claude-cli": pick_with_claude_cli, "ollama": pick_with_ollama}
    for name in chain:
        log(f"Ищу моменты ({name})…")
        if name == "heuristic":
            return pick_heuristic(video, tr, count, min_len, max_len), name
        if not has_text:
            raise RuntimeError(f"Для «{name}» нужна расшифровка речи")
        try:
            return llm[name](tr, count, min_len, max_len, focus=focus), name
        except Exception as exc:  # любой сбой нейросети — пробуем следующий способ
            log(f"  {name} не сработал: {exc}")
            if name == chain[-1]:
                raise RuntimeError(f"{name} не сработал: {exc}") from exc
    return [], "none"
