from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

from .media import fetch, probe
from .models import Moment, Preset, Transcript
from .moments import pick_heuristic, pick_with_claude
from .render import render_clip

ROOT = Path(__file__).resolve().parent.parent
PRESETS = ROOT / "presets"


def _slug(text: str, fallback: str) -> str:
    slug = re.sub(r"[^\w]+", "_", text.lower(), flags=re.UNICODE).strip("_")[:40]
    return slug or fallback


def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("source", help="Файл или ссылка (YouTube, Twitch VOD и т.д.)")
    p.add_argument("--preset", default="center", help="Имя из presets/ или путь к json")
    p.add_argument("--filler", help="Видео-залипалка для раскладки split")
    p.add_argument("--out", default="out", help="Папка для готовых роликов")
    p.add_argument("--whisper", default="small", help="Модель faster-whisper: small, medium, large-v3")
    p.add_argument("--lang", default="ru")
    p.add_argument("--transcript", help="Готовая расшифровка json (пропустить распознавание)")
    p.add_argument("--no-subs", action="store_true", help="Без субтитров речи")


def _transcript(args, video: Path) -> Transcript | None:
    if args.transcript:
        return Transcript.load(Path(args.transcript))
    if args.no_subs and getattr(args, "picker", "heuristic") == "heuristic":
        return None
    try:
        from .transcribe import transcribe
        print("Расшифровываю речь…", flush=True)
        return transcribe(video, args.whisper, args.lang)
    except ImportError:
        print("faster-whisper не установлен — работаю без расшифровки (pip install faster-whisper).",
              file=sys.stderr)
        return None


def cmd_run(args) -> int:
    out_dir = Path(args.out)
    video = fetch(args.source, out_dir / "_downloads")
    info = probe(video)
    preset = Preset.load(args.preset, PRESETS)
    if args.no_subs:
        preset.subtitles = False
    tr = _transcript(args, video)
    if tr and not tr.duration:
        tr.duration = info["duration"]

    picker = args.picker
    if picker == "auto":
        picker = "claude" if tr and tr.segments and os.environ.get("ANTHROPIC_API_KEY") else "heuristic"
    print(f"Ищу моменты ({picker})…", flush=True)
    if picker == "claude":
        if not tr or not tr.segments:
            print("Для выбора через Claude нужна расшифровка.", file=sys.stderr)
            return 2
        moments = pick_with_claude(tr, args.count, args.min, args.max)
    else:
        moments = pick_heuristic(video, tr, args.count, args.min, args.max)
    if not moments:
        print("Подходящих моментов не нашлось.", file=sys.stderr)
        return 1

    clip_dir = out_dir / video.stem
    clip_dir.mkdir(parents=True, exist_ok=True)
    report = []
    for i, m in enumerate(moments, 1):
        name = f"{i:02d}_{_slug(m.title, 'clip')}.mp4"
        print(f"[{i}/{len(moments)}] {m.start:.0f}–{m.end:.0f} с · ★{m.score} · {m.title or 'без названия'}",
              flush=True)
        render_clip(video, m, preset, clip_dir / name, tr, Path(args.filler) if args.filler else None)
        report.append({**m.model_dump(), "file": name, "preset": preset.name})
    (clip_dir / "moments.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Готово: {len(report)} роликов в {clip_dir}")
    return 0


def cmd_clip(args) -> int:
    out_dir = Path(args.out)
    video = fetch(args.source, out_dir / "_downloads")
    preset = Preset.load(args.preset, PRESETS)
    if args.no_subs:
        preset.subtitles = False
    tr = _transcript(args, video) if preset.subtitles else None
    m = Moment(start=args.start, end=args.end, title=args.title or "")
    path = out_dir / video.stem / f"{_slug(args.title or '', 'clip')}_{int(args.start)}.mp4"
    render_clip(video, m, preset, path, tr, Path(args.filler) if args.filler else None)
    print(f"Готово: {path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="clipper", description="Авто-нарезчик вертикальных роликов")
    sub = parser.add_subparsers(dest="cmd", required=True)

    run_p = sub.add_parser("run", help="Найти моменты и смонтировать ролики")
    _add_common(run_p)
    run_p.add_argument("--count", type=int, default=5, help="Сколько роликов сделать")
    run_p.add_argument("--min", type=float, default=20, help="Мин. длина ролика, с")
    run_p.add_argument("--max", type=float, default=60, help="Макс. длина ролика, с")
    run_p.add_argument("--picker", choices=["auto", "claude", "heuristic"], default="auto",
                       help="Кто выбирает моменты: Claude API или локальная эвристика")
    run_p.set_defaults(func=cmd_run)

    clip_p = sub.add_parser("clip", help="Смонтировать один отрезок вручную")
    _add_common(clip_p)
    clip_p.add_argument("--start", type=float, required=True)
    clip_p.add_argument("--end", type=float, required=True)
    clip_p.add_argument("--title", default="")
    clip_p.set_defaults(func=cmd_clip)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
