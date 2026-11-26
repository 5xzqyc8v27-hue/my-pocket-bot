"""Разбор ролика-примера: смотрим кадры и склейки, получаем пресет и описание стиля."""
from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
from pathlib import Path

from pydantic import BaseModel, Field

from .analysis import detect_scenes
from .media import probe, run
from .models import Preset


class StyleGuess(BaseModel):
    format: str = Field(description="single — один момент; top — подборка «топ N» со списком")
    layout: str = Field(description="center, square, split или reaction (вебка + игра)")
    webcam_position: str = Field(default="top", description="top или bottom")
    webcam_share: float = Field(default=0.4, description="доля высоты под вебку, 0.2-0.6")
    title: bool = True
    title_y: float = Field(default=0.12, description="центр надписи-хука по высоте, 0-1")
    title_color: str = Field(default="FFFFFF", description="RRGGBB")
    title_glow: str | None = Field(default=None, description="цвет свечения RRGGBB или null")
    subtitles: bool = True
    subtitle_words: int = Field(default=3, description="слов на экране одновременно, 1-6")
    subtitle_upper: bool = False
    subtitle_color: str = "FFFFFF"
    subtitle_highlight: str = "FFE600"
    subtitle_y: float = 0.72
    has_watermark: bool = False
    notes: str = Field(description="5-8 предложений: приёмы монтажа, темп, как подан хук, "
                                   "вставки, переходы, что стоит повторить")


PROMPT = """Это раскадровка вертикального ролика-нарезки (кадры идут слева направо, сверху вниз).
Длительность {duration:.0f} с, склеек найдено: {cuts}.
Разбери оформление и монтаж, чтобы повторить стиль в своём ролике."""

JSON_RULE = (
    "\n\nОтветь только JSON-объектом без пояснений и без markdown. Типы строго по схеме: числа — числами "
    "(доли от 0 до 1), флаги — true/false, цвета — RRGGBB. Весь текст-описание — только в поле notes.\n"
    "Схема: " + json.dumps(StyleGuess.model_json_schema()["properties"], ensure_ascii=False)
)


def contact_sheet(video: Path, out: Path, frames: int = 8) -> Path:
    duration = probe(video)["duration"]
    out.parent.mkdir(parents=True, exist_ok=True)
    run(["ffmpeg", "-y", "-v", "error", "-i", str(video), "-frames:v", "1", "-q:v", "3",
         "-vf", f"fps={frames / max(duration, 1):.4f},scale=320:-2,tile=4x{(frames + 3) // 4}", str(out)])
    return out


def _ask_api(sheet: Path, prompt: str) -> StyleGuess:
    import anthropic

    response = anthropic.Anthropic().messages.parse(
        model=os.environ.get("CLIPPER_MODEL", "claude-opus-5-5"), max_tokens=16000,
        messages=[{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                         "data": base64.standard_b64encode(sheet.read_bytes()).decode()}},
            {"type": "text", "text": prompt}]}],
        output_format=StyleGuess)
    if response.parsed_output is None:
        raise RuntimeError(f"Claude не вернул разбор (stop_reason={response.stop_reason})")
    return response.parsed_output


def _ask_cli(sheet: Path, prompt: str) -> StyleGuess:
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    text = f"Открой изображение {sheet.resolve()} инструментом Read.\n{prompt}{JSON_RULE}"
    proc = subprocess.run(["claude", "-p", "--output-format", "json", "--allowedTools", "Read"],
                          input=text, capture_output=True, text=True, timeout=600, env=env)
    if proc.returncode != 0:
        raise RuntimeError(f"Claude Code завершился с ошибкой: {(proc.stderr or proc.stdout)[-400:]}")
    reply = proc.stdout
    try:
        reply = str(json.loads(reply).get("result", reply))
    except json.JSONDecodeError:
        pass
    start, end = reply.find("{"), reply.rfind("}")
    if start < 0:
        raise RuntimeError("Claude не вернул JSON с разбором")
    return StyleGuess.model_validate_json(reply[start:end + 1])


def analyze_reference(video: Path, name: str, presets_dir: Path, work_dir: Path) -> dict:
    """Возвращает описание стиля и сохраняет пресет с этим именем."""
    info = probe(video)
    cuts = len(detect_scenes(video))
    sheet = contact_sheet(video, work_dir / f"{name}.jpg")
    prompt = PROMPT.format(duration=info["duration"], cuts=cuts)
    if os.environ.get("ANTHROPIC_API_KEY"):
        guess = _ask_api(sheet, prompt)
    elif shutil.which("claude"):
        guess = _ask_cli(sheet, prompt)
    else:
        raise RuntimeError("Для разбора примеров нужен Claude: ключ API или вход в Claude Code")
    fields = guess.model_dump(exclude={"format", "notes", "has_watermark"})
    if fields["layout"] not in ("center", "square", "split", "reaction"):
        fields["layout"] = "center"
    preset = Preset(name=name, **fields)
    preset.save(presets_dir)
    return {"name": name, "format": guess.format, "notes": guess.notes, "cuts": cuts,
            "duration": info["duration"], "sheet": sheet.name, "preset": preset.model_dump()}
