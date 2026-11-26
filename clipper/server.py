"""Локальная панель: python -m clipper.server  ->  http://127.0.0.1:8765"""
from __future__ import annotations

import json
import random
import re
import threading
import time
import traceback
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from .analysis import detect_scenes, uniqueness
from .media import fetch, probe
from .models import Moment, Preset, Transcript
from .moments import available_pickers, pick
from .render import render_clip, render_frame

ROOT = Path(__file__).resolve().parent.parent
PRESETS = ROOT / "presets"
DATA = ROOT / "data"
STATE_FILE = DATA / "state.json"
WEB = Path(__file__).resolve().parent / "web"

app = FastAPI(title="Clipper")
lock = threading.RLock()
state: dict = {"projects": {}}


def load_state() -> None:
    DATA.mkdir(exist_ok=True)
    if STATE_FILE.exists():
        state.update(json.loads(STATE_FILE.read_text(encoding="utf-8")))
    for p in state["projects"].values():  # после перезапуска незавершённые задачи считаем прерванными
        if p["status"] not in ("ready", "error"):
            p["status"], p["message"] = "error", "Прервано перезапуском панели"


def save_state() -> None:
    with lock:
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(STATE_FILE)


def project(pid: str) -> dict:
    p = state["projects"].get(pid)
    if not p:
        raise HTTPException(404, "Проект не найден")
    return p


def set_status(p: dict, status: str, message: str = "") -> None:
    with lock:
        p["status"], p["message"] = status, message
    save_state()


def slug(text: str, fallback: str = "clip") -> str:
    return re.sub(r"[^\w]+", "_", text.lower(), flags=re.UNICODE).strip("_")[:40] or fallback


def transcript_of(p: dict) -> Transcript | None:
    path = Path(p["video"]).with_suffix(".transcript.json")
    return Transcript.load(path) if path.exists() else None


def filler_of(p: dict) -> Path | None:
    f = p.get("filler")
    return Path(f) if f and Path(f).exists() else None


# ---------- фоновые задачи ----------

def analyze_job(pid: str, opts: dict) -> None:
    p = state["projects"][pid]
    try:
        set_status(p, "download", "Получаю видео…")
        video = fetch(p["source"], DATA / "downloads")
        info = probe(video)
        with lock:
            p.update(video=str(video), title=video.stem, **info)
        tr = None
        if opts["whisper"] != "off":
            set_status(p, "transcribe", "Расшифровываю речь…")
            try:
                from .transcribe import transcribe
                tr = transcribe(video, opts["whisper"], opts["lang"] or None)
                tr.duration = tr.duration or info["duration"]
            except ImportError:
                p["warning"] = "faster-whisper не установлен — без субтитров и выбора по смыслу"
            except Exception as exc:  # распознавание не должно ронять весь анализ
                p["warning"] = f"Речь не распозналась ({str(exc)[:120]}) — без субтитров"
        set_status(p, "scenes", "Ищу смены сцен…")
        p["scenes"] = detect_scenes(video)
        set_status(p, "moments", "Ищу сильные моменты…")
        moments, used = pick(video, tr, opts["picker"], opts["count"], opts["min"], opts["max"],
                             log=lambda msg: set_status(p, "moments", msg))
        with lock:
            p["picker"] = used
            p["moments"] = [m.model_dump() for m in sorted(moments, key=lambda m: -m.score)]
        set_status(p, "ready", f"Найдено моментов: {len(moments)}")
    except Exception as exc:
        traceback.print_exc()
        set_status(p, "error", str(exc)[:500])


def render_job(pid: str, indexes: list[int], preset: Preset) -> None:
    p = state["projects"][pid]
    video, tr = Path(p["video"]), transcript_of(p)
    out_dir = DATA / "clips" / pid
    try:
        for n, i in enumerate(indexes, 1):
            m = Moment.model_validate(p["moments"][i])
            set_status(p, "render", f"Монтирую {n} из {len(indexes)}: {m.title or 'без названия'}")
            name = f"{int(m.start):05d}_{slug(m.title)}_{preset.name}_{uuid.uuid4().hex[:4]}.mp4"
            render_clip(video, m, preset, out_dir / name, tr, filler_of(p))
            with lock:
                p.setdefault("clips", []).insert(0, {
                    "file": name, "title": m.title, "caption": m.caption, "hashtags": m.hashtags,
                    "score": m.score, "profanity": m.profanity, "preset": preset.name,
                    "start": m.start, "end": m.end, "speed": preset.speed, "created": time.time()})
        set_status(p, "ready", f"Смонтировано роликов: {len(indexes)}")
    except Exception as exc:
        traceback.print_exc()
        set_status(p, "error", str(exc)[:500])


# ---------- API ----------

class NewProject(BaseModel):
    source: str
    picker: str = "auto"
    count: int = 8
    min: float = 20
    max: float = 60
    whisper: str = "small"
    lang: str = "ru"
    filler: str = ""


class MomentPatch(BaseModel):
    title: str | None = None
    start: float | None = None
    end: float | None = None
    caption: str | None = None


class PreviewReq(BaseModel):
    project: str
    moment: int
    preset: Preset
    at: float = 0.5


class RenderReq(BaseModel):
    project: str
    moments: list[int]
    preset: Preset


@app.get("/")
def index():
    return FileResponse(WEB / "index.html")


@app.get("/api/state")
def get_state():
    presets = [Preset.load(str(f), PRESETS).model_dump() for f in sorted(PRESETS.glob("*.json"))]
    with lock:
        return {"projects": state["projects"], "presets": presets, "pickers": available_pickers(),
                "defaults": Preset().model_dump()}


