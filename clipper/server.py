"""Локальная панель: python -m clipper.server  ->  http://127.0.0.1:8765"""
from __future__ import annotations

import json
import random
import shutil
import subprocess
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
from . import health as health_mod
from . import history, llm
from . import story as story_mod
from .schedule import next_slots
from .chat import chat_spikes, find_read_messages, load_chat
from .compile import find_themes, render_compilation
from .media import fetch, probe
from . import trends
from .models import Insert, Moment, Preset, Transcript
from .moments import available_pickers, pick
from .reference import analyze_reference
from .render import render_clip, render_frame
from .streams import StreamManager
from .top import render_top
from .variety import vary

import os
import sys


def _arg(name: str, default: str = "") -> str:
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv[:-1] else default


ROOT = Path(__file__).resolve().parent.parent
PRESETS = ROOT / "presets"
# Профиль — отдельное рабочее пространство пользователя: свои проекты, стримеры, аккаунты и файлы.
PROFILE = re.sub(r"[^\w-]", "", os.environ.get("CLIPPER_PROFILE") or _arg("--profile"))
PASSWORD = os.environ.get("CLIPPER_PASSWORD", "")
PORT = int(_arg("--port", "8765"))
DATA = ROOT / "data" / "profiles" / PROFILE if PROFILE else ROOT / "data"
STATE_FILE = DATA / "state.json"
WEB = Path(__file__).resolve().parent / "web"

app = FastAPI(title="Clipper")


@app.middleware("http")
async def require_password(request: Request, call_next):
    """Если задан CLIPPER_PASSWORD, панель открывается только по паролю (логин любой)."""
    if PASSWORD:
        import base64
        import secrets
        given = request.headers.get("authorization", "")
        try:
            ok = given.startswith("Basic ") and secrets.compare_digest(
                base64.b64decode(given[6:]).decode().split(":", 1)[1], PASSWORD)
        except Exception:
            ok = False
        if not ok:
            return JSONResponse({"detail": "Нужен пароль"}, status_code=401,
                                headers={"WWW-Authenticate": 'Basic realm="Clipper"'})
    return await call_next(request)
lock = threading.RLock()
state: dict = {"projects": {}, "references": {}, "streamers": {}, "watch": {}, "trend_videos": {}, "accounts": {}, "themes": {}, "queue": {}}
ASSETS = DATA / "assets"
KINDS = {"photos": "Фото и вырезки", "emoji": "Эмодзи и стикеры", "fonts": "Шрифты", "sounds": "Звуки",
         "banners": "Баннеры", "fillers": "Залипалки", "templates": "Шаблоны",
         "memes": "Мем-вставки", "music": "Музыка", "voice": "Озвучка"}


def load_state() -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    if STATE_FILE.exists():
        loaded = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        streamers = loaded.pop("streamers", {})
        state.update(loaded)
        state["streamers"].update(streamers)  # тот же словарь, за которым следит менеджер эфиров
    state.setdefault("references", {})
    for st in state["streamers"].values():
        st.update(recording=False, live=False, working="", last_check=0)
    for key in ("watch", "trend_videos", "accounts", "themes", "queue"):
        state.setdefault(key, {})
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


