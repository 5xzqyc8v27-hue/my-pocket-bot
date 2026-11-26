"""Локальная панель: python -m clipper.server  ->  http://127.0.0.1:8765"""
from __future__ import annotations

import json
import random
import shutil
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
from .capcut import default_drafts_dir, export_draft
from .media import fetch, probe
from .models import Moment, Preset, Transcript
from .moments import available_pickers, pick
from .reference import analyze_reference
from .render import render_clip, render_frame
from .streams import StreamManager
from .top import render_top
from .variety import vary

ROOT = Path(__file__).resolve().parent.parent
PRESETS = ROOT / "presets"
DATA = ROOT / "data"
STATE_FILE = DATA / "state.json"
WEB = Path(__file__).resolve().parent / "web"

app = FastAPI(title="Clipper")
lock = threading.RLock()
state: dict = {"projects": {}, "references": {}, "streamers": {}}
ASSETS = DATA / "assets"
KINDS = {"photos": "Фото и вырезки", "emoji": "Эмодзи и стикеры", "fonts": "Шрифты", "sounds": "Звуки",
         "banners": "Баннеры", "fillers": "Залипалки", "templates": "Шаблоны"}


def load_state() -> None:
    DATA.mkdir(exist_ok=True)
    if STATE_FILE.exists():
        loaded = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        streamers = loaded.pop("streamers", {})
        state.update(loaded)
        state["streamers"].update(streamers)  # тот же словарь, за которым следит менеджер эфиров
    state.setdefault("references", {})
    for st in state["streamers"].values():
        st.update(recording=False, live=False, working="", last_check=0)
    for r in state["references"].values():
        if r["status"] == "work":
            r["status"], r["message"] = "error", "Прервано перезапуском панели"
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
                             log=lambda msg: set_status(p, "moments", msg), focus=opts.get("focus", ""))
        with lock:
            p["picker"] = used
            p["moments"] = [m.model_dump() for m in sorted(moments, key=lambda m: -m.score)]
        set_status(p, "ready", f"Найдено моментов: {len(moments)}")
    except Exception as exc:
        traceback.print_exc()
        set_status(p, "error", str(exc)[:500])


