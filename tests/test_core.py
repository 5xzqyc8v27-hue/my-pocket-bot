from clipper.models import Moment, Preset, Segment, Transcript, Word
from clipper.moments import _clean
from clipper.render import build_filter
from clipper.subtitles import ass_color, ass_time, build_ass


def test_ass_helpers():
    assert ass_color("FFE600") == "&H0000E6FF"
    assert ass_time(61.5) == "0:01:01.50"


def test_build_ass_highlights_each_word():
    words = [Word(start=10.0, end=10.4, text="раз"), Word(start=10.4, end=11.0, text="два")]
    ass = build_ass(words, 10.0, 5.0, "Хук", Preset())
    assert ass.count("Dialogue: 0") == 2 and "ХУК" in ass


def test_words_spread_when_no_word_timings():
    tr = Transcript(segments=[Segment(start=0, end=4, text="один два три четыре")])
    words = tr.words_between(0, 4)
    assert [w.text for w in words] == ["один", "два", "три", "четыре"]
    assert abs(words[-1].end - 4) < 1e-6


def test_split_falls_back_without_filler():
    preset = Preset(layout="split")
    assert "vstack" in build_filter(preset, True, False, None)
    assert "vstack" not in build_filter(preset, False, False, None)


def test_clean_drops_overlaps_and_bad_lengths():
    moments = [Moment(start=0, end=30, title="a", score=90), Moment(start=20, end=50, title="b", score=80),
               Moment(start=60, end=62, title="c", score=99), Moment(start=70, end=100, title="d", score=70)]
    assert [m.title for m in _clean(moments, 200, 20, 60, 5)] == ["a", "d"]


def test_parse_json_reply_tolerates_wrapping_text():
    from clipper.moments import _parse_json_reply
    text = 'Вот моменты:\n```json\n{"moments": [{"start": 1, "end": 31, "title": "x"}]}\n```'
    assert _parse_json_reply(text)[0].end == 31


def test_trends_ranking_uses_growth_and_window():
    from clipper.trends import merge, parse_entries, ranking
    now = 1_800_000_000.0
    data = {"channel": "Chan", "entries": [
        {"id": "a", "title": "new hit", "view_count": 50000, "timestamp": now - 3600 * 5},
        {"id": "b", "title": "old", "view_count": 900000, "timestamp": now - 86400 * 20},
        {"id": "c", "title": "small", "view_count": 100, "timestamp": now - 3600}]}
    videos: dict = {}
    merge(videos, parse_entries(data), "chan", now - 7200)
    data["entries"][1]["view_count"] = 990000
    merge(videos, parse_entries(data), "chan", now)
    day = ranking(videos, "24h", now)
    assert day[0]["id"] == "b" and day[0]["growth"] == 90000  # старый ролик, но резко растёт
    assert {r["id"] for r in day} == {"a", "b", "c"}
    assert [r["id"] for r in ranking(videos, "30d", now)][0] == "b"


def test_chat_parse_and_read_detection():
    from clipper.chat import find_read_messages, parse_line
    assert parse_line(":vasya!vasya@vasya.tmi.twitch.tv PRIVMSG #zloy :Как думаешь насчет лаборатории?") == (
        "vasya", "Как думаешь насчет лаборатории?")
    assert parse_line("PING :tmi.twitch.tv") is None
    tr = Transcript(duration=120, segments=[
        Segment(start=10, end=14, text="сегодня играем в гта"),
        Segment(start=40, end=45, text="как думаешь насчёт своей лаборатории с грязнульками"),
        Segment(start=45, end=49, text="да ты тупишь бро я гений")])
    chat = [{"t": 5, "user": "a", "text": "привет"},
            {"t": 31, "user": "a774kl", "text": "Как думаешь насчет своей лаборатории с грязнульками?"},
            {"t": 60, "user": "b", "text": "совсем другое длинное сообщение про погоду"}]
    found = find_read_messages(tr, chat)
    assert len(found) == 1 and found[0].chat_user == "a774kl"
    assert 39 <= found[0].start <= 40 and found[0].end >= 49


def test_chat_spikes_and_rechat_parse():
    from clipper.chat import chat_spikes
    from clipper.history import parse_rechat
    chat = [{"t": float(t), "user": "u", "text": "ок"} for t in range(0, 600, 10)]
    chat += [{"t": 300 + i * 0.2, "user": "u", "text": "АХАХАХ что это было"} for i in range(40)]
    spikes = chat_spikes(chat, 600)
    assert len(spikes) == 1 and spikes[0].start == 280 and spikes[0].category == "всплеск чата"
    raw = {"comments": [{"content_offset_seconds": 12.5, "commenter": {"display_name": "Vasya"},
                         "message": {"body": "привет"}}, {"broken": 1}]}
    assert parse_rechat(raw) == [{"t": 12.5, "user": "Vasya", "text": "привет"}]


def test_schedule_slots_spread_and_skip_taken():
    import time
    from clipper.schedule import next_slots
    now = time.mktime((2026, 11, 27, 8, 0, 0, 0, 0, -1))
    slots = next_slots([], 3, per_day=2, from_hour=10, to_hour=22, now=now)
    hours = [time.localtime(s).tm_hour for s in slots]
    assert hours == [13, 19, 13] and time.localtime(slots[2]).tm_mday == 28
    assert next_slots([slots[0]], 1, 2, 10, 22, now)[0] == slots[1]


def test_censor_keeps_punctuation_and_clean_words():
    from clipper.subtitles import censor_word
    assert censor_word("блять,") == "б****,"
    assert censor_word("привет") == "привет"
    assert censor_word("Нахуй!") == "Н****!"