def filler_of(p: dict, preset: Preset | None = None, moment: Moment | None = None) -> Path | None:
    """Залипалка для сплита; для раскладки «чат» — картинка момента или случайное фото стримера."""
    if preset and preset.layout == "chat":
        if moment and moment.image and resolve_asset(moment.image).exists():
            return resolve_asset(moment.image)
        for owner in (p.get("streamer") or "common", "common"):
            photos = sorted((ASSETS / owner / "photos").glob("*")) if (ASSETS / owner / "photos").is_dir() else []
            if photos:
                return random.choice(photos)
        return None
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
        if not video.with_suffix(".chat.jsonl").exists() and "twitch.tv/videos" in p["source"]:
            set_status(p, "moments", "Скачиваю чат трансляции…")
            try:
                history.fetch_vod_chat(p["source"], video)
            except Exception:
                traceback.print_exc()
        chat = load_chat(video.with_suffix(".chat.jsonl"))
        if chat and tr and tr.segments:  # моменты, где стример прочитал сообщение из чата и ответил
            moments = [*moments, *find_read_messages(tr, chat)]
        if chat:  # и места, где чат резко оживился
            spikes = [m for m in chat_spikes(chat, info["duration"])
                      if not any(m.start < x.end and x.start < m.end for x in moments)]
            moments = [*moments, *spikes]
        with lock:
            p["picker"] = used
            p["chat_messages"] = len(chat)
            p["moments"] = [m.model_dump() for m in sorted(moments, key=lambda m: -m.score)]
        set_status(p, "ready", f"Найдено моментов: {len(moments)}")
        if opts.get("auto_preset"):  # сразу монтируем сильные моменты в выбранном формате
            good = [i for i, m in enumerate(p["moments"])
                    if m["score"] >= opts.get("auto_min_score", 60) and m.get("category") != "чат"]
            if good:
                render_job(pid, good, Preset.load(opts["auto_preset"], PRESETS), varied=len(good) > 1)
    except Exception as exc:
        traceback.print_exc()
        set_status(p, "error", str(exc)[:500])


def render_job(pid: str, indexes: list[int], base: Preset, varied: bool = False, styles: int = 1,
               common_title: str = "") -> None:
    p = state["projects"][pid]
    video, tr = Path(p["video"]), transcript_of(p)
    out_dir = DATA / "clips" / pid
    try:
        jobs = [(i, base) for i in indexes]
        if styles > 1:  # тот же момент в других пресетах — каждый со своей вариацией
            others = [x for x in (Preset.load(str(f), PRESETS) for f in sorted(PRESETS.glob("*.json")))
                      if x.name != base.name and x.layout not in ("chat", "split")]
            random.shuffle(others)
            jobs += [(i, o) for i in indexes for o in others[:styles - 1]]
            indexes = [j[0] for j in jobs]
        for n, (i, chosen) in enumerate(jobs, 1):
            m = Moment.model_validate(p["moments"][i])
            if common_title.strip():
                m.title = common_title.strip()
            preset = vary(chosen, seed=int(time.time() * 1000) + n) if (varied or chosen is not base) else chosen
            set_status(p, "render", f"Монтирую {n} из {len(indexes)}: {m.title or 'без названия'}")
            name = f"{int(m.start):05d}_{slug(m.title)}_{preset.name}_{uuid.uuid4().hex[:4]}.mp4"
            render_clip(video, m, preset, out_dir / name, tr, filler_of(p, preset, m), resolve=resolve_asset)
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
    auto_preset: str = ""  # если задан — сильные моменты сразу монтируются в этом пресете
    auto_min_score: int = 60


