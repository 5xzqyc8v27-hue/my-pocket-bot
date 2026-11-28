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
from .face import apply_face, find_face
from . import blotato
from . import health as health_mod
from . import history, kick, llm, packs, rules
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
         "memes": "Мем-вставки", "music": "Музыка", "voice": "Озвучка",
         "effects": "Эффекты и переходы", "luts": "Цветокор (LUT)"}


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
    for q in state.get("queue", {}).values():
        if q.get("status") == "posting" and not q.get("submission"):
            q.update(status="failed", message="Панель перезапустилась во время отправки — проверьте аккаунт и повторите")
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


def fonts_of(p: dict) -> list[Path]:
    """Папки со шрифтами из материалов: стримера проекта и общие."""
    return [ASSETS / owner / "fonts" for owner in dict.fromkeys([p.get("streamer") or "common", "common"])]


LANG_HINTS = {"uk": " Названия, подписи и хуки пиши на украинском языке.",
              "en": " Названия, подписи и хуки пиши на английском языке."}


def rules_of(p: dict) -> dict:
    """Правила конкурса стримера, к которому относится проект (пустой словарь, если правил нет)."""
    return state["streamers"].get(p.get("streamer") or "", {})


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
                tr = transcribe(video, opts["whisper"], None if opts["lang"] in ("", "auto") else opts["lang"])
                tr.duration = tr.duration or info["duration"]
            except ImportError:
                p["warning"] = "faster-whisper не установлен — без субтитров и выбора по смыслу"
            except Exception as exc:  # распознавание не должно ронять весь анализ
                p["warning"] = f"Речь не распозналась ({str(exc)[:120]}) — без субтитров"
        set_status(p, "scenes", "Ищу лицо стримера и смены сцен…")
        try:
            p["face"] = find_face(video, 0, min(info["duration"], 600))
        except Exception:
            p["face"] = None
        p["scenes"] = detect_scenes(video)
        set_status(p, "moments", "Ищу сильные моменты…")
        moments, used = pick(video, tr, opts["picker"], opts["count"], opts["min"], opts["max"],
                             log=lambda msg: set_status(p, "moments", msg),
                             focus=(opts.get("focus", "") + LANG_HINTS.get(opts.get("lang", ""), "")).strip(),
                             avoid=rules_of(p).get("rule_avoid", ""))
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
                    if m["score"] >= opts.get("auto_min_score", 0) and m.get("category") != "чат"]
            if good:
                render_job(pid, good, Preset.load(opts["auto_preset"], PRESETS), varied=len(good) > 1)
    except Exception as exc:
        traceback.print_exc()
        set_status(p, "error", str(exc)[:500])


