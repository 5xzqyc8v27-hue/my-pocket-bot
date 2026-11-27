"""Чат стрима: запись сообщений Twitch и поиск моментов, где стример читает сообщение вслух."""
from __future__ import annotations

import json
import re
import socket
import threading
import time
from difflib import SequenceMatcher
from pathlib import Path

from .models import Moment, Transcript

PRIVMSG = re.compile(r"^(?:@\S+ )?:(\w+)!\S+ PRIVMSG #\w+ :(.*)$")


def parse_line(line: str) -> tuple[str, str] | None:
    m = PRIVMSG.match(line.strip())
    return (m.group(1), m.group(2)) if m else None


class ChatLogger(threading.Thread):
    """Читает чат Twitch в режиме гостя (аккаунт не нужен) и пишет его в файл с временем."""

    def __init__(self, channel: str, out_file: Path):
        super().__init__(daemon=True)
        self.channel, self.out_file, self.alive = channel.lower(), out_file, True

    def stop(self) -> None:
        self.alive = False

    def run(self) -> None:
        self.out_file.parent.mkdir(parents=True, exist_ok=True)
        while self.alive:
            try:
                with socket.create_connection(("irc.chat.twitch.tv", 6667), timeout=30) as sock, \
                        open(self.out_file, "a", encoding="utf-8") as out:
                    sock.sendall(f"NICK justinfan{int(time.time()) % 90000 + 10000}\r\nJOIN #{self.channel}\r\n".encode())
                    sock.settimeout(300)
                    buf = b""
                    while self.alive:
                        data = sock.recv(4096)
                        if not data:
                            break
                        buf += data
                        *lines, buf = buf.split(b"\r\n")
                        for raw in lines:
                            line = raw.decode("utf-8", "replace")
                            if line.startswith("PING"):
                                sock.sendall(b"PONG :tmi.twitch.tv\r\n")
                            elif (msg := parse_line(line)):
                                out.write(json.dumps({"t": time.time(), "user": msg[0], "text": msg[1]},
                                                     ensure_ascii=False) + "\n")
                                out.flush()
            except OSError:
                pass
            if self.alive:
                time.sleep(10)  # обрыв связи — переподключаемся


def load_chat(path: Path, start: float = 0.0, length: float | None = None) -> list[dict]:
    """Сообщения из файла, время пересчитано в секунды от начала видео."""
    out = []
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            m = json.loads(line)
            t = float(m["t"]) - start
        except (ValueError, KeyError, TypeError):
            continue
        if t >= 0 and (length is None or t <= length):
            out.append({"t": round(t, 2), "user": str(m.get("user", "")), "text": str(m.get("text", ""))})
    return out


def _norm(text: str) -> list[str]:
    return [w for w in re.sub(r"[^\w\s]", " ", text.lower().replace("ё", "е")).split() if w]


def _same(a: str, b: str) -> bool:
    if a == b:
        return True
    if min(len(a), len(b)) >= 4 and a[:4] == b[:4]:
        return True
    return len(a) > 3 and SequenceMatcher(None, a, b).ratio() >= 0.75


def find_read_messages(tr: Transcript, chat: list[dict], window: float = 90.0, min_words: int = 3,
                       threshold: float = 0.6) -> list[Moment]:
    """Пары «сообщение из чата -> стример прочитал его вслух и отреагировал»."""
    words = tr.words_between(0, tr.duration or 1e9)
    tokens = [(w.start, w.end, t) for w in words for t in _norm(w.text)]
    rate = len(chat) / max(tr.duration or 1.0, 1.0)  # сообщений в секунду в среднем
    found: list[Moment] = []
    for msg in chat:
        want = _norm(msg["text"])
        if len(want) < min_words:
            continue
        zone = [(i, tok) for i, tok in enumerate(tokens) if msg["t"] - 2 <= tok[0] <= msg["t"] + window]
        best, best_i, best_j = 0.0, -1, -1
        span = int(len(want) * 1.6) + 2
        for k, (i, _) in enumerate(zone):
            part = zone[k:k + span]
            hits = [j for j, tok in part if any(_same(tok[2], w) for w in want)]
            used = {next(w for w in want if _same(tokens[j][2], w)) for j in hits}
            ratio = len(used) / len(set(want))
            if ratio > best:
                best, best_i, best_j = ratio, i, (hits[-1] if hits else i)
        if best < threshold:
            continue
        start = max(0.0, tokens[best_i][0] - 0.6)
        read_end = tokens[best_j][1]
        end = read_end + 6.0  # после чтения оставляем место под реакцию
        for seg in tr.segments:  # тянем до конца фразы, но не дольше 20 секунд реакции
            if read_end < seg.end <= read_end + 20 and seg.start <= end + 1.5:
                end = max(end, seg.end)
        burst = sum(1 for c in chat if read_end <= c["t"] <= read_end + 12) / 12
        boost = min(15, int(10 * burst / rate)) if rate > 0 else 0
        found.append(Moment(start=round(start, 2), end=round(min(end, tr.duration or end), 2),
                            title=msg["text"][:60], category="чат", score=min(99, int(45 + best * 35) + boost),
                            chat_user=msg["user"], chat_text=msg["text"],
                            reason=f"Стример прочитал сообщение из чата (совпадение {int(best * 100)}%) и ответил"))
    found.sort(key=lambda m: -m.score)
    kept: list[Moment] = []
    for m in found:
        if not any(m.start < k.end and k.start < m.end for k in kept):
            kept.append(m)
    return kept