class MomentPatch(BaseModel):
    image: str | None = None
    chat_user: str | None = None
    chat_text: str | None = None
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
    styles: int = 1  # сколько разных стилей сделать из каждого момента
    common_title: str = ""  # одна надпись для всех роликов пачки


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
                "disk_free_gb": round(shutil.disk_usage(DATA).free / 1e9, 1),
                "watch": state["watch"], "accounts": state["accounts"], "profile": PROFILE,
                "themes": state["themes"], "theme_status": theme_status, "queue": state["queue"],
                "story_status": story_status, "mac_voice": story_mod.platform.system() == "Darwin",
                "fetching": sorted(fetching)}


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
        render_frame(Path(p["video"]), m, req.preset, out, transcript_of(p), filler_of(p, req.preset, m), req.at)
    except RuntimeError as exc:
        raise HTTPException(400, str(exc)[-400:])
    return FileResponse(out, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@app.get("/api/projects/{pid}/frame")
def source_frame(pid: str, at: float = 0.0):
    """Чистый кадр исходника — на нём мышкой выделяют вебку или часть кадра."""
    p = project(pid)
    if not p.get("video"):
        raise HTTPException(400, "Видео ещё не готово")
    out = DATA / "preview" / f"{pid}_src.jpg"
    out.parent.mkdir(parents=True, exist_ok=True)
    t = max(0.0, min(at, p.get("duration", 0) - 0.5))
    proc = subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", f"{t:.2f}", "-i", p["video"], "-frames:v", "1",
                           "-vf", "scale=960:-2", "-q:v", "4", str(out)], capture_output=True)
    if proc.returncode != 0 or not out.exists():
        raise HTTPException(400, "Не удалось получить кадр")
    return FileResponse(out, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@app.get("/api/fonts")
def fonts():
    """Шрифты, установленные в системе, плюс загруженные в материалы."""
    names = set()
    if shutil.which("fc-list"):
        out = subprocess.run(["fc-list", ":", "family"], capture_output=True, text=True).stdout
        names |= {line.split(",")[0].strip() for line in out.splitlines() if line.strip()}
    for folder in ("/System/Library/Fonts", "/Library/Fonts", str(Path.home() / "Library/Fonts")):
        if Path(folder).is_dir():
            names |= {f.stem for f in Path(folder).glob("*.[to]tf")}
    return sorted(n for n in names if n and not n.startswith("."))[:400] or ["Arial", "Helvetica", "Impact"]


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
    threading.Thread(target=render_job, args=(req.project, req.moments, req.preset, req.vary, max(1, min(req.styles, 5)), req.common_title),
                     daemon=True).start()
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
    stamp = re.search(r"_(\d{8}_\d{6})\.ts$", chunk.name)
    log = chunk.parent / "chat.jsonl"
    if stamp and log.exists():  # вырезаем из общего лога чата сообщения этого куска
        begin = time.mktime(time.strptime(stamp.group(1), "%Y%m%d_%H%M%S"))
        part = load_chat(log, begin, probe(chunk)["duration"])
        chunk.with_suffix(".chat.jsonl").write_text(
            "\n".join(json.dumps(m, ensure_ascii=False) for m in part), encoding="utf-8")
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
            if m["score"] >= st["min_score"] and not (st["skip_profanity"] and m["profanity"])
            and m.get("category") != "чат"]  # ролики по чату ждут просмотра человеком
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


# ---------- тематические подборки из нескольких трансляций ----------

theme_status: dict = {"busy": False, "message": ""}


class ThemesReq(BaseModel):
    streamer: str = ""  # пусто — по всем проектам
    count: int = 5


class ThemeRenderReq(BaseModel):
    preset: Preset
    music: str = ""  # материал «владелец/music/имя»


def themes_job(streamer: str, count: int) -> None:
    try:
        sources = []
        for pid, p in list(state["projects"].items()):
            if p.get("video") and (not streamer or p.get("streamer") == streamer):
                tr = transcript_of(p)
                if tr and tr.segments:
                    tr.duration = p.get("duration") or tr.duration  # длина самого видео надёжнее
                    sources.append((pid, p["title"], tr))
        if not sources:
            raise RuntimeError("Нет трансляций с расшифровкой речи — сначала обработайте исходники")
        theme_status["message"] = f"Ищу темы в {len(sources)} трансляциях…"
        found = find_themes(sources[-25:], count)
        with lock:
            for th in found:
                tid = uuid.uuid4().hex[:8]
                state["themes"][tid] = {"id": tid, "streamer": streamer, **th.model_dump(), "created": time.time()}
        theme_status["message"] = f"Найдено тем: {len(found)}"
    except Exception as exc:
        traceback.print_exc()
        theme_status["message"] = f"Ошибка: {str(exc)[:300]}"
    theme_status["busy"] = False
    save_state()


@app.post("/api/themes")
def find_themes_api(req: ThemesReq):
    if theme_status["busy"]:
        raise HTTPException(409, "Поиск тем уже идёт")
    theme_status.update(busy=True, message="Запускаю поиск тем…")
    threading.Thread(target=themes_job, args=(req.streamer, max(1, min(req.count, 10))), daemon=True).start()
    return {"ok": True}


@app.delete("/api/themes/{tid}")
def delete_theme(tid: str):
    with lock:
        state["themes"].pop(tid, None)
    save_state()
    return {"ok": True}


