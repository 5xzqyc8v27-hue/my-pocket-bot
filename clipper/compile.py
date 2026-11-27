"""Тематические подборки: короткие куски из разных трансляций под одним заголовком."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from pydantic import BaseModel, Field

from .media import run
from .models import Moment, Preset, Transcript
from .render import render_clip


class Piece(BaseModel):
    project: str = Field(description="id трансляции из списка")
    start: float
    end: float


class Theme(BaseModel):
    title: str = Field(description="Заголовок подборки на экране, до 5 слов, например «Искусство обзывания»")
    why: str = Field(description="Что объединяет куски и почему это смешно")
    pieces: list[Piece] = Field(description="3-6 кусков по 4-12 секунд, желательно из разных трансляций")


class _Themes(BaseModel):
    themes: list[Theme]


SYSTEM = """Ты — редактор мемных нарезок стримеров для TikTok и Shorts.
Тебе дают расшифровки нескольких трансляций одного стримера с таймкодами.
Найди темы для подборок: повторяющиеся приколы, любимые словечки и обзывательства, похожие
реакции, одинаковые ситуации. Одна подборка — 3-6 коротких кусков (4-12 секунд каждый) под общим
заголовком. Примеры заголовков: «Искусство обзывания», «3 вида одноклассников», «Когда всё бесит».

Правила:
- Каждый кусок — законченная фраза или реакция, границы по таймкодам строк.
- Куски внутри подборки не повторяют друг друга дословно и идут от слабого к самому смешному.
- Бери куски из разных трансляций, если тема встречается в нескольких.
- Мат допустим. Не бери оскорбления по национальности, религии и подобным признакам.
- Заголовок короткий, без кавычек и эмодзи."""

JSON_RULE = ('\n\nОтветь только JSON-объектом без пояснений: {"themes": [{"title": "", "why": "", '
             '"pieces": [{"project": "", "start": 0.0, "end": 0.0}]}]}')


def _prompt(sources: list[tuple[str, str, Transcript]], count: int) -> str:
    blocks = []
    for pid, title, tr in sources:
        lines = "\n".join(f"[{s.start:.1f}-{s.end:.1f}] {s.text}" for s in tr.segments)
        blocks.append(f"=== Трансляция id={pid} ({title}) ===\n{lines}")
    return f"Нужно до {count} подборок.\n\n" + "\n\n".join(blocks)


def find_themes(sources: list[tuple[str, str, Transcript]], count: int = 5) -> list[Theme]:
    """Ищет темы подборок через Claude: по API, если есть ключ, иначе через подписку."""
    prompt = _prompt(sources, count)
    if os.environ.get("ANTHROPIC_API_KEY"):
        import anthropic
        response = anthropic.Anthropic().messages.parse(
            model=os.environ.get("CLIPPER_MODEL", "claude-opus-5-5"), max_tokens=16000, system=SYSTEM,
            messages=[{"role": "user", "content": prompt}], output_format=_Themes)
        if response.parsed_output is None:
            raise RuntimeError(f"Claude не вернул темы (stop_reason={response.stop_reason})")
        themes = response.parsed_output.themes
    elif shutil.which("claude"):
        env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
        proc = subprocess.run(["claude", "-p", "--output-format", "json"], input=SYSTEM + "\n\n" + prompt + JSON_RULE,
                              capture_output=True, text=True, timeout=900, env=env)
        if proc.returncode != 0:
            raise RuntimeError(f"Claude Code завершился с ошибкой: {(proc.stderr or proc.stdout)[-400:]}")
        reply = proc.stdout
        try:
            reply = str(json.loads(reply).get("result", reply))
        except json.JSONDecodeError:
            pass
        a, b = reply.find("{"), reply.rfind("}")
        if a < 0:
            raise RuntimeError("Claude не вернул JSON с темами")
        themes = _Themes.model_validate_json(reply[a:b + 1]).themes
    else:
        raise RuntimeError("Для поиска тем нужен Claude: ключ API или вход в Claude Code")
    known = {pid: tr.duration for pid, _, tr in sources}
    out = []
    for th in themes:  # отбрасываем куски с несуществующими id и кривыми границами
        th.pieces = [p for p in th.pieces if p.project in known and 1.5 <= p.end - p.start <= 20
                     and p.start >= 0 and (not known[p.project] or p.end <= known[p.project] + 1)]
        if len(th.pieces) >= 2:
            out.append(th)
    return out[:count]


def render_compilation(pieces: list[tuple[Path, Moment, Transcript | None]], title: str, preset: Preset,
                       out_path: Path, music: Path | None = None, filler: Path | None = None) -> Path:
    """Монтирует каждый кусок в общем оформлении с одним заголовком и склеивает встык."""
    if len(pieces) < 2:
        raise ValueError("Для подборки нужно минимум два куска")
    base = preset.model_copy(update={"title": True, "title_anim": False, "speed": 1.0, "camera": "none"})
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        names = []
        for i, (video, moment, tr) in enumerate(pieces):
            part = Path(tmp) / f"part{i:02d}.mp4"
            render_clip(video, moment.model_copy(update={"title": title, "inserts": [], "chat_text": ""}),
                        base, part, tr, filler)
            names.append(f"file '{part.name}'")
        (Path(tmp) / "list.txt").write_text("\n".join(names), encoding="utf-8")
        joined = Path(tmp) / "joined.mp4"
        run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", "list.txt",
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-c:a", "aac", "-b:a", "160k",
             "-movflags", "+faststart", str(joined)], cwd=Path(tmp))
        if music and Path(music).exists():  # тихая музыка под всем роликом
            run(["ffmpeg", "-y", "-v", "error", "-i", str(joined), "-stream_loop", "-1", "-i", str(Path(music).resolve()),
                 "-filter_complex", "[1:a]volume=0.16[m];[0:a][m]amix=inputs=2:normalize=0:duration=first[a]",
                 "-map", "0:v", "-map", "[a]", "-c:v", "copy", "-c:a", "aac", "-b:a", "160k",
                 "-movflags", "+faststart", str(out_path.resolve())])
        else:
            shutil.copyfile(joined, out_path)
    return out_path