def render_job(pid: str, indexes: list[int], base: Preset, varied: bool = False) -> None:
    p = state["projects"][pid]
    video, tr = Path(p["video"]), transcript_of(p)
    out_dir = DATA / "clips" / pid
    try:
        for n, i in enumerate(indexes, 1):
            m = Moment.model_validate(p["moments"][i])
            preset = vary(base, seed=int(time.time() * 1000) + i) if varied else base
            set_status(p, "render", f"Монтирую {n} из {len(indexes)}: {m.title or 'без названия'}")
            name = f"{int(m.start):05d}_{slug(m.title)}_{preset.name}_{uuid.uuid4().hex[:4]}.mp4"
            render_clip(video, m, preset, out_dir / name, tr, filler_of(p))
            with lock:
                p.setdefault("clips", []).insert(0, {
                    "file": name, "title": m.title, "caption": m.caption, "hashtags": m.hashtags,
                    "score": m.score, "profanity": m.profanity,
                    "preset": preset.name + (" · вариация" if varied else ""),
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
    streamer: str = ""
    focus: str = ""


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
    vary: bool = False


@app.get("/")
def index():
    return FileResponse(WEB / "index.html")


@app.get("/api/state")
def get_state():
    presets = [Preset.load(str(f), PRESETS).model_dump() for f in sorted(PRESETS.glob("*.json"))]
    with lock:
        return {"projects": state["projects"], "presets": presets, "pickers": available_pickers(),
                "defaults": Preset().model_dump(), "references": state["references"],
                "assets": list_assets(), "kinds": KINDS, "streamers": state["streamers"],
                "disk_free_gb": round(shutil.disk_usage(DATA).free / 1e9, 1)}


@app.get("/api/health")
def health():
    """Автопроверка: что установлено и что ещё нужно настроить."""
    import importlib.util
    import os
    import shutil

    from .moments import ollama_available

    def lib(name: str) -> bool:
        return importlib.util.find_spec(name) is not None

    checks = [
        ("ffmpeg", "Монтаж видео", shutil.which("ffmpeg") is not None, True, "brew install ffmpeg"),
        ("streamlink", "Запись эфиров Twitch и Kick", shutil.which("streamlink") is not None, False,
         "pip install streamlink"),
        ("yt-dlp", "Скачивание по ссылкам", shutil.which("yt-dlp") is not None, False, "pip install yt-dlp"),
        ("faster-whisper", "Распознавание речи: субтитры и выбор по смыслу", lib("faster_whisper"), False,
         "pip install faster-whisper (модель скачается сама при первом запуске)"),
        ("Claude по API", "Выбор моментов за кредиты", bool(os.environ.get("ANTHROPIC_API_KEY")), False,
         "export ANTHROPIC_API_KEY=ваш_ключ — перед запуском панели"),
        ("Claude по подписке", "Выбор моментов и разбор примеров без кредитов", shutil.which("claude") is not None,
         False, "Установить Claude Code, запустить claude и выполнить /login"),
        ("Ollama", "Локальная нейросеть, бесплатно", ollama_available(), False,
         "brew install ollama && ollama serve && ollama pull qwen2.5:7b"),
        ("pycapcut", "Экспорт в CapCut", lib("pycapcut"), False, "pip install pycapcut"),
        ("CapCut", "Папка проектов CapCut найдена", default_drafts_dir() is not None, False,
         "Установить CapCut и один раз создать в нём любой проект"),
    ]
    return [{"name": n, "what": w, "ok": ok, "required": req, "fix": fix} for n, w, ok, req, fix in checks]


@app.post("/api/projects")
def new_project(req: NewProject):
    pid = uuid.uuid4().hex[:8]
    with lock:
        state["projects"][pid] = {"id": pid, "source": req.source.strip(), "title": req.source.strip()[-60:],
                                  "status": "queued", "message": "В очереди", "moments": [], "clips": [],
                                  "filler": req.filler.strip(), "streamer": slug(req.streamer, "common"),
                                  "created": time.time()}
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
    threading.Thread(target=render_job, args=(req.project, req.moments, req.preset, req.vary), daemon=True).start()
    return {"ok": True}


# ---------- стримеры: эфир -> монтаж ----------

class StreamerReq(BaseModel):
    name: str
    platform: str = "twitch"  # twitch, kick или file (готовая запись эфира)
    channel: str
    enabled: bool = True
    montage: bool = True
    preset: str = "reaction"
    focus: str = ""
    picker: str = "auto"
    per_chunk: int = 4
    min_score: int = 60
    skip_profanity: bool = False
    chunk_minutes: float = 10
    delete_chunks: bool = False
    whisper: str = "small"


def process_chunk(st: dict, chunk: Path) -> int:
    """Один записанный кусок эфира: найти моменты и смонтировать лучшие."""
    pid = uuid.uuid4().hex[:8]
    with lock:
        state["projects"][pid] = {"id": pid, "source": str(chunk), "title": chunk.stem, "status": "queued",
                                  "message": "В очереди", "moments": [], "clips": [], "filler": "",
                                  "streamer": st["id"], "auto": True, "created": time.time()}
    analyze_job(pid, {"picker": st["picker"], "count": st["per_chunk"], "min": 15, "max": 60,
                      "whisper": st["whisper"], "lang": "ru", "focus": st["focus"]})
    p = state["projects"][pid]
    if p["status"] == "error":
        raise RuntimeError(p["message"])
    good = [i for i, m in enumerate(p["moments"])
            if m["score"] >= st["min_score"] and not (st["skip_profanity"] and m["profanity"])]
    if not good:
        set_status(p, "ready", "Сильных моментов в этом куске нет")
        return 0
    preset = Preset.load(st["preset"], PRESETS)
    if not preset.watermark:
        preset.watermark = ""
    render_job(pid, good, preset, varied=len(good) > 1)
    if p["status"] == "error":
        raise RuntimeError(p["message"])
    return len(good)


manager = StreamManager(state["streamers"], DATA / "recordings", lock, lambda: save_state(), process_chunk)


@app.post("/api/streamers")
def save_streamer(req: StreamerReq):
    if req.platform not in ("twitch", "kick", "file"):
        raise HTTPException(400, "Площадка: twitch, kick или file")
    channel = req.channel.strip()
    if req.platform != "file":
        channel = channel.rstrip("/").split("/")[-1].lower()  # принимаем и ссылку, и ник
        if not re.fullmatch(r"[a-z0-9_\-]{2,40}", channel):
            raise HTTPException(400, "Укажите ник канала или ссылку на него")
    sid = slug(req.name, "streamer")
    with lock:
        old = state["streamers"].get(sid, {})
        state["streamers"][sid] = {**old, **req.model_dump(), "id": sid, "channel": channel,
                                   "created": old.get("created", time.time())}
    manager.force.add(sid)
    save_state()
    return {"id": sid}


@app.patch("/api/streamers/{sid}")
def patch_streamer(sid: str, changes: dict):
    st = state["streamers"].get(sid)
    if not st:
        raise HTTPException(404, "Стример не найден")
    allowed = set(StreamerReq.model_fields) - {"name", "platform", "channel"}
    with lock:
        st.update({k: v for k, v in changes.items() if k in allowed})
    manager.force.add(sid)
    save_state()
    return st


@app.post("/api/streamers/{sid}/check")
def check_streamer(sid: str):
    if sid not in state["streamers"]:
        raise HTTPException(404, "Стример не найден")
    manager.force.add(sid)
    return {"ok": True}


@app.delete("/api/streamers/{sid}")
def delete_streamer(sid: str):
    if sid not in state["streamers"]:
        raise HTTPException(404, "Стример не найден")
    manager.stop(sid)
    with lock:
        del state["streamers"][sid]
    save_state()
    return {"ok": True}


# ---------- материалы: общие и по стримерам ----------

def list_assets() -> dict:
    out: dict = {"common": {k: [] for k in KINDS}}
    if ASSETS.is_dir():
        for owner in sorted(ASSETS.iterdir()):
            if owner.is_dir():
                out[owner.name] = {k: sorted(f.name for f in (owner / k).glob("*") if f.is_file())
                                   for k in KINDS}
    return out


def asset_path(owner: str, kind: str, name: str) -> Path:
    if kind not in KINDS:
        raise HTTPException(400, "Неизвестный тип материала")
    return ASSETS / slug(owner, "common") / kind / Path(name).name


@app.put("/api/assets/{owner}/{kind}")
async def upload_asset(owner: str, kind: str, name: str, request: Request):
    stem, ext = Path(name).stem, Path(name).suffix.lower()
    target = asset_path(owner, kind, slug(stem, "file") + ext)
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "wb") as f:
        async for chunk in request.stream():
            f.write(chunk)
    return {"name": target.name}


@app.get("/assets/{owner}/{kind}/{name}")
def get_asset(owner: str, kind: str, name: str):
    path = asset_path(owner, kind, name)
    if not path.exists():
        raise HTTPException(404, "Файл не найден")
    return FileResponse(path)


@app.delete("/api/assets/{owner}/{kind}/{name}")
def delete_asset(owner: str, kind: str, name: str):
    path = asset_path(owner, kind, name)
    if not path.exists():
        raise HTTPException(404, "Файл не найден")
    path.unlink()
    return {"ok": True}


# ---------- формат «ТОП N» ----------

class TopReq(BaseModel):
    project: str
    moments: list[int]
    title: str
    preset: Preset
    photo: str = ""  # имя файла из фото стримера


def top_job(pid: str, req: TopReq) -> None:
    p = state["projects"][pid]
    try:
        moments = sorted((Moment.model_validate(p["moments"][i]) for i in req.moments), key=lambda m: -m.score)
        owner = p.get("streamer") or "common"
        photo = asset_path(owner, "photos", req.photo) if req.photo else None
        fonts = ASSETS / owner / "fonts"
        name = f"top_{slug(req.title)}_{uuid.uuid4().hex[:4]}.mp4"
        render_top(Path(p["video"]), moments, req.title, req.preset, DATA / "clips" / pid / name,
                   photo if photo and photo.exists() else None, fonts if fonts.is_dir() else None)
        with lock:
            p.setdefault("clips", []).insert(0, {
                "file": name, "title": req.title, "caption": "", "hashtags": [], "kind": "top",
                "score": max(m.score for m in moments), "profanity": any(m.profanity for m in moments),
                "preset": req.preset.name, "start": moments[-1].start, "end": moments[-1].end,
                "speed": 1.0, "created": time.time()})
        set_status(p, "ready", f"Топ из {len(moments)} моментов готов")
    except Exception as exc:
        traceback.print_exc()
        set_status(p, "error", str(exc)[:500])


@app.post("/api/top")
def make_top(req: TopReq):
    p = project(req.project)
    if p["status"] not in ("ready", "error") or not p.get("video"):
        raise HTTPException(409, "Проект занят — дождитесь окончания текущей задачи")
    if not 2 <= len(req.moments) <= 10 or any(not 0 <= i < len(p["moments"]) for i in req.moments):
        raise HTTPException(400, "Отметьте от 2 до 10 моментов")
    if not req.title.strip():
        raise HTTPException(400, "Введите заголовок топа")
    set_status(p, "render", "Собираю топ…")
    threading.Thread(target=top_job, args=(req.project, req), daemon=True).start()
    return {"ok": True}


# ---------- ролики-примеры ----------

class RefReq(BaseModel):
    source: str
    name: str


def reference_job(rid: str, source: str) -> None:
    r = state["references"][rid]
    try:
        video = fetch(source, DATA / "downloads")
        result = analyze_reference(video, rid, PRESETS, DATA / "references")
        with lock:
            r.update(result, status="ready", message="Пресет сохранён")
    except Exception as exc:
        traceback.print_exc()
        with lock:
            r.update(status="error", message=str(exc)[:500])
    save_state()


@app.post("/api/references")
def new_reference(req: RefReq):
    rid = slug(req.name, "primer")
    with lock:
        state["references"][rid] = {"name": rid, "source": req.source.strip(), "status": "work",
                                    "message": "Разбираю стиль…", "created": time.time()}
    save_state()
    threading.Thread(target=reference_job, args=(rid, req.source.strip()), daemon=True).start()
    return {"name": rid}


@app.get("/references/{name}")
def reference_sheet(name: str):
    path = DATA / "references" / Path(name).name
    if not path.exists():
        raise HTTPException(404, "Нет раскадровки")
    return FileResponse(path)


class CapcutReq(BaseModel):
    project: str
    moment: int
    preset: Preset
    drafts_dir: str = ""


@app.post("/api/capcut")
def capcut(req: CapcutReq):
    """Экспорт момента в черновик CapCut для ручной доводки."""
    p = project(req.project)
    if not p.get("video") or not 0 <= req.moment < len(p["moments"]):
        raise HTTPException(400, "Выберите момент")
    target = Path(req.drafts_dir).expanduser() if req.drafts_dir.strip() else default_drafts_dir()
    found = target is not None
    target = target or DATA / "capcut_drafts"
    try:
        path = export_draft(Path(p["video"]), Moment.model_validate(p["moments"][req.moment]),
                            req.preset, transcript_of(p), target)
    except ImportError:
        raise HTTPException(400, "Не установлена библиотека pycapcut (pip install pycapcut)")
    return {"path": str(path), "capcut_found": found}


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
    manager.start()
    print("Панель: http://127.0.0.1:8765")
    uvicorn.run(app, host="127.0.0.1", port=8765, log_level="warning")


if __name__ == "__main__":
    main()