def theme_render_job(tid: str, req: ThemeRenderReq) -> None:
    th = state["themes"][tid]
    try:
        pieces = []
        for piece in th["pieces"]:
            p = state["projects"].get(piece["project"])
            if p and p.get("video") and Path(p["video"]).exists() and piece["end"] <= p.get("duration", 1e9) + 0.5:
                pieces.append((Path(p["video"]), Moment(start=piece["start"], end=piece["end"], title=""), None))
        if len(pieces) < 2:
            raise RuntimeError("Исходные трансляции этой темы удалены или куски выходят за пределы видео")
        home = next(p for p in state["projects"].values() if p.get("video") == str(pieces[0][0]))
        name = f"podborka_{slug(th['title'])}_{uuid.uuid4().hex[:4]}.mp4"
        music = resolve_asset(req.music) if req.music else None
        render_compilation(pieces, th["title"], req.preset, DATA / "clips" / home["id"] / name, music,
                           filler_of(home, req.preset))
        with lock:
            home.setdefault("clips", []).insert(0, {
                "file": name, "title": th["title"], "caption": th.get("why", ""), "hashtags": [], "kind": "theme",
                "score": 0, "profanity": False, "preset": req.preset.name + " · подборка",
                "start": pieces[0][1].start, "end": pieces[0][1].end, "speed": 1.0, "created": time.time()})
            th.update(status="ready", clip=name, clip_project=home["id"], message="Готово")
    except Exception as exc:
        traceback.print_exc()
        with lock:
            th.update(status="error", message=str(exc)[:300])
    save_state()


@app.post("/api/themes/{tid}/render")
def render_theme(tid: str, req: ThemeRenderReq):
    th = state["themes"].get(tid)
    if not th:
        raise HTTPException(404, "Тема не найдена")
    if th.get("status") == "work":
        raise HTTPException(409, "Эта подборка уже монтируется")
    with lock:
        th.update(status="work", message="Монтирую…")
    threading.Thread(target=theme_render_job, args=(tid, req), daemon=True).start()
    return {"ok": True}


# ---------- очередь публикаций ----------

class QueueReq(BaseModel):
    project: str
    files: list[str]
    accounts: list[str] = []  # пусто — все аккаунты, подходящие стримеру проекта
    per_day: int = 2
    from_hour: int = 10
    to_hour: int = 22


class QueuePatch(BaseModel):
    status: str | None = None  # scheduled, published, skipped
    shift_hours: float | None = None
    caption: str | None = None


@app.post("/api/queue")
def add_to_queue(req: QueueReq):
    """Ставит ролики в расписание: для каждого аккаунта — ближайшие свободные слоты."""
    p = project(req.project)
    ids = req.accounts or [a["id"] for a in state["accounts"].values()
                           if not a.get("streamer") or a["streamer"] == p.get("streamer")]
    ids = [i for i in ids if i in state["accounts"]]
    if not ids:
        raise HTTPException(400, "Сначала добавьте аккаунты во вкладке «Аккаунты»")
    clips = [c for c in p.get("clips", []) if c["file"] in req.files]
    if not clips:
        raise HTTPException(400, "Выберите ролики")
    made = 0
    with lock:
        for aid in ids:
            taken = [q["when"] for q in state["queue"].values() if q["account"] == aid and q["status"] != "skipped"]
            fresh = [c for c in clips if not any(q["account"] == aid and q["file"] == c["file"] and
                                                 q["project"] == p["id"] for q in state["queue"].values())]
            for clip, when in zip(fresh, next_slots(taken, len(fresh), req.per_day, req.from_hour, req.to_hour)):
                qid = uuid.uuid4().hex[:8]
                tags = " ".join("#" + t.lstrip("#") for t in clip.get("hashtags", []))
                state["queue"][qid] = {"id": qid, "project": p["id"], "file": clip["file"], "account": aid,
                                       "title": clip["title"], "when": when, "status": "scheduled",
                                       "caption": " ".join(x for x in (clip.get("caption") or clip["title"], tags) if x)}
                made += 1
    save_state()
    return {"added": made}


