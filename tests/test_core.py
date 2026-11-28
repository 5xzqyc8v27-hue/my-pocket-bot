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


def test_reach_verdict_and_ip_parse():
    from clipper.health import parse_ip, reach_verdict
    now = 1_800_000_000.0
    def posts(views):
        return [{"views": v, "ts": now - 86400 * (i + 1)} for i, v in enumerate(views)]
    assert reach_verdict(posts([5000, 7000, 4000, 6000, 5500, 8000, 3000]), now)["level"] == "ok"
    drop = reach_verdict(posts([200, 150, 300, 6000, 5500, 8000, 3000]), now)
    assert drop["level"] == "warn" and "упали" in drop["verdict"]
    assert reach_verdict(posts([3, 0, 8, 6000, 5500, 8000, 3000]), now)["level"] == "bad"
    assert reach_verdict(posts([100, 200]), now)["level"] == "unknown"
    ip = parse_ip({"status": "success", "query": "1.2.3.4", "country": "X", "isp": "Host Ltd", "hosting": True})
    assert ip["level"] == "bad" and "дата-центра" in ip["verdict"]
    assert parse_ip({"status": "success", "query": "5.6.7.8", "isp": "Home ISP", "mobile": False})["level"] == "ok"


def test_webcam_rect_from_face_snaps_to_edges():
    from clipper.face import apply_face, webcam_rect
    face = {"cx": 0.153, "cy": 0.833, "w": 0.095, "h": 0.169, "stable": 0.8}
    x, y, w, h = webcam_rect(face)
    assert x == 0 and abs(y + h - 1) < 1e-6 and 0.25 < w < 0.4 and 0.35 < h < 0.5
    assert apply_face(Preset(layout="reaction"), face).webcam == (x, y, w, h)
    assert apply_face(Preset(layout="reaction", auto_face=False), face).webcam == Preset().webcam
    assert apply_face(Preset(layout="center"), face).webcam == Preset().webcam


def test_blotato_requests_follow_the_official_package(tmp_path):
    from clipper import blotato
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"\x00" * 1000)
    calls = []

    def send(method, path, body=None, query=""):
        calls.append((method, path, body, query))
        if path == "/v2/media":
            return {"url": "https://cdn.example/clip.mp4"}
        if path == "/v2/posts":
            return {"postSubmissionId": "sub-1"}
        if path == "/v2/users/me/accounts":
            return {"items": [{"id": 7, "username": "clips"}]}
        return {"status": "published", "publicUrl": "https://tiktok.example/v/1"}

    assert blotato.list_accounts("tiktok", send) == [{"id": "7", "name": "clips"}] and calls[0][3] == "platform=tiktok"
    assert blotato.publish(video, "tiktok", "7", "подпись #тег", "Заголовок", send) == "sub-1"
    media, post = calls[1], calls[2]
    assert media[2]["url"].startswith("data:video/mp4;base64,")
    body = post[2]["post"]
    assert body["accountId"] == "7" and body["content"] == {"platform": "tiktok", "text": "подпись #тег",
                                                            "mediaUrls": ["https://cdn.example/clip.mp4"]}
    assert body["target"]["targetType"] == "tiktok" and body["target"]["privacyLevel"] == "PUBLIC_TO_EVERYONE"
    assert blotato.target("youtube", "T" * 150)["title"] == "T" * 100
    assert blotato.target("instagram", "")["mediaType"] == "reel"
    assert blotato.status("sub-1", send)["state"] == "published"


def test_find_stutters_catches_repeats_and_slips():
    from clipper.events import find_stutters
    def ws(text):
        return [Word(start=i * 0.5, end=i * 0.5 + 0.4, text=t) for i, t in enumerate(text.split())]
    kinds = lambda text: [(f["kind"], f["text"]) for f in find_stutters(ws(text))]
    assert kinds("своя рабоработория лаборатория с грязнульками")[0] == ("оговорка", "рабоработория")
    assert ("повтор слова", "я") not in kinds("а я пошёл домой")
    assert kinds("ну это это вообще что такое")[0][0] == "повтор слова"
    rep = kinds("сейчас ребята секунду мне будто плохо стало сейчас ребята секунду")
    assert any(k == "повтор фразы" for k, _ in rep)
    assert kinds("обычная фраза без всяких запинок сегодня") == []


def test_find_stutters_catches_self_correction():
    from clipper.events import find_stutters
    words = [Word(start=i * 0.6, end=i * 0.6 + 0.5, text=t) for i, t in
             enumerate("своя рабб раборатория грязнуль лаборатория грязнули да".split())]
    kinds = [f["kind"] for f in find_stutters(words, min_gap=0.5)]
    assert "запинка" in kinds and "оговорка" in kinds


