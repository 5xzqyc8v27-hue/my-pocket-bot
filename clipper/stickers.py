"""Стикеры по смыслу слов: «доллары» — пачка денег, «автобус» — автобус, «Германия» — флаг.

Картинки лежат в clipper/data/stickers и входят в проект; слова сверяются по началу («долар» ловит
«долари», «доларів»). Работает без нейросети — по словарю для украинского и русского.
"""
from __future__ import annotations

from pathlib import Path

from .models import Insert, Word

FOLDER = Path(__file__).parent / "data" / "stickers"

# имя файла, эмодзи, начала слов
STICKERS: list[tuple[str, str, tuple[str, ...]]] = [
    ("dollars", "💵", ("долар", "доллар", "бакс")),
    ("moneybag", "💰", ("гроші", "грошей", "грошим", "деньг", "бабк", "бабл", "гривн", "зарплат", "тисяч", "тысяч", "мільйон", "миллион")),
    ("coin", "🪙", ("біткоін", "биткоин", "біткоїн", "крипт", "монет")),
    ("bus", "🚌", ("автобус", "маршрутк")),
    ("car", "🚗", ("машин", "автомобіл", "автомобил", "тачк", "беху", "бмв", "таксі", "такси")),
    ("plane", "✈️", ("літак", "самолет", "самолёт", "аеропорт", "аэропорт")),
    ("germany", "🇩🇪", ("німеччин", "герман")),
    ("ukraine", "🇺🇦", ("україн", "украин")),
    ("poland", "🇵🇱", ("польщ", "польш")),
    ("phone", "📱", ("телефон", "айфон", "мобіл", "мобил", "подзвон", "позвон", "дзвон", "звони", "набер")),
    ("camera", "🎥", ("камер",)),
    ("photo", "📸", ("фотк", "сфотк", "фото")),
    ("mic", "🎤", ("мікрофон", "микрофон", "макафон")),
    ("headphones", "🎧", ("навушник", "наушник")),
    ("laptop", "💻", ("комп'ютер", "компьютер", "ноутбук", "акаунт", "аккаунт")),
    ("gamepad", "🎮", ("грати", "граєш", "граю", "играть", "играешь", "катк")),
    ("gun", "🔫", ("стріля", "стреля", "автомат", "пістолет", "пистолет", "калаш")),
    ("bomb", "💣", ("бомб",)),
    ("house", "🏠", ("хату", "хата", "хаті", "додому", "дудому", "домой", "квартир")),
    ("door", "🚪", ("двер",)),
    ("bed", "🛏️", ("ліжк", "кроват", "спати", "спать")),
    ("pizza", "🍕", ("піц", "пицц")),
    ("beer", "🍺", ("пиво", "пива", "бухл", "бухи", "бухий", "бухой", "п'ян", "пьян")),
    ("cigarette", "🚬", ("сигар", "курит", "курю", "палит", "цигар")),
    ("coffee", "☕", ("каву", "кава", "кофе")),
    ("burger", "🍔", ("їсти", "їжа", "жрать", "поїсти", "поесть", "голодн")),
    ("police", "👮", ("поліц", "полиц", "мент", "мусор")),
    ("judge", "⚖️", ("суд ", "суду", "суді", "судит")),
    ("lock", "🔒", ("тюрм", "закри", "закрой", "замок")),
    ("punch", "👊", ("поб'ю", "побить", "побив", "побил", "вдар", "ударь", "ударю", "бійк", "драк", "в'єб", "въеб")),
    ("heart", "❤️", ("любл", "кохан", "кохаю", "зустрічат", "встречат")),
    ("broken_heart", "💔", ("бывш", "колишн", "розлуч", "расста")),
    ("baby", "👶", ("дитин", "маленьк", "ребенок", "ребёнок", "малий", "малый")),
    ("girl", "👩", ("дівчин", "дівчат", "девушк", "девочк", "дівк")),
    ("grandma", "👵", ("бабус", "бабушк", "бабця")),
    ("mom", "🤱", ("мама", "маму", "мамі", "мами", "маме", "мамо")),
    ("dog", "🐶", ("собак", "песик", "пёс")),
    ("cat", "🐱", ("котик", "кішк", "кошк", "коти ")),
    ("beaver", "🦫", ("бобер", "бобр", "бобёр")),
    ("monkey", "🐒", ("мавп", "обезьян")),
    ("fire", "🔥", ("вогонь", "огонь", "пожеж", "горит")),
    ("skull", "💀", ("помер", "вмер", "умер", "смерт", "вбив", "убив", "уб'ю", "убью", "знищ", "уничтож")),
    ("laugh", "😂", ("смішн", "смешн", "ржу", "сміюсь", "смеюсь", "сміят", "смеят")),
    ("scared", "😱", ("страшн", "боюс", "боїшс", "боишь", "жах")),
    ("thinking", "🤔", ("думаю", "думаєш", "думаешь", "подумай")),
    ("clown", "🤡", ("клоун", "дурн", "дурак", "дебіл", "дебил", "лох")),
    ("crown", "👑", ("корол", "цар", "топов")),
    ("trophy", "🏆", ("виграв", "виграл", "выиграл", "перемог", "победи")),
    ("cross", "❌", ("забан", "заборон", "запрещ", "запрет", "не можна", "нельзя")),
    ("check", "✅", ("чесно", "честно", "правду", "правда", "слово даю")),
    ("handshake", "🤝", ("договор", "домов", "дружи", "дружу", "подруж", "товариш")),
    ("gift", "🎁", ("подар",)),
    ("cake", "🎂", ("народженн", "рожден", "іменин", "именин")),
    ("scissors", "✂️", ("постриж", "підстриж", "стрижк", "пострич", "волос")),
    ("hat", "🎩", ("шляп", "капелюх", "шапк")),
    ("cart", "🛒", ("магазин", "купи", "купл")),
    ("hospital", "🏥", ("лікарн", "больниц", "лікар", "врач")),
    ("school", "🏫", ("школ", "однокласн", "одноклассн")),
    ("briefcase", "💼", ("робот", "работ")),
    ("sleep", "😴", ("сплю", "спиш", "спишь", "заснув", "уснул")),
    ("toilet", "🚽", ("туалет", "обісц", "обосц")),
    ("ball", "⚽", ("футбол", "м'яч", "мяч")),
    ("boxing", "🥊", ("бокс", "бій ", "бой ")),
    ("music", "🎵", ("пісн", "песн", "музик", "музык", "співа", "спой")),
    ("dance", "💃", ("танц", "танцуй")),
    ("live", "🔴", ("ефір", "эфир", "стрім", "стрим")),
    ("eyes", "👀", ("дивись", "подив", "смотри", "посмотр", "підгляд", "подгляд")),
    ("quiet", "🤫", ("тихо", "мовчи", "молчи", "секрет")),
    ("pray", "🙏", ("вибач", "извин", "прости", "пробач", "будь ласка", "пожалуйст")),
    ("wave", "👋", ("привіт", "привет", "бувай")),
    ("ghost", "👻", ("глюк", "привид", "призрак", "нікого нема", "никого нет")),
    ("speaker", "🔊", ("колонк", "гучн", "громк")),
    ("clock", "⏰", ("хвилин", "минут", "годину", "години", "секунд")),
]


