"""Стримеры: слежение за эфиром, запись кусками и очередь на автомонтаж."""
from __future__ import annotations

import json
import shutil
import subprocess
import threading
import time
import traceback
from pathlib import Path
from typing import Callable

URLS = {"twitch": "https://www.twitch.tv/{}", "kick": "https://kick.com/{}"}
CHECK_EVERY = 60  # секунд между проверками «в эфире ли»


def stream_url(platform: str, channel: str) -> str:
    return URLS[platform].format(channel)


def is_live(platform: str, channel: str) -> tuple[bool, str]:
    """В эфире ли канал. Ключи Twitch не нужны — спрашиваем через streamlink."""
    if platform == "file":
        return Path(channel).expanduser().exists(), ""
    if not shutil.which("streamlink"):
        return False, "streamlink не установлен (pip install streamlink)"
    try:
        proc = subprocess.run(["streamlink", "--json", stream_url(platform, channel)],
                              capture_output=True, text=True, timeout=45)
        data = json.loads(proc.stdout or "{}")
    except (subprocess.TimeoutExpired, json.JSONDecodeError):
        return False, "не удалось проверить эфир"
    if data.get("streams"):
        return True, ""
    err = str(data.get("error", ""))
    return False, "" if "No playable streams" in err else err[:160]


class Recorder:
    """Пишет эфир в файлы-куски: streamlink отдаёт поток, ffmpeg режет его без перекодирования."""

    def __init__(self, platform: str, channel: str, out_dir: Path, name: str, chunk_minutes: float):
        out_dir.mkdir(parents=True, exist_ok=True)
        seg = ["-c", "copy", "-f", "segment", "-segment_time", str(int(chunk_minutes * 60)), "-reset_timestamps", "1"]
        if platform == "file":  # файл режется мгновенно — имена по времени совпали бы, нумеруем
            seg += [str(out_dir / f"{name}_{time.strftime('%Y%m%d_%H%M%S')}_%03d.ts")]
        else:
            seg += ["-strftime", "1", str(out_dir / f"{name}_%Y%m%d_%H%M%S.ts")]
        self.source = None
        if platform == "file":  # готовая запись эфира — режем её на куски так же, как живой поток
            self.ffmpeg = subprocess.Popen(["ffmpeg", "-v", "error", "-i", str(Path(channel).expanduser()), *seg])
        else:
            self.source = subprocess.Popen(
                ["streamlink", "--stdout", "--twitch-disable-ads", stream_url(platform, channel), "best"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            self.ffmpeg = subprocess.Popen(["ffmpeg", "-v", "error", "-i", "pipe:0", *seg], stdin=self.source.stdout)
            self.source.stdout.close()

    def running(self) -> bool:
        return self.ffmpeg.poll() is None

    def stop(self) -> None:
        for proc in (self.source, self.ffmpeg):
            if proc and proc.poll() is None:
                proc.terminate()


class StreamManager:
    def __init__(self, streamers: dict, rec_dir: Path, lock: threading.RLock, save: Callable[[], None],
                 process_chunk: Callable[[dict, Path], int]):
        self.streamers, self.rec_dir, self.lock, self.save = streamers, rec_dir, lock, save
        self.process_chunk = process_chunk
        self.recorders: dict[str, Recorder] = {}
        self.queue: list[tuple[str, Path]] = []
        self.force: set[str] = set()

    def start(self) -> None:
        threading.Thread(target=self._watch_loop, daemon=True).start()
        threading.Thread(target=self._montage_loop, daemon=True).start()

    def stop(self, sid: str) -> None:
        rec = self.recorders.pop(sid, None)
        if rec:
            rec.stop()

    def _update(self, s: dict, **kw) -> None:
        with self.lock:
            s.update(kw)
        self.save()

    def _watch_loop(self) -> None:
        while True:
            for sid, s in list(self.streamers.items()):
                try:
                    self._tick(sid, s)
                except Exception:
                    traceback.print_exc()
            time.sleep(5)

    def _tick(self, sid: str, s: dict) -> None:
        rec = self.recorders.get(sid)
        if not s.get("enabled"):
            if rec:
                self.stop(sid)
                self._update(s, recording=False, live=False, message="Запись выключена")
            return
        if rec and not rec.running():
            self.recorders.pop(sid)
            rec = None
            done = s["platform"] == "file"
            self._update(s, recording=False, live=False, enabled=not done,
                         message="Запись обработана целиком" if done else "Эфир закончился")
        if not rec and s.get("enabled"):
            due = time.time() - s.get("last_check", 0) >= CHECK_EVERY or sid in self.force
            if due:
                self.force.discard(sid)
                live, note = is_live(s["platform"], s["channel"])
                self._update(s, live=live, last_check=time.time(),
                             message=note or ("В эфире" if live else "Не в эфире — жду"))
                if live:
                    self.recorders[sid] = rec = Recorder(s["platform"], s["channel"], self.rec_dir / sid, sid,
                                                         s.get("chunk_minutes", 10))
                    self._update(s, recording=True, live_since=time.time(), message="В эфире — пишу")
        # готовые куски: все, кроме того, который ещё пишется
        files = sorted((self.rec_dir / sid).glob("*.ts")) if (self.rec_dir / sid).is_dir() else []
        if rec and rec.running() and files:
            files = files[:-1]
        known = set(s.get("processed", [])) | {p.name for q, p in self.queue if q == sid} | {s.get("working", "")}
        for f in files:
            if f.name not in known and f.stat().st_size > 100_000:
                self.queue.append((sid, f))
        if files:
            self._update(s, last_chunk=files[-1].name)

    def _montage_loop(self) -> None:
        while True:
            if not self.queue:
                time.sleep(2)
                continue
            sid, chunk = self.queue.pop(0)
            s = self.streamers.get(sid)
            if not s:
                continue
            if not s.get("montage"):
                self._update(s, processed=[*s.get("processed", []), chunk.name][-500:])
                continue
            self._update(s, working=chunk.name, montage_message=f"Смотрю кусок {chunk.name}")
            try:
                made = self.process_chunk(s, chunk)
                note = f"Последний кусок: {made} роликов"
            except Exception as exc:
                traceback.print_exc()
                made, note = 0, f"Ошибка на куске {chunk.name}: {str(exc)[:200]}"
            self._update(s, working="", processed=[*s.get("processed", []), chunk.name][-500:],
                         chunks_done=s.get("chunks_done", 0) + 1, clips_made=s.get("clips_made", 0) + made,
                         last_clip=time.time() if made else s.get("last_clip", 0), montage_message=note)
            if s.get("delete_chunks") and chunk.exists():
                chunk.unlink()
