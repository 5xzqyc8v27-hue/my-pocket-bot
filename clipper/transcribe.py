from __future__ import annotations

from pathlib import Path

from .models import Segment, Transcript, Word


def transcribe(video: Path, model_size: str = "small", language: str | None = "ru",
               cache: bool = True) -> Transcript:
    """Расшифровка речи локально через faster-whisper, с пословными таймингами.

    Результат кладётся рядом с видео в <имя>.transcript.json и переиспользуется.
    """
    cache_path = video.with_suffix(".transcript.json")
    if cache and cache_path.exists():
        return Transcript.load(cache_path)

    from faster_whisper import WhisperModel  # тяжёлый импорт — только когда нужен

    model = WhisperModel(model_size, device="auto", compute_type="int8")
    segments, info = model.transcribe(str(video), language=language, word_timestamps=True,
                                      vad_filter=True)
    out = Transcript(language=info.language, duration=info.duration)
    for seg in segments:
        words = [Word(start=w.start, end=w.end, text=w.word.strip()) for w in (seg.words or [])]
        out.segments.append(Segment(start=seg.start, end=seg.end, text=seg.text.strip(), words=words))
    if cache:
        out.save(cache_path)
    return out