@app.patch("/api/queue/{qid}")
def patch_queue(qid: str, patch: QueuePatch):
    q = state["queue"].get(qid)
    if not q:
        raise HTTPException(404, "Запись не найдена")
    with lock:
        if patch.status in ("scheduled", "published", "skipped"):
            q["status"] = patch.status
            if patch.status == "published":
                q["published_at"] = time.time()
        if patch.shift_hours:
            q["when"] += patch.shift_hours * 3600
            q["status"] = "scheduled"
        if patch.caption is not None:
            q["caption"] = patch.caption
    save_state()
    return q


@app.delete("/api/queue/{qid}")
def delete_queue(qid: str):
    with lock:
        state["queue"].pop(qid, None)
    save_state()
    return {"ok": True}


@app.post("/api/queue/cleanup")
def cleanup_published(dry_run: bool = True, older_hours: float = 24):
    """Удаляет файлы роликов, которые вышли во всех аккаунтах больше older_hours назад."""
    edge = time.time() - older_hours * 3600
    by_file: dict = {}
    for q in state["queue"].values():
        by_file.setdefault((q["project"], q["file"]), []).append(q)
    targets = [(pid, f) for (pid, f), posts in by_file.items()
               if all(x["status"] in ("published", "skipped") for x in posts)
               and any(x["status"] == "published" for x in posts)
               and max(x.get("published_at", 0) for x in posts) < edge
               and (DATA / "clips" / pid / f).exists()]
    size = sum((DATA / "clips" / pid / f).stat().st_size for pid, f in targets)
    if not dry_run:
        with lock:
            for pid, f in targets:
                (DATA / "clips" / pid / f).unlink()
                if pid in state["projects"]:
                    state["projects"][pid]["clips"] = [c for c in state["projects"][pid]["clips"] if c["file"] != f]
        save_state()
    return {"files": len(targets), "mb": round(size / 1e6, 1), "deleted": not dry_run}


# ---------- ролик-рассказ ----------

story_status: dict = {"busy": False, "message": ""}


class ScriptReq(BaseModel):
    topic: str


class StoryReq(BaseModel):
    script: str
    title: str = ""
    voice: str = ""  # материал «владелец/voice/имя»; пусто — встроенный голос macOS
    projects: list[str]  # из каких исходников брать видеоряд
    preset: Preset
    music: str = ""


@app.post("/api/story/script")
def story_script(req: ScriptReq):
    if not req.topic.strip():
        raise HTTPException(400, "Введите тему")
    try:
        return {"script": llm.ask(f"Тема ролика: {req.topic.strip()}", story_mod.SCRIPT_SYSTEM, 1500)}
    except RuntimeError as exc:
        raise HTTPException(400, str(exc))


def story_job(req: StoryReq) -> None:
    try:
        home = state["projects"][req.projects[0]]
        broll = [Path(state["projects"][pid]["video"]) for pid in req.projects]
        work = DATA / "story"
        work.mkdir(parents=True, exist_ok=True)
        if req.voice:
            voice = resolve_asset(req.voice)
        else:
            story_status["message"] = "Озвучиваю…"
            voice = story_mod.speak(req.script, work / f"voice_{uuid.uuid4().hex[:6]}.wav")
        story_status["message"] = "Монтирую…"
        name = f"rasskaz_{slug(req.title or req.script[:30])}_{uuid.uuid4().hex[:4]}.mp4"
        story_mod.render_story(req.script, voice, broll, req.preset, DATA / "clips" / home["id"] / name,
                               req.title, music=resolve_asset(req.music) if req.music else None)
        with lock:
            home.setdefault("clips", []).insert(0, {
                "file": name, "title": req.title or story_mod.split_sentences(req.script)[0][:60],
                "caption": "", "hashtags": [], "kind": "story", "score": 0, "profanity": False,
                "preset": req.preset.name + " · рассказ", "start": 0, "end": 0, "speed": 1.0, "created": time.time()})
        story_status.update(message="Готово — ролик в проекте «" + home["title"] + "»", project=home["id"])
    except Exception as exc:
        traceback.print_exc()
        story_status["message"] = f"Ошибка: {str(exc)[:300]}"
    story_status["busy"] = False
    save_state()