SIGNS = {"germany": ("🇩🇪", "ГЕРМАНИЯ"), "ukraine": ("🇺🇦", "УКРАИНА"), "poland": ("🇵🇱", "ПОЛЬША")}


def _text_font(size: int):
    """Жирный шрифт с кириллицей для табличек: из проекта, иначе системный."""
    from PIL import ImageFont
    root = Path(__file__).parent.parent
    for path in (root / "samples/assets/common/fonts/Montserrat_Black.ttf",
                 Path("/System/Library/Fonts/Supplemental/Arial Bold.ttf"),
                 Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")):
        if path.exists():
            return ImageFont.truetype(str(path), size)
    return ImageFont.load_default()


def draw_sign(emoji_image, text: str):
    """Дорожный указатель: синяя табличка с белой рамкой, флаг, название и стрелка."""
    from PIL import Image, ImageDraw
    font = _text_font(64)
    probe = ImageDraw.Draw(Image.new("RGBA", (10, 10)))
    tw = int(probe.textlength(text, font=font))
    w, h = 150 + tw + 110, 150
    im = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle((0, 0, w - 1, h - 1), 26, fill=(255, 255, 255, 255))
    d.rounded_rectangle((8, 8, w - 9, h - 9), 20, fill=(24, 84, 196, 255))
    flag = emoji_image.resize((96, int(96 * emoji_image.height / emoji_image.width)), Image.LANCZOS)
    im.alpha_composite(flag, (28, (h - flag.height) // 2))
    d.text((146, h // 2), text, font=font, fill=(255, 255, 255, 255), anchor="lm")
    x = 146 + tw + 26  # стрелка «прямо»
    d.polygon([(x + 30, 34), (x + 62, 76), (x + 42, 76), (x + 42, 116), (x + 18, 116), (x + 18, 76), (x - 2, 76)],
              fill=(255, 255, 255, 255))
    return im


def build(font_path: str = "/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf") -> int:
    """Рисует картинки стикеров из цветного шрифта эмодзи. Нужно один раз — файлы лежат в проекте."""
    from PIL import Image, ImageDraw, ImageFont
    font = ImageFont.truetype(font_path, 109)
    FOLDER.mkdir(parents=True, exist_ok=True)

    def glyph(emoji: str):
        im = Image.new("RGBA", (300, 150), (0, 0, 0, 0))
        ImageDraw.Draw(im).text((4, 4), emoji, font=font, embedded_color=True)
        return im.crop(im.getbbox()) if im.getbbox() else None

    made = 0
    for name, emoji, _ in STICKERS:
        im = glyph(emoji)
        if im is None:
            continue
        if name in SIGNS:  # страны — табличкой-указателем, а не голым флагом
            draw_sign(glyph(SIGNS[name][0]), SIGNS[name][1]).save(FOLDER / f"{name}.png")
        else:
            scale = 360 / max(im.size)
            im.resize((int(im.width * scale), int(im.height * scale)), Image.LANCZOS).save(FOLDER / f"{name}.png")
        made += 1
    return made


# названия анимированных эмодзи из паков пользователя (русские и английские), которые подходят к стикеру
HINTS: dict[str, tuple[str, ...]] = {'dollars': ('money-mouth', 'деньги'), 'moneybag': ('money-mouth', 'бриллиант'), 'coin': ('бриллиант',), 'laptop': ('сидит за компом',), 'beer': ('woozy', 'потный'), 'burger': ('savoring food', 'ножик вилка'), 'punch': ('гнев', 'symbols on mouth', 'злой'), 'heart': ('сердце', 'red heart', 'много сердечек', 'two hearts'), 'broken_heart': ('broken heart', 'без настроения'), 'fire': ('огонь', 'fire'), 'skull': ('призрак страшно', 'screaming in fear'), 'laugh': ('смех до слез', 'tears of joy', 'rolling on the floor', 'смех'), 'scared': ('призрак страшно', 'screaming in fear', 'fearful'), 'thinking': ('думает', 'thinking face', 'мысли'), 'clown': ('клоун', 'clown'), 'crown': ('чел с короной',), 'trophy': ('празднует', 'partying', 'хлопает'), 'cross': ('gesturing no', 'ноу', 'нет', 'дизлайк', 'thumbs down'), 'check': ('инфа сотка', 'окей', 'ok hand', 'thumbs up'), 'handshake': ('жмут руки',), 'cake': ('празднует', 'partying'), 'cart': ('с покупочками',), 'hospital': ('заболевший', 'thermometer', 'medical mask'), 'sleep': ('зевнул', 'sleeping face'), 'toilet': ('закрывает нос', 'какашка', 'nauseated'), 'boxing': ('бицуха', 'flexed biceps'), 'music': ('ноты',), 'eyes': ('eyes', 'что это'), 'quiet': ('shushing', 'zipper-mouth'), 'pray': ('folded hands', 'щенячьи глазки', 'pleading'), 'wave': ('привет', 'waving hand'), 'ghost': ('призрак',), 'girl': ('воздушный поцелуй', 'blowing a kiss')}

# чувства и реакции: своей картинки нет, берутся только из анимированных эмодзи в материалах
REACTIONS: list[tuple[tuple[str, ...], tuple[str, ...]]] = [
    (("шок", "офіг", "офиг", "нічого собі", "ничего себе", "ніхуя", "нихуя", "піздец", "пиздец"),
     ("взрыв мозга", "exploding head", "вау", "astonished")),
    (("красавчик", "молодец", "молодець", "красава", "браво"), ("хлопает", "clapping hands", "харизматичный", "sunglasses")),
    (("плач", "сльоз", "слез", "реве", "плакат"), ("вытирает нос и падает слеза", "loudly crying", "crying face")),
    (("злий", "злой", "бісит", "бесит", "заїба", "заеба"), ("гнев", "злой", "symbols on mouth", "angry face")),
    (("чого", "чего", "як це", "как это", "не поняв", "не понял"), ("чего", "чивооо", "есть вопрос", "raised eyebrow", "man shrugging")),
    (("сумн", "грустн", "обіжа", "обижа", "образ"), ("без настроения", "pensive", "disappointed")),
    (("жарко", "спека", "спітн", "вспоте"), ("потный", "hot face")),
    (("холодн", "змерз", "замерз"), ("холоднный", "cold face")),
    (("цілу", "целу", "поцілу", "поцелу", "чмок"), ("чмоки чмоки", "воздушный поцелуй", "blowing a kiss")),
    (("хворі", "заболі", "заболе"), ("заболевший", "thermometer")),
    (("диявол", "дьявол", "чорт", "черт"), ("дьявол", "smiling face with horns")),
    (("лайно", "говно", "гівно", "какаш"), ("какашка",)),
    (("ура", "свято", "праздн", "вітаю", "поздрав"), ("празднует", "partying")),
    (("точно", "сто відсотк", "сто процент", "інфа сотка", "инфа сотка", "гарантую"), ("инфа сотка", "ok hand")),
    (("добре", "хорошо", "окей", "ладно", "згоден", "согласен"), ("окей", "ok hand", "thumbs up")),
]
MOVING = (".mp4", ".webm", ".mov", ".gif")


def asset_index(dirs: list[Path] | None) -> list[tuple[str, Path]]:
    """Анимированные эмодзи из материалов: (название строчными, файл), короткие названия первыми."""
    out = []
    for folder in dirs or []:
        if folder.is_dir():
            for f in folder.iterdir():
                if f.suffix.lower() in MOVING:
                    out.append((f.stem.lower().replace("_", " ").replace("-", " "), f))
    return sorted(out, key=lambda row: len(row[0]))


def from_assets(hints: tuple[str, ...], index: list[tuple[str, Path]]) -> Path | None:
    for hint in hints:
        key = hint.replace("-", " ")
        for name, path in index:
            if key in name:
                return path
    return None


def find(words: list[Word], clip_start: float, speed: float = 1.0, limit: int = 6, gap: float = 2.6,
         length: float = 0.0, dirs: list[Path] | None = None) -> list[Insert]:
    """Вставки-стикеры к словам отрезка: не чаще раза в gap секунд, каждый стикер один раз.

    Сначала ищется анимированный эмодзи из материалов (dirs) с подходящим названием,
    и только если такого нет — встроенная картинка."""
    index = asset_index(dirs)

    def clean(text: str) -> str:
        return text.lower().strip('.,!…:;"«»-–— ')

    out, used, last, side = [], set(), -gap, 0
    for i, w in enumerate(words):
        text = clean(w.text)
        pair = (text + " " + clean(words[i + 1].text)) if i + 1 < len(words) else text
        at = (w.start - clip_start) / speed
        if at - last < gap or (length and at > length - 1.8):
            continue

        def hit(stems: tuple[str, ...]) -> bool:
            return any((text + " ").startswith(s) or pair.startswith(s) for s in stems)

        chosen = None
        for name, _, stems in STICKERS:
            if name not in used and hit(stems):
                file = from_assets(HINTS.get(name, ()), index)
                if file and str(file) in used:  # этот эмодзи из пака уже показывали — берём свою картинку
                    file = None
                builtin = FOLDER / f"{name}.png"
                if file or builtin.exists():
                    chosen = (name, file or builtin)
                    break
        if not chosen and index:
            for stems, hints in REACTIONS:
                file = from_assets(hints, index)
                if file and str(file) not in used and hit(stems):
                    chosen = (str(file), file)
                    break
        if chosen:
            moving = chosen[1].suffix.lower() in MOVING
            out.append(Insert(file=str(chosen[1]), at=round(max(0.0, at - 0.1), 2), duration=2.0 if moving else 1.7,
                              place="right" if side % 2 == 0 else "left", scale=0.26 if moving else 0.24))
            used.update((chosen[0], str(chosen[1])))
            last, side = at, side + 1
        if len(out) >= limit:
            break
    return out


if __name__ == "__main__":
    print("стикеров:", build())