@app.post("/api/projects")
def new_project(req: NewProject):
    pid = uuid.uuid4().hex[:8]
    with lock:
        state["projects"][pid] = {"id": pid, "source": req.source.strip(), "title": req.source.strip()[-60:],
                                  "status": "queued", "message": "В очереди", "moments": [], "clips": [],
                                  "filler": req.filler.strip(), "created": time.time()}
    save_state()
    threading.Thread(target=analyze_job, args=(pid, req.model_dump()), daemon=True).start()
    return {"id": pid}


@app.put("/api/upload")
async def upload(request: Request, name: str):
    """Загрузка файла из браузера: тело запроса — сам файл."""
    safe = slug(Path(name).stem, "video") + (Path(name).suffix.lower() or ".mp4")
    target = DATA / "uploads" / f"{uuid.uuid4().hex[:6]}_{safe}"
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "wb") as f:
        async for chunk in request.stream():
            f.write(chunk)
    return {"path": str(target)}


@app.delete("/api/projects/{pid}")
def delete_project(pid: str):
    project(pid)
    with lock:
        del state["projects"][pid]
    save_state()
    return {"ok": True}


@app.patch("/api/projects/{pid}/moments/{i}")
def patch_moment(pid: str, i: int, patch: MomentPatch):
    p = project(pid)
    if not 0 <= i < len(p["moments"]):
        raise HTTPException(404, "Момент не найден")
    changes = {k: v for k, v in patch.model_dump().items() if v is not None}
    merged = {**p["moments"][i], **changes}
    if merged["end"] - merged["start"] < 1:
        raise HTTPException(400, "Конец должен быть позже начала")
    with lock:
        p["moments"][i] = merged
    save_state()
    return merged


@app.post("/api/projects/{pid}/moments")
def add_moment(pid: str, m: Moment):
    p = project(pid)
    with lock:
        p["moments"].append(m.model_dump())
    save_state()
    return {"index": len(p["moments"]) - 1}


@app.post("/api/preview")
def preview(req: PreviewReq):
    p = project(req.project)
    if not p.get("video"):
        raise HTTPException(400, "Видео ещё не готово")
    m = Moment.model_validate(p["moments"][req.moment])
    out = DATA / "preview" / f"{req.project}.jpg"
    try:
        render_frame(Path(p["video"]), m, req.preset, out, transcript_of(p), filler_of(p), req.at)
    except RuntimeError as exc:
        raise HTTPException(400, str(exc)[-400:])
    return FileResponse(out, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@app.post("/api/presets")
def save_preset(preset: Preset):
    preset.name = slug(preset.name, "preset")
    preset.save(PRESETS)
    return {"name": preset.name}


@app.post("/api/presets/random")
def random_preset(preset: Preset):
    """Кнопка «Рандом»: случайные, но аккуратные значения уникализации."""
    preset.mirror = random.random() < 0.3
    preset.speed = round(random.uniform(1.0, 1.06), 2)
    preset.content_zoom = round(random.uniform(1.0, 1.15), 2)
    preset.brightness = round(random.uniform(-0.03, 0.04), 2)
    preset.contrast = round(random.uniform(1.0, 1.08), 2)
    preset.saturation = round(random.uniform(1.0, 1.2), 2)
    preset.background_blur = random.randint(20, 40)
    return preset


@app.post("/api/render")
def render(req: RenderReq):
    p = project(req.project)
    if p["status"] not in ("ready", "error") or not p.get("video"):
        raise HTTPException(409, "Проект занят — дождитесь окончания текущей задачи")
    bad = [i for i in req.moments if not 0 <= i < len(p["moments"])]
    if bad or not req.moments:
        raise HTTPException(400, "Выберите моменты для монтажа")
    set_status(p, "render", "Запускаю монтаж…")
    threading.Thread(target=render_job, args=(req.project, req.moments, req.preset), daemon=True).start()
    return {"ok": True}


def clip_path(pid: str, name: str) -> Path:
    path = (DATA / "clips" / pid / Path(name).name)
    if not path.exists():
        raise HTTPException(404, "Ролик не найден")
    return path


@app.get("/clips/{pid}/{name}")
def get_clip(pid: str, name: str):
    return FileResponse(clip_path(pid, name), media_type="video/mp4")


@app.delete("/api/clips/{pid}/{name}")
def delete_clip(pid: str, name: str):
    p = project(pid)
    clip_path(pid, name).unlink()
    with lock:
        p["clips"] = [c for c in p["clips"] if c["file"] != name]
    save_state()
    return {"ok": True}


@app.post("/api/check/{pid}/{name}")
def check_clip(pid: str, name: str):
    """Проверка уникальности: сравнение ролика с исходным отрезком."""
    p = project(pid)
    clip = next((c for c in p["clips"] if c["file"] == name), None)
    if not clip:
        raise HTTPException(404, "Ролик не найден")
    try:
        result = uniqueness(Path(p["video"]), clip_path(pid, name), clip["start"], clip.get("speed", 1.0))
    except RuntimeError as exc:
        raise HTTPException(400, str(exc))
    with lock:
        clip["check"] = result
    save_state()
    return result


@app.exception_handler(Exception)
async def on_error(_: Request, exc: Exception):
    return JSONResponse({"detail": str(exc)[:500]}, status_code=500)


def main() -> None:
    import uvicorn
    load_state()
    print("Панель: http://127.0.0.1:8765")
    uvicorn.run(app, host="127.0.0.1", port=8765, log_level="warning")


if __name__ == "__main__":
    main()
