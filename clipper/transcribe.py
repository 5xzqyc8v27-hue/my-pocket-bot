from __future__ import annotations

from pathlib import Path

from .models import Segment, Transcript, Word


MODELS = Path(__file__).resolve().parent.parent / "models"
GITHUB = "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models"


def _download(url: str, target: Path) -> None:
    import urllib.request
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".part")
    urllib.request.urlretrieve(url, tmp)
    tmp.replace(target)


def _sherpa_files(size: str) -> tuple[Path, Path, Path, Path]:
    """Модель Whisper и детектор речи с GitHub — скачиваются один раз в папку models/."""
    import tarfile
    folder = MODELS / f"sherpa-onnx-whisper-{size}"
    if not (folder / f"{size}-tokens.txt").exists():
        archive = MODELS / f"whisper-{size}.tar.bz2"
        _download(f"{GITHUB}/sherpa-onnx-whisper-{size}.tar.bz2", archive)
        with tarfile.open(archive) as tar:
            tar.extractall(MODELS, filter="data")
        archive.unlink()
    vad = MODELS / "silero_vad.onnx"
    if not vad.exists():
        _download(f"{GITHUB}/silero_vad.onnx", vad)
    return (folder / f"{size}-encoder.int8.onnx", folder / f"{size}-decoder.int8.onnx",
            folder / f"{size}-tokens.txt", vad)


def transcribe_sherpa(video: Path, model_size: str = "small", language: str | None = "ru") -> Transcript:
    """Запасной движок: sherpa-onnx. Модели берутся с GitHub, пословных таймингов нет (фразы целиком)."""
    import subprocess

    import numpy as np
    import sherpa_onnx

    size = model_size if model_size in ("tiny", "base", "small", "medium") else "small"
    encoder, decoder, tokens, vad_path = _sherpa_files(size)
    rec = sherpa_onnx.OfflineRecognizer.from_whisper(encoder=str(encoder), decoder=str(decoder), tokens=str(tokens),
                                                     language=language or "", task="transcribe", num_threads=4)
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(video), "-vn", "-ac", "1", "-ar", "16000",
                          "-f", "f32le", "-"], capture_output=True, check=True).stdout
    audio = np.frombuffer(raw, dtype=np.float32)
    cfg = sherpa_onnx.VadModelConfig()
    cfg.silero_vad.model = str(vad_path)
    cfg.silero_vad.min_silence_duration = 0.3
    cfg.silero_vad.max_speech_duration = 12
    cfg.sample_rate = 16000
    vad = sherpa_onnx.VoiceActivityDetector(cfg, buffer_size_in_seconds=60)
    out = Transcript(language=language or "", duration=len(audio) / 16000)

    def drain() -> None:
        while not vad.empty():
            seg = vad.front
            samples = np.array(seg.samples, dtype=np.float32)
            # Движок ограничивает число слов длиной звука, и скороговорка обрезается —
            # дополняем кусок тишиной, чтобы быстрой речи хватило места.
            pad = max(0, int(max(len(samples) * 2.5, 8 * 16000)) - len(samples))
            stream = rec.create_stream()
            stream.accept_waveform(16000, np.concatenate([samples, np.zeros(pad, dtype=np.float32)]))
            rec.decode_stream(stream)
            text = stream.result.text.strip()
            if text:
                start = seg.start / 16000
                out.segments.append(Segment(start=round(start, 2), end=round(start + len(samples) / 16000, 2), text=text))
            vad.pop()

    window = cfg.silero_vad.window_size
    for i in range(0, len(audio) - window, window):
        vad.accept_waveform(audio[i:i + window])
        drain()
    vad.flush()
    drain()
    return out


def _faster_whisper(video: Path, model_size: str, language: str | None) -> Transcript:
    from faster_whisper import WhisperModel  # тяжёлый импорт — только когда нужен

    model = WhisperModel(model_size, device="auto", compute_type="int8")
    segments, info = model.transcribe(str(video), language=language, word_timestamps=True, vad_filter=True)
    out = Transcript(language=info.language, duration=info.duration)
    for seg in segments:
        words = [Word(start=w.start, end=w.end, text=w.word.strip()) for w in (seg.words or [])]
        out.segments.append(Segment(start=seg.start, end=seg.end, text=seg.text.strip(), words=words))
    return out


def transcribe(video: Path, model_size: str = "small", language: str | None = "ru",
               cache: bool = True) -> Transcript:
    """Расшифровка речи локально. Основной движок — faster-whisper (с пословными таймингами);
    если он недоступен или не смог скачать модель — запасной sherpa-onnx с моделью с GitHub.
    Движок можно задать явно: CLIPPER_STT=faster или CLIPPER_STT=sherpa.

    Результат кладётся рядом с видео в <имя>.transcript.json и переиспользуется.
    """
    import os

    cache_path = video.with_suffix(".transcript.json")
    if cache and cache_path.exists():
        return Transcript.load(cache_path)
    engine = os.environ.get("CLIPPER_STT", "auto")
    out, first_error = None, None
    if engine in ("auto", "faster"):
        try:
            out = _faster_whisper(video, model_size, language)
        except Exception as exc:  # нет библиотеки или не скачалась модель
            first_error = exc
            if engine == "faster":
                raise
    if out is None:
        try:
            out = transcribe_sherpa(video, model_size, language)
        except ImportError as exc:
            raise ImportError("Не установлен ни faster-whisper, ни sherpa-onnx") from (first_error or exc)
    if cache:
        out.save(cache_path)
    return out