@app.post("/api/story")
def make_story(req: StoryReq):
    if story_status["busy"]:
        raise HTTPException(409, "Рассказ уже монтируется")
    if len(req.script.split()) < 8:
        raise HTTPException(400, "Сценарий слишком короткий")
    ok = [pid for pid in req.projects if state["projects"].get(pid, {}).get("video")]
    if not ok:
        raise HTTPException(400, "Выберите хотя бы один исходник для видеоряда")
    if req.voice and not resolve_asset(req.voice).exists():
        raise HTTPException(400, "Файл озвучки не найден")
    req.projects = ok
    story_status.update(busy=True, message="Запускаю…")
    threading.Thread(target=story_job, args=(req,), daemon=True).start()
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


def resolve_asset(ref: str) -> Path:
    """«владелец/тип/имя» -> путь к файлу в материалах."""
    parts = ref.split("/")
    if len(parts) == 3 and parts[1] in KINDS:
        return ASSETS / slug(parts[0], "common") / parts[1] / Path(parts[2]).name
    return Path(ref)


# ---------- вставки в момент ----------

class InsertsReq(BaseModel):
    inserts: list[Insert]


@app.put("/api/projects/{pid}/moments/{i}/inserts")
def set_inserts(pid: str, i: int, req: InsertsReq):
    p = project(pid)
    if not 0 <= i < len(p["moments"]):
        raise HTTPException(404, "Момент не найден")
    with lock:
        p["moments"][i]["inserts"] = [x.model_dump() for x in req.inserts]
    save_state()
    return p["moments"][i]


@app.post("/api/projects/{pid}/moments/{i}/auto_inserts")
def auto_inserts(pid: str, i: int):
    """Авто-вставки: на самых громких местах ставим случайный мем или эмодзи и звук из материалов."""
    from .moments import loud_peaks
    p = project(pid)
    if not 0 <= i < len(p["moments"]) or not p.get("video"):
        raise HTTPException(404, "Момент не найден")
    m = p["moments"][i]
    pool: dict[str, list[str]] = {"visual": [], "sounds": []}
    for owner in {p.get("streamer") or "common", "common"}:
        for kind in ("memes", "emoji", "sounds"):
            files = list_assets().get(owner, {}).get(kind, [])
            pool["sounds" if kind == "sounds" else "visual"] += [f"{owner}/{kind}/{f}" for f in files]
    if not pool["visual"] and not pool["sounds"]:
        raise HTTPException(400, "Сначала загрузите мем-вставки, эмодзи или звуки во вкладке «Материалы»")
    peaks = loud_peaks(Path(p["video"]), m["start"], m["end"] - m["start"], count=3) or [
        round((m["end"] - m["start"]) * 0.5, 2)]
    made = []
    for t in peaks:
        if pool["visual"]:
            ref = random.choice(pool["visual"])
            made.append(Insert(file=ref, at=t, duration=1.5, place=random.choice(["top", "center"]),
                               scale=0.3 if "/emoji/" in ref else 0.5))
        if pool["sounds"]:
            made.append(Insert(file=random.choice(pool["sounds"]), at=t, duration=2.0))
    with lock:
        m["inserts"] = [x.model_dump() for x in made]
    save_state()
    return m


IDEA_SYSTEM = """Ты придумываешь смешную картинку-иллюстрацию для мемной нарезки стримера.
Формат ролика: сверху стример читает сообщение из чата и отвечает, снизу — нелепая картинка,
где стример изображён в образе по теме сообщения (например, сообщение про пряники — стример
в виде пряничного человечка; про школу — стример-директор за столом).
Образ должен быть добрым и абсурдным, без унижения, эротики и насилия.
Ответь двумя строками: первая — идея по-русски одной фразой, вторая — готовый запрос для
генератора картинок на английском (вертикальный кадр, фотореализм, лицо берётся с приложенного фото)."""