def render_job(pid: str, indexes: list[int], base: Preset, varied: bool = False, styles: int = 1,
               common_title: str = "", merge: bool = False) -> None:
    p = state["projects"][pid]
    video, tr = Path(p["video"]), transcript_of(p)
    out_dir = DATA / "clips" / pid
    try:
        if merge and len(indexes) >= 2:  # отмеченные моменты — одним роликом, по порядку эфира
            preset = apply_face(base, p.get("face"))
            pieces = [(video, Moment.model_validate(p["moments"][i]), tr) for i in sorted(indexes)]
            title = common_title.strip()
            set_status(p, "render", f"Собираю нарезку из {len(pieces)} моментов")
            name = f"narezka_{slug(title or pieces[0][1].title)}_{preset.name}_{uuid.uuid4().hex[:4]}.mp4"
            render_compilation(pieces, title, preset, out_dir / name, None, filler_of(p, preset), resolve_asset)
            first = pieces[0][1]
            with lock:
                p.setdefault("clips", []).insert(0, {
                    "file": name, "title": title or first.title, "caption": first.caption, "hashtags": first.hashtags,
                    "score": max(m.score for _, m, _ in pieces), "profanity": any(m.profanity for _, m, _ in pieces),
                    "preset": preset.name + " · нарезка", "start": first.start, "end": pieces[-1][1].end,
                    "speed": 1.0, "created": time.time()})
            set_status(p, "ready", f"Нарезка из {len(pieces)} моментов готова")
            return
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
            preset = apply_face(preset, p.get("face"))
            rule = rules_of(p)  # правила — подсказка: монтируем как просили, но предупреждаем
            notes = []
            if rule.get("rule_no_split") and preset.layout == "split":
                notes.append("сплит с отвлекающим видео по правилам этого стримера могут не принять")
            if rule.get("rule_no_ads") and preset.banner:
                notes.append("баннер в ролике по правилам этого стримера может считаться рекламой")
            if preset.ad_break and rule.get("rule_ad_len"):
                from .media import probe
                banner = resolve_asset(preset.ad_break)
                extra = (preset.ad_break_len if banner.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp")
                         else probe(banner)["duration"]) if banner.is_file() else 0
                note = rules.ad_len_note(m.duration / preset.speed + preset.teaser + extra, rule["rule_ad_len"])
                if note:
                    notes.append(note)
            if m.risk:
                notes.append(f"момент задевает тему, за которую могут не заплатить: {m.risk}")
            if notes:
                p["warning"] = "Обратите внимание: " + "; ".join(dict.fromkeys(notes))
            set_status(p, "render", f"Монтирую {n} из {len(indexes)}: {m.title or 'без названия'}")
            name = f"{int(m.start):05d}_{slug(m.title)}_{preset.name}_{uuid.uuid4().hex[:4]}.mp4"
            render_clip(video, m, preset, out_dir / name, tr, filler_of(p, preset, m), resolve=resolve_asset, fonts=fonts_of(p))
            with lock:
                p.setdefault("clips", []).insert(0, {
                    "file": name, "title": m.title, "caption": m.caption, "hashtags": m.hashtags,
                    "score": m.score, "profanity": m.profanity,
                    "preset": preset.name + (" · вариация" if varied else ""), "ad_break": bool(preset.ad_break),
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
    auto_min_score: int = 0  # при ручном добавлении монтируем все найденные моменты


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
    merge: bool = False  # склеить выбранные моменты в один ролик


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
                "themes": state["themes"], "theme_status": theme_status, "queue": queue_view(),
                "story_status": story_status, "blotato": blotato.enabled(), "pack_status": pack_status, "uniq": {**uniq_status, **uniq_files()}, "packs": saved_packs(), "mac_voice": story_mod.platform.system() == "Darwin",
                "edge_voices": story_mod.EDGE_VOICES if shutil.which("edge-tts") else {},
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
        ("sherpa-onnx", "Запасное распознавание речи (модель с GitHub)", lib("sherpa_onnx"), False,
         "pip install sherpa-onnx"),
        ("edge-tts", "Бесплатная онлайн-озвучка для рассказов", shutil.which("edge-tts") is not None, False,
         "pip install edge-tts"),
        ("Claude по API", "Выбор моментов за кредиты", bool(os.environ.get("ANTHROPIC_API_KEY")), False,
         "export ANTHROPIC_API_KEY=ваш_ключ — перед запуском панели"),
        ("Claude по подписке", "Выбор моментов и разбор примеров без кредитов", shutil.which("claude") is not None,
         False, "Установить Claude Code, запустить claude и выполнить /login"),
        ("Ollama", "Локальная нейросеть, бесплатно", ollama_available(), False,
         "brew install ollama && ollama serve && ollama pull qwen2.5:7b"),
        ("OpenCV", "Умный кроп по лицу", lib("cv2"), False, 'pip install "opencv-python-headless<5"'),
        ("Blotato", "Автовыкладка в TikTok, YouTube, Instagram", blotato.enabled(), False,
         "export BLOTATO_API_KEY=ваш_ключ — перед запуском панели; затем включить автовыкладку у аккаунтов"),
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
    req.preset = apply_face(req.preset, p.get("face"))
    try:
        render_frame(Path(p["video"]), m, req.preset, out, transcript_of(p), filler_of(p, req.preset, m), req.at, fonts_of(p), resolve_asset)
    except RuntimeError as exc:
        raise HTTPException(400, str(exc)[-400:])
    return FileResponse(out, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@app.post("/api/projects/{pid}/face")
def refind_face(pid: str):
    """Заново ищет лицо стримера (например, если вебку переставили)."""
    p = project(pid)
    if not p.get("video"):
        raise HTTPException(400, "Видео ещё не готово")
    try:
        face = find_face(Path(p["video"]), 0, min(p.get("duration", 60), 600))
    except RuntimeError as exc:
        raise HTTPException(400, str(exc))
    with lock:
        p["face"] = face
    save_state()
    return {"face": face}


@app.get("/api/projects/{pid}/zip")
def clips_zip(pid: str):
    """Все готовые ролики проекта одним архивом."""
    import zipfile
    p = project(pid)
    files = [DATA / "clips" / pid / c["file"] for c in p.get("clips", [])]
    files = [f for f in files if f.exists()]
    if not files:
        raise HTTPException(404, "Готовых роликов нет")
    out = DATA / "preview" / f"{pid}.zip"
    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_STORED) as z:
        for f in files:
            z.write(f, f.name)
    return FileResponse(out, media_type="application/zip", filename=f"{slug(p['title'], 'clips')}.zip")


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
    mine = set()
    if ASSETS.is_dir():  # шрифты из материалов показываем первыми
        for f in ASSETS.glob("*/fonts/*.[to]tf"):
            family = ""
            if shutil.which("fc-scan"):
                family = subprocess.run(["fc-scan", "--format", "%{family}", str(f)], capture_output=True,
                                        text=True).stdout.split(",")[0].strip()
            mine.add(family or f.stem)
    system = sorted(n for n in names - mine if n and not n.startswith("."))[:400]
    return sorted(mine) + (system or ["Arial", "Helvetica", "Impact"])


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
    threading.Thread(target=render_job, args=(req.project, req.moments, req.preset, req.vary, max(1, min(req.styles, 5)), req.common_title, req.merge),
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
    lang: str = "ru"  # язык речи стримера: ru, uk, en или auto
    # правила конкурса стримера
    rule_link: str = ""       # обязательная ссылка в подписи, например «twitch: ZLOYn»
    rule_no_split: bool = False  # запрещён сплит с отвлекающим видео
    rule_no_ads: bool = False    # запрещены баннеры и реклама в ролике
    rule_avoid: str = ""      # темы, за которые не платят
    rule_days: int = 0        # сколько дней принимаются ролики (0 — без срока)
    rule_tiers: str = ""      # пороги выплат «просмотры:нарезка$:мем$» через запятую
    rule_rates: str = ""      # оплата за просмотры «формат:$ за 1000:минимум просмотров» через запятую
    rule_ad_len: str = ""     # допустимая длина ролика с баннером, секунд: «15-60»


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
                      "whisper": st["whisper"], "lang": st.get("lang") or "ru", "focus": st["focus"]})
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
        render_top(Path(p["video"]), moments, req.title, apply_face(req.preset, p.get("face")), DATA / "clips" / pid / name,
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


class ClipsTopReq(BaseModel):
    clips: list[dict] = []  # отмеченные клипы [{url, title}]; пусто — панель сама берёт лучшие из разных эфиров
    count: int = 4
    period: str = "all"
    title: str = ""
    seconds: float = 11.5  # сколько секунд брать из каждого клипа
    preset: Preset | None = None


def short_label(text: str, limit: int = 24) -> str:
    """Подпись места в топе: коротко и без обрыва посреди слова."""
    words, out = text.strip().split(), ""
    for word in words:
        if out and len(out) + len(word) + 1 > limit:
            break
        out = (out + " " + word).strip()
    return (out or text.strip()[:limit]).capitalize()


def pick_top_clips(entries: list[dict], count: int) -> list[dict]:
    """Лучшие клипы по просмотрам, не больше одного с каждого дня — чтобы топ был из разных эфиров."""
    out, days = [], set()
    for e in sorted(entries, key=lambda e: -(e.get("views") or 0)):
        day = int((e.get("ts") or 0) // 86400)
        if day in days or not 6 <= (e.get("duration") or 0) <= 90:
            continue
        days.add(day)
        out.append(e)
        if len(out) == count:
            break
    return out


def clips_top_job(pid: str, st: dict, req: ClipsTopReq) -> None:
    from .top import best_window, join_pieces
    p = state["projects"][pid]
    try:
        chosen = req.clips
        if not chosen:
            set_status(p, "download", "Ищу лучшие клипы канала…")
            entries = (kick.clip_entries(st["channel"], req.period) if st["platform"] == "kick"
                       else history.list_clips(st["channel"], req.period))
            chosen = pick_top_clips(entries, max(2, min(req.count, 10)))
        if len(chosen) < 2:
            raise RuntimeError("Для топа нужно минимум два клипа — за этот период их не нашлось")
        pieces, labels = [], []
        for n, c in enumerate(chosen, 1):
            set_status(p, "download", f"Скачиваю клип {n} из {len(chosen)}: {c.get('title', '')[:40]}")
            video = fetch(c["url"], DATA / "sources" / "clips")
            start, end = best_window(video, probe(video)["duration"], req.seconds)
            pieces.append((video, start, end))
            labels.append(short_label(c.get("title") or f"Момент {n}"))
        set_status(p, "render", "Склеиваю моменты…")
        joined = DATA / "sources" / f"{pid}.mp4"
        spans = join_pieces(pieces, joined)
        moments = [Moment(start=a, end=b, title=label, caption="", hashtags=[], score=max(1, 100 - k * 5), reason="клип зрителей")
                   for k, ((a, b), label) in enumerate(zip(spans, labels))]
        info = probe(joined)
        with lock:
            p.update(video=str(joined), moments=[m.model_dump() for m in moments], **info)
        lang = st.get("lang") or "ru"
        title = req.title.strip() or (f"ТОП {len(moments)} МОМЕНТИ {st['name']} НАЙКРАЩЕ В КІНЦІ" if lang == "uk"
                                      else f"ТОП {len(moments)} МОМЕНТОВ {st['name']} ЛУЧШЕЕ В КОНЦЕ")
        preset = (req.preset or Preset.load("center", PRESETS)).model_copy(update={"layout": "center"})
        if preset.content_zoom < 1.3:
            preset = preset.model_copy(update={"content_zoom": 1.7})
        fonts = ASSETS / st["id"] / "fonts"
        fonts = fonts if fonts.is_dir() else ASSETS / "common" / "fonts"
        name = f"top_{slug(title)}_{uuid.uuid4().hex[:4]}.mp4"
        render_top(joined, moments, title, preset, DATA / "clips" / pid / name, None, fonts if fonts.is_dir() else None)
        with lock:
            p.setdefault("clips", []).insert(0, {
                "file": name, "title": title, "caption": "", "hashtags": [], "kind": "top", "score": 100,
                "profanity": False, "preset": preset.name, "start": 0, "end": moments[-1].end, "speed": 1.0,
                "created": time.time()})
        set_status(p, "ready", f"Топ из {len(moments)} клипов готов")
    except Exception as exc:
        traceback.print_exc()
        set_status(p, "error", str(exc)[:500])


@app.post("/api/streamers/{sid}/clips_top")
def clips_top(sid: str, req: ClipsTopReq):
    """ТОП из клипов зрителей: лучшие моменты разных эфиров одним роликом со списком мест."""
    st = state["streamers"].get(sid)
    if not st:
        raise HTTPException(404, "Стример не найден")
    if st["platform"] not in ("twitch", "kick"):
        raise HTTPException(400, "Клипы зрителей доступны для Twitch и Kick")
    pid = uuid.uuid4().hex[:8]
    with lock:
        state["projects"][pid] = {"id": pid, "source": "клипы зрителей", "title": f"ТОП клипов · {st['name']}",
                                  "status": "queued", "message": "В очереди", "moments": [], "clips": [], "filler": "",
                                  "streamer": sid, "created": time.time()}
    save_state()
    threading.Thread(target=clips_top_job, args=(pid, st, req), daemon=True).start()
    return {"id": pid}


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
                           filler_of(home, req.preset), resolve_asset)
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
    url: str | None = None
    views: int | None = None
    meme: bool | None = None


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
                caption = " ".join(x for x in (clip.get("caption") or clip["title"], tags) if x)
                state["queue"][qid] = {"id": qid, "project": p["id"], "file": clip["file"], "account": aid,
                                       "title": clip["title"], "when": when, "status": "scheduled",
                                       "meme": "chat" in clip.get("preset", ""), "views": 0, "url": "",
                                       "caption": rules.with_link(caption, rules_of(p).get("rule_link", ""))}
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
            q.pop("message", None)
            q["status"] = patch.status
            if patch.status == "published":
                q["published_at"] = time.time()
        if patch.shift_hours:
            q["when"] += patch.shift_hours * 3600
            q["status"] = "scheduled"
        if patch.caption is not None:
            q["caption"] = patch.caption
        for key in ("url", "views", "meme"):
            if getattr(patch, key) is not None:
                q[key] = getattr(patch, key)
    save_state()
    return q


@app.delete("/api/queue/{qid}")
def delete_queue(qid: str):
    with lock:
        state["queue"].pop(qid, None)
    save_state()
    return {"ok": True}


def queue_view() -> dict:
    """Очередь с расчётом выплаты для вышедших роликов."""
    out = {}
    for qid, q in state["queue"].items():
        row = dict(q)
        rule = rules_of(state["projects"].get(q["project"], {}))
        if q["status"] == "published" and rule.get("rule_rates"):
            clips = state["projects"].get(q["project"], {}).get("clips", [])
            banner = any(c["file"] == q["file"] and c.get("ad_break") for c in clips)
            row["payout"] = rules.payout_rate(int(q.get("views") or 0), rule["rule_rates"], banner)
        elif q["status"] == "published" and rule.get("rule_tiers"):
            row["payout"] = rules.payout(int(q.get("views") or 0), q.get("published_at", time.time()),
                                         rule["rule_tiers"], int(rule.get("rule_days") or 0), bool(q.get("meme")))
        out[qid] = row
    return out


@app.post("/api/queue/{qid}/views")
def refresh_views(qid: str):
    """Подтянуть просмотры вышедшего ролика по его ссылке."""
    q = state["queue"].get(qid)
    if not q or not q.get("url"):
        raise HTTPException(400, "Сначала укажите ссылку на вышедший ролик")
    try:
        proc = subprocess.run(["yt-dlp", "--dump-single-json", "--skip-download", "--no-playlist", q["url"]],
                              capture_output=True, text=True, timeout=120)
        views = int(json.loads(proc.stdout).get("view_count") or 0)
    except FileNotFoundError:
        raise HTTPException(400, "yt-dlp не установлен (pip install yt-dlp)")
    except Exception:
        raise HTTPException(400, "Не удалось получить просмотры — впишите число вручную")
    with lock:
        q["views"] = views
    save_state()
    return {"views": views}


@app.get("/api/rules/{name}")
def rule_template(name: str):
    if name not in rules.TEMPLATES:
        raise HTTPException(404, "Такого шаблона правил нет")
    return rules.TEMPLATES[name]


@app.get("/api/blotato/accounts")
def blotato_accounts(platform: str):
    """Аккаунты из кабинета Blotato — чтобы связать их с аккаунтами панели."""
    if not blotato.enabled():
        raise HTTPException(400, "Ключ Blotato не задан: запустите панель с BLOTATO_API_KEY=ваш_ключ")
    try:
        return blotato.list_accounts(platform)
    except RuntimeError as exc:
        raise HTTPException(400, str(exc)[:300])


def publish_loop() -> None:
    """Автовыкладка: подошло время — отправляем ролик через Blotato и следим за статусом заявки."""
    while True:
        time.sleep(30)
        if not blotato.enabled():
            continue
        for q in list(state["queue"].values()):
            acc = state["accounts"].get(q["account"], {})
            try:
                if q["status"] == "scheduled" and q["when"] <= time.time() and acc.get("autopost") and acc.get("blotato_id"):
                    clip = DATA / "clips" / q["project"] / q["file"]
                    if not clip.exists():
                        raise RuntimeError("Файл ролика не найден")
                    with lock:
                        q["status"] = "posting"
                    save_state()
                    sub = blotato.publish(clip, acc["platform"], acc["blotato_id"], q["caption"], q["title"])
                    with lock:
                        q.update(submission=sub, message="Отправлено, жду подтверждения площадки")
                elif q["status"] == "posting" and q.get("submission"):
                    st = blotato.status(q["submission"])
                    if st["state"] == "published":
                        with lock:
                            q.update(status="published", published_at=time.time(), url=st["url"], message="")
                    elif st["state"] == "failed":
                        with lock:
                            q.update(status="failed", message=st["error"] or "Площадка отклонила публикацию")
                else:
                    continue
            except Exception as exc:
                traceback.print_exc()
                with lock:
                    q.update(status="failed", message=str(exc)[:300])
            save_state()


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
        if req.voice and not req.voice.startswith(("edge:", "mac")):
            voice = resolve_asset(req.voice)
        else:
            story_status["message"] = "Озвучиваю…"
            voice = story_mod.speak(req.script, work / f"voice_{uuid.uuid4().hex[:6]}.wav", req.voice)
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
    if req.voice and not req.voice.startswith(("edge:", "mac")) and not resolve_asset(req.voice).exists():
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
    clean: bool = False  # готовый вертикальный ролик без надписей + хук и субтитры дорожками


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
        m = Moment.model_validate(p["moments"][req.moment])
        if req.clean:
            from .capcut import export_clean_draft
            path = export_clean_draft(Path(p["video"]), m, apply_face(req.preset, p.get("face")), transcript_of(p),
                                      target, resolve=resolve_asset, fonts=fonts_of(p))
        else:
            path = export_draft(Path(p["video"]), m, req.preset, transcript_of(p), target, resolve=resolve_asset)
    except ImportError:
        raise HTTPException(400, "Не установлена библиотека pycapcut (pip install pycapcut)")
    skipped = path / "не_добавлено.txt"
    return {"path": str(path), "capcut_found": found,
            "skipped": skipped.read_text(encoding="utf-8").splitlines() if skipped.exists() else []}


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


INSERT_SYSTEM = """Ты монтируешь мемную нарезку стримера. Тебе дают реплики ролика с временем и список
файлов из библиотеки: мем-видео, картинки, эмодзи, звуки (смысл файла понятен по названию).
Подбери 1-4 вставки, которые усиливают шутку: реакция на фразу, звук на провал, эмодзи на эмоцию.
Вставка должна подходить по смыслу к словам в этот момент; если ничего не подходит — верни пустой список.
Не ставь вставки чаще, чем раз в 2 секунды, и не в первую секунду.
Ответь только JSON: {"inserts": [{"file": "точное имя из списка", "at": секунда_от_начала_ролика, "why": "почему"}]}"""


def smart_inserts(m: dict, tr: Transcript | None, pool: dict, peaks: list[float]) -> list[Insert]:
    """Вставки по смыслу: Claude выбирает файл под конкретную реплику. Пусто, если Claude недоступен."""
    files = pool["visual"] + pool["sounds"]
    if not tr or not files or not llm.available():
        return []
    length = m["end"] - m["start"]
    lines = [f"[{s.start - m['start']:.1f}] {s.text}" for s in tr.segments if s.end > m["start"] and s.start < m["end"]]
    if not lines:
        return []
    names = {ref.split("/", 1)[1]: ref for ref in files}
    prompt = (f"Длина ролика {length:.0f} с.\nРеплики:\n" + "\n".join(lines) +
              f"\n\nЗапинки и громкие места (секунды): {', '.join(f'{p:.1f}' for p in peaks) or 'нет'}" +
              "\n\nБиблиотека:\n" + "\n".join(sorted(names)))
    try:
        reply = llm.ask(prompt, INSERT_SYSTEM, 1500)
        data = json.loads(reply[reply.find("{"):reply.rfind("}") + 1])
    except Exception:
        return []
    out: list[Insert] = []
    for item in data.get("inserts", [])[:4]:
        ref = names.get(str(item.get("file", "")).strip())
        try:
            at = float(item.get("at", -1))
        except (TypeError, ValueError):
            continue
        if ref and 0.5 <= at < length - 0.5:
            sound = "/sounds/" in ref
            out.append(Insert(file=ref, at=round(at, 1), duration=2.0 if sound else 1.5,
                              place="top" if "/emoji/" in ref else "center", scale=0.3 if "/emoji/" in ref else 0.5))
    return out


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
    from .events import find_stutters
    tr = transcript_of(p)
    peaks = [e["t"] for e in find_stutters(tr.words_between(m["start"], m["end"]), m["start"])][:3] if tr else []
    for t in loud_peaks(Path(p["video"]), m["start"], m["end"] - m["start"], count=3):  # добираем громкими местами
        if len(peaks) < 3 and all(abs(t - x) > 2 for x in peaks):
            peaks.append(t)
    peaks = sorted(peaks) or [round((m["end"] - m["start"]) * 0.5, 2)]
    smart = smart_inserts(m, tr, pool, peaks)
    if smart:
        with lock:
            m["inserts"] = [x.model_dump() for x in smart]
        save_state()
        return m
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
    if st["platform"] not in ("twitch", "kick"):
        raise HTTPException(400, "История трансляций доступна для Twitch и Kick")
    cached = history_cache.get(sid)
    if cached and not refresh and time.time() - cached["at"] < 1800:
        return cached["items"]
    try:
        items = (kick.history_entries(st["channel"]) if st["platform"] == "kick"
                 else history.list_vods(st["platform"], st["channel"]))
    except FileNotFoundError:
        raise HTTPException(400, "yt-dlp не установлен (pip install yt-dlp)")
    except Exception as exc:
        raise HTTPException(400, str(exc)[:300])
    history_cache[sid] = {"at": time.time(), "items": items}
    return items


@app.get("/api/streamers/{sid}/clips")
def streamer_clips(sid: str, period: str = "7d"):
    """Популярные клипы зрителей канала — готовая подсказка, какие моменты смешные."""
    st = state["streamers"].get(sid)
    if not st:
        raise HTTPException(404, "Стример не найден")
    if st["platform"] not in ("twitch", "kick"):
        raise HTTPException(400, "Клипы зрителей доступны для Twitch и Kick")
    try:
        if st["platform"] == "kick":
            return kick.clip_entries(st["channel"], period)
        return history.list_clips(st["channel"], period)
    except FileNotFoundError:
        raise HTTPException(400, "yt-dlp не установлен (pip install yt-dlp)")
    except Exception as exc:
        raise HTTPException(400, str(exc)[:300])


# ---------- импорт пака с Google Диска ----------

pack_status: dict = {"busy": False, "message": ""}


def saved_packs() -> list[dict]:
    """Паки из файла packs.json в корне проекта — чтобы подключать их одной кнопкой."""
    try:
        return json.loads((ROOT / "packs.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


class PackReq(BaseModel):
    url: str
    owner: str = "common"
    kinds: list[str] = ["memes", "sounds", "music", "fonts", "fillers", "emoji", "effects", "luts"]


@app.post("/api/packs/gdrive")
def import_pack(req: PackReq):
    """Скачивает общую папку Google Диска или Яндекс Диска и раскладывает файлы по разделам материалов."""
    if pack_status["busy"]:
        raise HTTPException(409, "Импорт уже идёт")
    try:
        packs.check_url(req.url)
    except RuntimeError as exc:
        raise HTTPException(400, str(exc))
    kinds = [k for k in req.kinds if k in packs.KIND_NAMES]
    if not kinds:
        raise HTTPException(400, "Выберите, что импортировать")

    def job():
        try:
            def progress(n, total, name):
                pack_status["message"] = f"Скачиваю {n} из {total}: {name}"
            res = packs.import_pack(req.url, ASSETS, slug(req.owner, "common"), kinds, progress)
            pack_status["message"] = (f"Готово: скачано {res['downloaded']}, уже было {res['skipped']}"
                                      + (f", не удалось {res['failed']}" if res["failed"] else ""))
        except ImportError:
            pack_status["message"] = "Ошибка: не установлен gdown (pip install gdown)"
        except Exception as exc:
            traceback.print_exc()
            pack_status["message"] = f"Ошибка: {str(exc)[:300]}"
        pack_status["busy"] = False

    pack_status.update(busy=True, message="Читаю список файлов…")
    threading.Thread(target=job, daemon=True).start()
    return {"ok": True}


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
    blotato_id: str = ""  # какой аккаунт Blotato соответствует этому аккаунту
    autopost: bool = False  # выкладывать самому по расписанию (нужен ключ Blotato)


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


# ---------- уникализатор: пачка готовых видео -> несколько копий каждого ----------

UNIQ = DATA / "uniq"
uniq_status: dict = {"busy": False, "stop": False, "message": "", "done": 0, "total": 0}
uniq_proc: dict = {"p": None}


def uniq_files() -> dict:
    def rows(folder: Path) -> list[dict]:
        if not folder.is_dir():
            return []
        return [{"name": f.name, "mb": round(f.stat().st_size / 1e6, 1)}
                for f in sorted(folder.iterdir(), key=lambda f: -f.stat().st_mtime) if f.is_file()]
    return {"in": rows(UNIQ / "in"), "out": rows(UNIQ / "out"), "bg": rows(UNIQ / "bg")}


@app.put("/api/uniq/{box}")
async def uniq_upload(box: str, name: str, request: Request):
    """Загрузка видео для обработки (in) или фона (bg)."""
    if box not in ("in", "bg"):
        raise HTTPException(400, "Неизвестный раздел")
    target = UNIQ / box / (slug(Path(name).stem, "video") + Path(name).suffix.lower())
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "wb") as f:
        async for chunk in request.stream():
            f.write(chunk)
    return {"name": target.name}


class UniqPathReq(BaseModel):
    path: str


@app.post("/api/uniq/add")
def uniq_add(req: UniqPathReq):
    """Добавить файл или целую папку с этого компьютера — без копирования через браузер."""
    from .looks import collect_videos
    found = collect_videos([req.path.strip()])
    if not found:
        raise HTTPException(400, "По этому пути видео не найдено")
    (UNIQ / "in").mkdir(parents=True, exist_ok=True)
    for f in found:
        link = UNIQ / "in" / f.name
        if not link.exists():
            try:
                link.symlink_to(f.resolve())
            except OSError:
                shutil.copyfile(f, link)
    return {"added": len(found)}


@app.delete("/api/uniq/{box}")
def uniq_clear(box: str, name: str = ""):
    if box not in ("in", "out", "bg") or uniq_status["busy"]:
        raise HTTPException(400, "Сейчас нельзя" if uniq_status["busy"] else "Неизвестный раздел")
    folder = UNIQ / box
    for f in ([folder / Path(name).name] if name else list(folder.glob("*")) if folder.is_dir() else []):
        if f.is_file() or f.is_symlink():
            f.unlink()
    return {"ok": True}


@app.get("/uniq/{box}/{name}")
def uniq_file(box: str, name: str):
    path = UNIQ / ("out" if box == "out" else "in") / Path(name).name
    if not path.exists():
        raise HTTPException(404, "Файл не найден")
    return FileResponse(path)


@app.get("/api/uniq/zip")
def uniq_zip():
    import zipfile
    files = [f for f in (UNIQ / "out").glob("*") if f.is_file()] if (UNIQ / "out").is_dir() else []
    if not files:
        raise HTTPException(400, "Готовых копий пока нет")
    out = DATA / "preview" / "uniq.zip"
    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_STORED) as z:
        for f in files:
            z.write(f, f.name)
    return FileResponse(out, media_type="application/zip", filename="kopii.zip")


@app.post("/api/uniq/stop")
def uniq_stop():
    uniq_status["stop"] = True
    proc = uniq_proc["p"]
    if proc and proc.poll() is None:
        proc.terminate()
    return {"ok": True}


@app.post("/api/uniq/run")
def uniq_run(cfg: dict):
    """Запуск обработки: каждое видео из списка получает заданное число копий."""
    from . import looks
    from .top import has_audio
    if uniq_status["busy"]:
        raise HTTPException(409, "Обработка уже идёт")
    conf = dict(looks.UNIQ_DEFAULTS)
    for key, default in looks.UNIQ_DEFAULTS.items():
        if key in cfg:
            try:
                conf[key] = type(default)(cfg[key])
            except (TypeError, ValueError):
                raise HTTPException(400, f"Неверное значение: {key}")
    conf["copies"] = max(1, min(conf["copies"], 20))
    conf["zoom_min"], conf["zoom_max"] = max(1.0, conf["zoom_min"]), min(1.3, conf["zoom_max"])
    conf["speed_min"], conf["speed_max"] = max(0.8, conf["speed_min"]), min(1.25, conf["speed_max"])
    if conf["background"]:
        conf["background"] = str(UNIQ / "bg" / Path(conf["background"]).name)
    files = looks.collect_videos([str(UNIQ / "in")])
    if not files:
        raise HTTPException(400, "Сначала добавьте видео")
    (UNIQ / "out").mkdir(parents=True, exist_ok=True)

    def job():
        total, done, failed = len(files) * conf["copies"], 0, 0
        try:
            for src in files:
                audio = has_audio(src)
                for n in range(1, conf["copies"] + 1):
                    if uniq_status["stop"]:
                        raise InterruptedError
                    uniq_status.update(done=done, total=total, message=f"{src.stem}: копия {n} из {conf['copies']}")
                    out = UNIQ / "out" / f"{src.stem}_k{n}_{uuid.uuid4().hex[:4]}.mp4"
                    params = looks.uniq_params(conf, random.Random())
                    proc = subprocess.Popen(looks.uniq_command(src, out, conf, params, audio),
                                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
                    uniq_proc["p"] = proc
                    err = proc.communicate()[1]
                    if uniq_status["stop"]:
                        out.unlink(missing_ok=True)
                        raise InterruptedError
                    if proc.returncode != 0:
                        failed += 1
                        out.unlink(missing_ok=True)
                        print("uniq:", err[-400:])
                    done += 1
            uniq_status["message"] = f"Готово: копий {done - failed}" + (f", не удалось {failed}" if failed else "")
        except InterruptedError:
            uniq_status["message"] = f"Остановлено. Готово копий: {done - failed}"
        except Exception as exc:
            traceback.print_exc()
            uniq_status["message"] = f"Ошибка: {str(exc)[:300]}"
        uniq_status.update(busy=False, stop=False, done=done, total=total)

    uniq_status.update(busy=True, stop=False, done=0, total=len(files) * conf["copies"], message="Запускаю…")
    threading.Thread(target=job, daemon=True).start()
    return {"ok": True}


class CopiesReq(BaseModel):
    count: int = 3
    look: str = ""


@app.post("/api/copies/{pid}/{name}")
def clip_copies(pid: str, name: str, req: CopiesReq):
    """Несколько копий готового ролика: чуть разные приближение, скорость и цвет."""
    from .looks import make_copy
    p = project(pid)
    clip = next((c for c in p["clips"] if c["file"] == name), None)
    if not clip:
        raise HTTPException(404, "Ролик не найден")
    if p["status"] not in ("ready", "error"):
        raise HTTPException(409, "Проект занят — дождитесь окончания текущей задачи")
    count, src = max(1, min(req.count, 10)), clip_path(pid, name)

    def job():
        try:
            for n in range(1, count + 1):
                set_status(p, "render", f"Делаю копию {n} из {count}")
                new = f"{Path(name).stem}_k{uuid.uuid4().hex[:4]}.mp4"
                params = make_copy(src, src.parent / new, int(time.time() * 1000) + n, req.look)
                with lock:
                    p["clips"].insert(0, {**clip, "file": new, "preset": clip.get("preset", "") + " · копия",
                                          "speed": clip.get("speed", 1.0) * params["speed"], "created": time.time(),
                                          "check": None})
            set_status(p, "ready", f"Копий готово: {count}")
        except Exception as exc:
            traceback.print_exc()
            set_status(p, "error", str(exc)[:500])

    set_status(p, "render", "Делаю копии…")
    threading.Thread(target=job, daemon=True).start()
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


SAMPLE_TITLES = {
    "kalatay_top_den": "Kalatay · ТОП 4 с последнего стрима", "kalatay_top_nedelya": "Kalatay · ТОП 4 недели", "kalatay_top_mesyac": "Kalatay · ТОП 5 месяца",
    "kalatay_den_izvinyaysya": "Kalatay · диалог: Извиняйся (банер)", "kalatay_den_byvshaya": "Kalatay · диалог: Бывшая",
    "kalatay_nedelya_kamera": "Kalatay · диалог: Василь оторвал камеру (банер)",
    "kalatay_dialog_akkaunt": "Kalatay · диалог: за что забанили аккаунт", "kalatay_dialog_zheka": "Kalatay · диалог: Жека в Буковеле (банер)",
    "kalatay_dialog_vamos": "Kalatay · диалог: Вамос про детство (банер)",
    "kalatay_top4": "Kalatay · ТОП 4 моменти", "kalatay_top3": "Kalatay · ТОП 3 приколи",
    "kalatay_hook_monitor": "Kalatay · хук: Розбив монітор", "kalatay_hook_biy": "Kalatay · хук: Бій з тінню",
    "kalatay_1_pryzhok_viry_baner": "Kalatay · Прижок віри (банер UPG)", "kalatay_2_gorobchyk_mem": "Kalatay · Горобчик (мем)",
    "kalatay_3_nova_zadacha_vebka": "Kalatay · Нова задача (вебка + плашка)", "kalatay_4_mama_spalyla_baner": "Kalatay · Мама спалила вік (банер UPG)",
    "kalatay_mem_zadacha_na_raund": "Kalatay · мем: Задача на раунд", "kalatay_mem_neibor_i_mama": "Kalatay · мем: Нейбор и мама",
    "kalatay_mem_rozbyv_monitor": "Kalatay · мем: Розбив монітор", "kalatay_mem_biy_z_tinnyu": "Kalatay · мем: Бій з тінню",
    "kalatay_format_baner_upg": "Kalatay · формат «банер» UPG", "kalatay_vebka_i_igra": "Kalatay · вебка + игра", "kalatay_format_vebka_plashka": "Kalatay · формат «вебка» с плашкой",
    "spokoinaya_narezka": "Спокойная нарезка", "reklamnaya_pauza_demo": "Рекламная пауза (проба)",
    "cvetokor_glitch_emoji_demo": "Цветокор, глитч, эмодзи (проба)", "demo1_neon": "Неон (проба)",
    "demo2_classic": "Классика (проба)", "demo3_top": "ТОП (проба)", "demo4_stutter": "Двойной наезд на запинке (проба)"}


def install_samples(force: bool = False) -> dict:
    """Примеры готовых роликов и стартовые материалы из папки samples — один раз при первом запуске."""
    src, mark = ROOT / "samples", DATA / ".samples_installed"
    if not src.is_dir() or (mark.exists() and not force):
        return {"clips": 0, "assets": 0}
    copied = 0
    for f in (src / "assets").rglob("*"):
        if f.is_file():
            target = ASSETS / f.relative_to(src / "assets")
            if not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(f, target)
                copied += 1
    clips = []
    out = DATA / "clips" / "samples"
    out.mkdir(parents=True, exist_ok=True)
    for f in sorted((src / "clips").glob("*.mp4")):
        if not (out / f.name).exists():
            shutil.copyfile(f, out / f.name)
        clips.append({"file": f.name, "title": SAMPLE_TITLES.get(f.stem, f.stem), "caption": "", "hashtags": [], "score": 0,
                      "profanity": False, "preset": "пример", "start": 0, "end": 0, "speed": 1.0, "created": time.time()})
    with lock:
        state["projects"]["samples"] = {
            "id": "samples", "source": "samples", "title": "Примеры роликов", "status": "ready",
            "message": "Готовые ролики, собранные при разработке", "moments": [], "clips": clips, "filler": "",
            "streamer": "", "created": time.time()}
    DATA.mkdir(parents=True, exist_ok=True)
    mark.write_text("ok")
    save_state()
    return {"clips": len(clips), "assets": copied}


@app.post("/api/samples/install")
def samples_install():
    return install_samples(force=True)


def main() -> None:
    import uvicorn
    load_state()
    try:
        install_samples()
    except OSError:
        traceback.print_exc()
    manager.start()
    threading.Thread(target=trends_loop, daemon=True).start()
    threading.Thread(target=publish_loop, daemon=True).start()
    host = os.environ.get("CLIPPER_HOST", "127.0.0.1")
    if host != "127.0.0.1" and not PASSWORD:
        sys.exit("Чтобы открыть панель в сеть, задайте пароль: CLIPPER_PASSWORD=...")
    print(f"Панель: http://{host}:{PORT}" + (f" · профиль «{PROFILE}»" if PROFILE else ""))
    uvicorn.run(app, host=host, port=PORT, log_level="warning")


if __name__ == "__main__":
    main()