def test_contest_rules_link_and_payout_advice():
    from clipper.rules import ZLOY, parse_tiers, payout, with_link
    assert with_link("Смешной момент #злой", "twitch: ZLOYn") == "Смешной момент #злой twitch: ZLOYn"
    assert with_link("уже есть Twitch: zloyn", "twitch: ZLOYn") == "уже есть Twitch: zloyn"
    tiers, now, day = ZLOY["rule_tiers"], 1_800_000_000.0, 86400
    assert parse_tiers(tiers)[0] == (100000, 25.0, 7.5)
    fast = payout(150_000, now - 2 * day, tiers, 14, now=now)       # 75 тыс/день -> дойдёт до 500 тыс
    assert fast["amount"] == 25 and "подождать" in fast["advice"]
    slow = payout(120_000, now - 10 * day, tiers, 14, now=now)      # 12 тыс/день -> не дойдёт
    assert "подавайте" in slow["advice"]
    assert payout(120_000, now - 10 * day, tiers, 14, meme=True, now=now)["amount"] == 7.5
    assert payout(120_000, now - 13 * day, tiers, 14, now=now)["level"] == "warn"
    assert payout(50_000, now - 15 * day, tiers, 14, now=now)["level"] == "bad"
    assert payout(1_200_000, now - 3 * day, tiers, 14, now=now)["amount"] == 225


def test_pack_classify_and_names():
    from clipper.packs import classify, folder_id, target_name
    assert folder_id("https://drive.google.com/drive/folders/1D6-FTQPwDC4?usp=sharing") == "1D6-FTQPwDC4"
    assert classify("Мемы/[ATHENS] Cкала удивление.mp4") == "memes"
    assert classify("Звуки/Вуши/[ATHENS] Базовый вуш 1.mp3") == "sounds"
    assert classify("Музыка без АП/Фонк/track.mp3") == "music"
    assert classify("Фоны для монтажа/[ATHENS] фон 2.mp4") == "fillers"
    assert classify("Шрифты/Bartina Bold.ttf") == "fonts"
    assert classify("3D Модели/Техника/microphone.glb") is None
    assert classify("PNG's/Вырезаные буквы/01.png") is None
    assert classify("Мемы/._скрытый.mp4") is None
    assert target_name("Звуки/Мемные/[ATHENS] сверчки мем.mp3") == "Мемные_сверчки_мем.mp3"
    assert target_name("Мемы/[ATHENS] Cкала удивление.mp4") == "Cкала_удивление.mp4"


def test_pack_classify_yandex_folders():
    from clipper import packs
    assert packs.is_yandex("https://disk.yandex.ru/d/abc") and not packs.is_yandex("https://drive.google.com/drive/folders/x")
    assert packs.classify("Эмодзи/100_Crying Cat Face.mov") == "emoji"
    assert packs.classify("Эмодзи/100_Crying Cat Face.gif") is None
    assert packs.classify("Эмодзи/AnimatedEmojies_AgAD.mp4") is None
    assert packs.classify("Луты/Action Looks (1).cube") == "luts"
    assert packs.classify("Эффекты/Глитч/1.mp4") == "effects"
    assert packs.classify("Необходимый софт/After Effects.rar") is None
    assert packs.classify("Инструменты/shake.ffx") is None


def test_lut_and_effect_filters():
    from clipper.models import Preset
    from clipper.render import build_filter, _Ins
    p = Preset(name="t")
    assert "lut3d=look.cube" in build_filter(p, False, False, None, lut_name="look.cube")
    assert "lut3d" not in build_filter(p, False, False, None)
    ins = _Ins("/x/common/effects/glitch.mp4", 1.0, 1.5, "full", 1.0)
    assert ins.effect and "blend=all_mode=screen" in build_filter(p, False, False, None, inserts=[ins], first_insert=1)


def test_calm_preset_and_fade_join(tmp_path):
    from pathlib import Path
    from clipper.models import Preset
    from clipper import compile as comp
    p = Preset.load("calm", Path(__file__).parent.parent / "presets")
    assert p.transition == "fade" and p.camera == "push" and not p.subtitle_pop
    seen = {}
    comp.run, old = (lambda cmd, **k: seen.update(cmd=cmd)), comp.run
    try:
        comp.join_faded([tmp_path / "a.mp4", tmp_path / "b.mp4", tmp_path / "c.mp4"], [10, 8, 6], tmp_path / "o.mp4")
    finally:
        comp.run = old
    graph = seen["cmd"][seen["cmd"].index("-filter_complex") + 1]
    assert graph.count("xfade") == 2 and "offset=9.550" in graph and "offset=17.100" in graph


