from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, Field


class Word(BaseModel):
    start: float
    end: float
    text: str


class Segment(BaseModel):
    start: float
    end: float
    text: str
    words: list[Word] = Field(default_factory=list)


class Transcript(BaseModel):
    language: str = "ru"
    duration: float = 0.0
    segments: list[Segment] = Field(default_factory=list)

    def words_between(self, start: float, end: float) -> list[Word]:
        out: list[Word] = []
        for seg in self.segments:
            if seg.end < start or seg.start > end:
                continue
            words = seg.words or _spread_words(seg)
            # пометки распознавания вроде «[музыка]» в субтитры не идут
            out.extend(w for w in words if w.end > start and w.start < end and not w.text.strip().startswith(("[", "(")))
        return out

    @classmethod
    def load(cls, path: Path) -> "Transcript":
        return cls.model_validate_json(Path(path).read_text(encoding="utf-8"))

    def save(self, path: Path) -> None:
        Path(path).write_text(self.model_dump_json(indent=1), encoding="utf-8")


def _spread_words(seg: Segment) -> list[Word]:
    """Если у сегмента нет пословных таймингов — делим время поровну по длине слов."""
    tokens = seg.text.split()
    if not tokens:
        return []
    total = sum(len(t) for t in tokens)
    t, out = seg.start, []
    for tok in tokens:
        dur = (seg.end - seg.start) * len(tok) / total
        out.append(Word(start=t, end=t + dur, text=tok))
        t += dur
    return out


class Insert(BaseModel):
    """Вставка поверх ролика: мем-видео, картинка, эмодзи или звук."""

    file: str  # материал вида «владелец/тип/имя» или полный путь
    at: float = 0.0  # секунда от начала ролика
    duration: float = 1.5
    place: str = "center"  # full, top, center, bottom
    scale: float = 0.45  # ширина относительно кадра (кроме full)


class Moment(BaseModel):
    start: float = Field(description="Начало момента, секунды от начала видео")
    end: float = Field(description="Конец момента, секунды от начала видео")
    title: str = Field(description="Короткая цепляющая надпись-хук для экрана, до 6 слов")
    caption: str = Field(default="", description="Подпись к посту, 1-2 предложения")
    hashtags: list[str] = Field(default_factory=list)
    score: int = Field(default=50, description="Оценка виральности 0-100")
    reason: str = Field(default="", description="Почему момент интересен")
    profanity: bool = Field(default=False, description="Есть мат")
    category: str = Field(default="", description="Тип момента: смешное, мем, фейл, эмоция, конфликт, скилл, цитата, история")
    hooks: list[str] = Field(default_factory=list, description="2-3 альтернативные надписи-хука")
    risk: str = Field(default="", description="Если момент задевает темы, за которые могут не заплатить, — какие именно; иначе пусто")
    inserts: list[Insert] = Field(default_factory=list)
    chat_user: str = ""  # ник автора сообщения из чата (для раскладки «чат + реакция»)
    chat_text: str = ""
    image: str = ""  # своя картинка момента (материал «владелец/тип/имя») — низ кадра в раскладке «чат»
    image_idea: str = ""  # идея картинки под этот момент

    @property
    def duration(self) -> float:
        return self.end - self.start


class Preset(BaseModel):
    """Оформление ролика. Раскладки: split, center, square, reaction, chat, full."""

    name: str = "center"
    layout: str = "center"
    width: int = 1080
    height: int = 1920
    fps: int = 30
    # reaction: где в исходном кадре вебка (доли 0..1) и где её ставить
    webcam: tuple[float, float, float, float] = (0.0, 0.62, 0.28, 0.38)
    webcam_position: str = "top"
    webcam_share: float = 0.38
    # split: доля высоты под основной ролик, остальное — залипалка
    split_share: float = 0.5
    background_blur: int = 30
    content_zoom: float = 1.0
    crop_x: float = 0.5  # раскладка full: какую часть кадра брать по горизонтали (0 — левый край, 1 — правый)
    auto_face: bool = True  # умный кроп: вебка и положение кадра определяются по лицу автоматически
    cam_fullscreen: bool = False  # на самых громких местах вебка на пару секунд разворачивается на весь кадр
    # надпись-хук
    title: bool = True
    title_size: int = 64
    title_color: str = "FFFFFF"
    title_y: float = 0.12
    title_glow: str | None = None  # цвет неонового свечения, например A020F0
    title_anim: bool = False  # появление с увеличением
    teaser: float = 0.0  # секунд самого громкого места в самом начале ролика (0 — без тизера)
    # субтитры
    subtitles: bool = True
    subtitle_size: int = 72
    subtitle_color: str = "FFFFFF"
    subtitle_highlight: str = "FFE600"
    subtitle_y: float = 0.72
    subtitle_words: int = 3
    subtitle_upper: bool = False
    normalize: bool = True  # выравнивать громкость до стандарта площадок (-14 LUFS)
    censor: bool = False  # закрывать мат звёздочками в субтитрах
    subtitle_pop: bool = False  # каждое слово «выпрыгивает»
    watermark: str = ""  # ник мелким текстом поверх кадра
    watermark_y: float = 0.965
    font: str = "Arial"
    # уникализация: видимые изменения картинки и темпа
    mirror: bool = False
    speed: float = 1.0
    brightness: float = 0.0
    contrast: float = 1.0
    saturation: float = 1.0
    lut: str = ""  # цветокор: файл .cube из материалов («владелец/luts/имя») или путь
    lut_strength: float = 1.0
    transition: str = "none"  # подборки: none — встык, fade — мягкое затемнение между кусками
    # рекламная пауза: ролик замирает, поверх проигрывается баннер, потом ролик продолжается
    ad_break: str = ""  # видео или картинка баннера: «владелец/banners/имя» или путь
    ad_break_at: float = 0.5  # в какой доле ролика ставить (0.5 — посередине)
    ad_break_y: float = 0.6
    ad_break_scale: float = 0.94  # ширина баннера в долях кадра
    ad_break_len: float = 4.0  # сколько секунд показывать баннер-картинку
    border: int = 0
    border_color: str = "FFFFFF"
    chat_nick_color: str = "A970FF"
    chat_hide_nick: bool = False
    # движение камеры: none, push (медленный наезд), punch (резкие наезды на громких местах), both
    camera: str = "none"
    camera_strength: float = 0.12
    event_zoom: bool = False  # двойной наезд на запинках и повторах (если их нет — на самых громких местах)
    # баннер / водяной знак (png) поверх кадра
    banner: str | None = None
    banner_y: float = 0.9
    banner_width: float = 0.5

    @classmethod
    def load(cls, name_or_path: str, presets_dir: Path) -> "Preset":
        p = Path(name_or_path)
        if not p.exists():
            p = presets_dir / f"{name_or_path}.json"
        if not p.exists():
            raise FileNotFoundError(f"Пресет не найден: {name_or_path}")
        return cls.model_validate(json.loads(p.read_text(encoding="utf-8")))

    def save(self, presets_dir: Path) -> Path:
        path = presets_dir / f"{self.name}.json"
        path.write_text(json.dumps(self.model_dump(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return path
