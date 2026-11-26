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