def test_upg_offer_rates_and_ad_break_fields():
    from clipper import rules
    from clipper.models import Preset
    low = rules.payout_rate(5000, rules.UPG_L1["rule_rates"], banner=False)
    assert low["amount"] == 0 and low["next"] == 6000
    assert rules.payout_rate(100000, rules.UPG_L1["rule_rates"], banner=False)["amount"] == 70.0
    assert rules.payout_rate(100000, rules.UPG_L2["rule_rates"], banner=True)["amount"] == 30.0
    assert rules.payout_rate(9999, rules.UPG_L2["rule_rates"], banner=True)["amount"] == 0
    assert rules.ad_len_note(30, "15-60") == "" and rules.ad_len_note(10, "15-60") and rules.ad_len_note(61, "15-60")
    assert Preset(name="t").ad_break == "" and Preset(name="t").ad_break_at == 0.5


def test_looks_and_copies():
    from clipper.looks import LOOKS, look_filter, copy_params, copy_filter, clean_filters
    from clipper.models import Preset
    from clipper.render import build_filter
    assert all(look_filter(n, 0.5) for n in LOOKS) and look_filter("", 1) == "" and look_filter("нет", 1) == ""
    a, b = copy_params(1), copy_params(2)
    assert a != b and 1.0 < a["zoom"] <= 1.05 and 0.98 <= a["speed"] <= 1.03
    assert "setpts=PTS/" in copy_filter(a) and clean_filters(0, 0) == [] and len(clean_filters(0.5, 0.5)) == 2
    graph = build_filter(Preset(name="t", look="cold", sharpen=0.4), False, False, None)
    assert "colorbalance" in graph and "unsharp" in graph


def test_unloop_and_game_area():
    from clipper.transcribe import unloop
    from clipper.render import _area, build_filter
    from clipper.models import Preset
    assert unloop("Ні, ні, ні, ні, ні, ні, ні, ні, кінець").count("ні") <= 4
    assert unloop("обычная фраза без повторов") == "обычная фраза без повторов"
    assert _area("0.42,0,0.58,0.57") == (0.42, 0.0, 0.58, 0.57) and _area("") is None and _area("0.9,0,0.5,0.5") is None
    g = build_filter(Preset(name="t", layout="reaction", game_area="0.42,0,0.58,0.57"), False, False, None)
    assert "crop=iw*0.58:ih*0.57:iw*0.42:ih*0.0" in g


def test_kick_helpers():
    from clipper import kick
    assert kick.slug_of("https://kick.com/Kalatay3") == "kalatay3" and kick.slug_of("@kalatay3") == "kalatay3"
    assert kick.is_clip_url("https://kick.com/kalatay3/clips/clip_01M0AF2BHRKA4SPKHCTGVWR818")
    assert not kick.is_clip_url("https://kick.com/kalatay3/videos/abc")
    assert kick.PERIODS["7d"] == "week" and kick._ts("2026-08-18") > 0


def test_uniq_command_and_collect(tmp_path):
    import random
    from clipper import looks
    cfg = dict(looks.UNIQ_DEFAULTS)
    p = looks.uniq_params(cfg, random.Random(3))
    assert 1.0 <= p["zoom"] <= 1.04 and 0.98 <= p["speed"] <= 1.02
    cmd = looks.uniq_command(tmp_path / "a.mp4", tmp_path / "o.mp4", cfg, p, True)
    assert "-map_metadata" in cmd and "-vf" in cmd and any(x.startswith("atempo=") for x in cmd)
    bg = tmp_path / "bg.jpg"; bg.write_bytes(b"x")
    cmd = looks.uniq_command(tmp_path / "a.mp4", tmp_path / "o.mp4", {**cfg, "background": str(bg), "size": "vertical"}, p, False)
    assert "-filter_complex" in cmd and "-an" in cmd and "overlay" in cmd[cmd.index("-filter_complex") + 1]
    (tmp_path / "v1.mp4").write_bytes(b"x"); (tmp_path / "n.txt").write_bytes(b"x")
    assert [f.name for f in looks.collect_videos([str(tmp_path)])] == ["v1.mp4"]


def test_zip_pack_and_bottom_plate(tmp_path):
    import zipfile
    from clipper import packs
    from clipper.models import Preset
    from clipper.render import build_filter
    arc = tmp_path / "p.zip"
    with zipfile.ZipFile(arc, "w") as z:
        z.writestr("font.ttf", b"x"); z.writestr("boom.mp3", b"x"); z.writestr("CapCut.apk", b"x"); z.writestr("a/b.cube", b"x")
    assert packs.classify("Montage Pack.zip") == "zip"
    assert packs.unpack_zip(arc, tmp_path / "assets", "common", ["fonts", "sounds"]) == 2
    got = sorted(f.name for f in (tmp_path / "assets").rglob("*") if f.is_file())
    assert got == ["boom.mp3", "font.ttf"]
    g = build_filter(Preset(name="t", layout="reaction", banner_mode="bottom"), False, True, None, plate_h=340)
    assert "vstack=inputs=3" in g and "overlay=(W-w)/2:H*" not in g