@app.post("/api/projects/{pid}/moments/{i}/idea")
def image_idea(pid: str, i: int):
    """Идея картинки под момент — её потом генерируют в своём генераторе и загружают в материалы."""
    p = project(pid)
    if not 0 <= i < len(p["moments"]):
        raise HTTPException(404, "Момент не найден")
    m = p["moments"][i]
    tr = transcript_of(p)
    said = " ".join(w.text for w in tr.words_between(m["start"], m["end"])) if tr else ""
    try:
        idea = llm.ask(f"Сообщение из чата: {m.get('chat_text') or '—'}\nЧто говорит стример: {said or m.get('title', '')}",
                       IDEA_SYSTEM, 600)
    except RuntimeError as exc:
        raise HTTPException(400, str(exc))
    with lock:
        m["image_idea"] = idea
    save_state()
    return m


# ---------- история трансляций ----------

history_cache: dict = {}


@app.get("/api/streamers/{sid}/history")
def streamer_history(sid: str, refresh: bool = False):
    st = state["streamers"].get(sid)
    if not st:
        raise HTTPException(404, "Стример не найден")
    if st["platform"] not in ("twitch",):
        raise HTTPException(400, "История трансляций пока доступна только для Twitch")
    cached = history_cache.get(sid)
    if cached and not refresh and time.time() - cached["at"] < 1800:
        return cached["items"]
    try:
        items = history.list_vods(st["platform"], st["channel"])
    except FileNotFoundError:
        raise HTTPException(400, "yt-dlp не установлен (pip install yt-dlp)")
    except Exception as exc:
        raise HTTPException(400, str(exc)[:300])
    history_cache[sid] = {"at": time.time(), "items": items}
    return items


# ---------- материалы по ссылке ----------

fetching: set[str] = set()


class FetchReq(BaseModel):
    url: str


@app.post("/api/assets/{owner}/{kind}/fetch")
def fetch_asset(owner: str, kind: str, req: FetchReq):
    """Скачать мем, звук или залипалку по ссылке прямо в материалы."""
    if kind not in KINDS:
        raise HTTPException(400, "Неизвестный тип материала")
    if not req.url.strip().startswith(("http://", "https://")):
        raise HTTPException(400, "Нужна ссылка, начинающаяся с http")
    target = ASSETS / slug(owner, "common") / kind
    key = f"{owner}/{kind}: {req.url.strip()[:80]}"

    def job():
        try:
            target.mkdir(parents=True, exist_ok=True)
            fmt = ["-x", "--audio-format", "mp3"] if kind == "sounds" else [
                "-f", "bv*[height<=720]+ba/b[height<=720]/b", "--merge-output-format", "mp4"]
            subprocess.run(["yt-dlp", *fmt, "--no-playlist", "--restrict-filenames",
                            "-o", str(target / "%(title).60s.%(ext)s"), req.url.strip()],
                           capture_output=True, timeout=900)
        finally:
            fetching.discard(key)

    fetching.add(key)
    threading.Thread(target=job, daemon=True).start()
    return {"ok": True}


# ---------- мониторинг трендов ----------

class WatchReq(BaseModel):
    kind: str = "channel"  # channel или topic
    value: str
    name: str = ""


def refresh_trends(only: str | None = None) -> None:
    for wid, w in list(state["watch"].items()):
        if only and wid != only:
            continue
        try:
            entries = trends.fetch_entries(trends.source_url(w["kind"], w["value"]))
            with lock:
                trends.merge(state["trend_videos"], entries, wid)
                w.update(updated=time.time(), error="", count=len(entries))
        except FileNotFoundError:
            with lock:
                w.update(updated=time.time(), error="yt-dlp не установлен (pip install yt-dlp)")
        except Exception as exc:
            with lock:
                w.update(updated=time.time(), error=str(exc)[:300])
    with lock:  # ролики старше 45 дней больше не нужны
        old = time.time() - 45 * 86400
        for vid in [k for k, v in state["trend_videos"].items() if (v.get("ts") or v["first_seen"]) < old]:
            del state["trend_videos"][vid]
    save_state()