def test_kick_live_uses_site_api(monkeypatch):
    from clipper import kick, streams
    monkeypatch.setattr(kick, "_json", lambda path: {"livestream": {"is_live": True, "session_title": "t", "viewer_count": 5},
                                                     "playback_url": "https://x/y.m3u8"})
    assert streams.is_live("kick", "https://kick.com/Kalatay3") == (True, "")
    assert kick.live_info("kalatay3")["url"].endswith(".m3u8")
    monkeypatch.setattr(kick, "_json", lambda path: {"livestream": None, "playback_url": ""})
    assert streams.is_live("kick", "kalatay3") == (False, "")


def test_hook_highlight_and_top_pick():
    from clipper.subtitles import title_lines
    from clipper.server import pick_top_clips
    from clipper.top import list_top
    rows = title_lines("Нейбор *розбив монітор* на стрімі", 16, "FFFFFF", "FFD400")
    assert len(rows) == 3 and rows[0].count("&H0000D4FF&") == 1 and "&H0000D4FF&" not in rows[2]
    assert title_lines("*одне*", 20, "FFFFFF", "FF0000")[0].endswith("ОДНЕ")
    day = 86400
    entries = [{"url": "a", "views": 50, "ts": day * 10, "duration": 20}, {"url": "b", "views": 40, "ts": day * 10 + 5, "duration": 20},
               {"url": "c", "views": 30, "ts": day * 11, "duration": 300}, {"url": "d", "views": 20, "ts": day * 12, "duration": 15}]
    assert [e["url"] for e in pick_top_clips(entries, 3)] == ["a", "d"]
    assert list_top("ТОП 3", 1080, 1920) < list_top("ТОП 4 МОМЕНТИ КАЛАТАЯ НАЙКРАЩЕ В КІНЦІ", 1080, 1920)


def test_dialog_format_pieces():
    from pathlib import Path
    from clipper.models import Preset, Word
    from clipper.render import build_filter
    from clipper.speaker import crop_graph, timeline
    from clipper.subtitles import build_ass, title_lines, word_groups
    import numpy as np
    words = [Word(text=t, start=i * 0.4, end=i * 0.4 + 0.3) for i, t in enumerate(["Я", "зайшов.", "-", "І", "ви", "подивіться", "що", "тут"])]
    groups = [[w.text for w in g] for g in word_groups(words, 3)]
    assert groups == [["Я", "зайшов."], ["І", "ви", "подивіться"], ["що", "тут"]]
    p = Preset.load("dialog", Path(__file__).parent.parent / "presets")
    ass = build_ass(words, 0, 6, "*Вамос* рассказал про *детство*", p)
    assert "зайшов" in ass and "ЗАЙШОВ" not in ass and "зайшов." not in ass
    assert ass.count("Dialogue: 2,") >= 4 and "0:00:03.00" in ass  # хук печатается и уходит через 3 секунды
    assert "\\alpha&HFF&" in title_lines("а б в", 20, "FFFFFF", "FF0000", False, 1)[0]
    plan = {"cuts": [(0, 2, 0.1, 0.0, 0.3, 0.9), (2, 5, 0.6, 0.1, 0.25, 0.8)], "people": 2}
    graph = ";".join(crop_graph(plan, "src", "v", 1080, 1920))
    assert "split=2" in graph and "concat=n=2" in graph and "trim=start=2.000" in graph
    assert "concat=n=2" in build_filter(p, False, False, None, speaker_plan=plan)
    assert "crop=1080:1920" in build_filter(p, False, False, None)  # людей не нашли — кадр по центру
    act = np.zeros((2, 50)); act[0, :25] = 2; act[1, 25:] = 2
    assert [who for _, _, who in timeline(act)] == [0, 1]


def test_zip_draft(tmp_path):
    import zipfile
    from clipper.capcut import zip_draft
    d = tmp_path / "Проект"; d.mkdir(); (d / "draft_content.json").write_text("{}"); (d / "video.mp4").write_bytes(b"x")
    z = zip_draft(d, tmp_path / "p.zip")
    assert sorted(zipfile.ZipFile(z).namelist()) == ["Проект/draft_content.json", "Проект/video.mp4"]


def test_noise_phrases():
    from clipper.transcribe import is_noise
    assert all(is_noise(t) for t in ("[музика]", "*сміх*", "Дякую.", "Дякую за перегляд!", "...", "(оплески)"))
    assert not any(is_noise(t) for t in ("Дякую тобі, брате", "Вибачайся.", "Я камеру відірвав!"))