def trends_loop() -> None:
    while True:
        due = [w for w in state["watch"].values() if time.time() - w.get("updated", 0) > 3 * 3600]
        if due:
            refresh_trends()
        time.sleep(300)


@app.post("/api/watch")
def add_watch(req: WatchReq):
    if req.kind not in ("channel", "topic") or not req.value.strip():
        raise HTTPException(400, "Укажите ссылку на канал или тему")
    wid = slug(req.name or req.value, "source")
    with lock:
        state["watch"][wid] = {"id": wid, "kind": req.kind, "value": req.value.strip(),
                               "name": req.name.strip() or req.value.strip(), "updated": 0, "error": ""}
    save_state()
    threading.Thread(target=refresh_trends, args=(wid,), daemon=True).start()
    return {"id": wid}


@app.delete("/api/watch/{wid}")
def delete_watch(wid: str):
    with lock:
        state["watch"].pop(wid, None)
        for vid in [k for k, v in state["trend_videos"].items() if v.get("source") == wid]:
            del state["trend_videos"][vid]
    save_state()
    return {"ok": True}


@app.post("/api/trends/refresh")
def trends_refresh():
    threading.Thread(target=refresh_trends, daemon=True).start()
    return {"ok": True}


@app.get("/api/trends")
def get_trends(window: str = "24h"):
    if window not in trends.WINDOWS:
        raise HTTPException(400, "Окно: 24h, 48h или 30d")
    with lock:
        return trends.ranking(state["trend_videos"], window)


# ---------- аккаунты соцсетей ----------

class AccountReq(BaseModel):
    platform: str  # youtube, tiktok, instagram
    handle: str
    email: str = ""
    device: str = ""  # на каком телефоне или в каком браузере залогинен
    streamer: str = ""
    notes: str = ""


@app.post("/api/accounts")
def save_account(req: AccountReq):
    if req.platform not in ("youtube", "tiktok", "instagram") or not req.handle.strip():
        raise HTTPException(400, "Укажите площадку и имя аккаунта")
    aid = slug(f"{req.platform}_{req.handle}", "account")
    with lock:
        state["accounts"][aid] = {**req.model_dump(), "id": aid, "streamer": slug(req.streamer, "") if req.streamer else "",
                                  "created": state["accounts"].get(aid, {}).get("created", time.time())}
    save_state()
    return {"id": aid}


@app.delete("/api/accounts/{aid}")
def delete_account(aid: str):
    with lock:
        state["accounts"].pop(aid, None)
    save_state()
    return {"ok": True}


@app.post("/api/accounts/{aid}/check")
def check_account(aid: str):
    """Проверка охватов: сравниваем просмотры последних роликов аккаунта с его обычным уровнем."""
    acc = state["accounts"].get(aid)
    if not acc:
        raise HTTPException(404, "Аккаунт не найден")
    try:
        entries = trends.fetch_entries(health_mod.account_url(acc["platform"], acc["handle"]), limit=25)
        result = health_mod.reach_verdict(entries)
    except FileNotFoundError:
        raise HTTPException(400, "yt-dlp не установлен (pip install yt-dlp)")
    except RuntimeError as exc:
        raise HTTPException(400, str(exc)[:300])
    with lock:
        acc["reach"] = result
    save_state()
    return result


@app.get("/api/ip")
def ip_check():
    try:
        return health_mod.check_ip()
    except RuntimeError as exc:
        raise HTTPException(400, str(exc)[:300])


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
    threading.Thread(target=trends_loop, daemon=True).start()
    host = os.environ.get("CLIPPER_HOST", "127.0.0.1")
    if host != "127.0.0.1" and not PASSWORD:
        sys.exit("Чтобы открыть панель в сеть, задайте пароль: CLIPPER_PASSWORD=...")
    print(f"Панель: http://{host}:{PORT}" + (f" · профиль «{PROFILE}»" if PROFILE else ""))
    uvicorn.run(app, host=host, port=PORT, log_level="warning")


if __name__ == "__main__":
    main()
