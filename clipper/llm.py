"""Один вопрос к Claude: по API, если есть ключ, иначе через подписку (Claude Code)."""
from __future__ import annotations

import json
import os
import shutil
import subprocess


def available() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY")) or shutil.which("claude") is not None


def ask(prompt: str, system: str = "", max_tokens: int = 2000) -> str:
    if os.environ.get("ANTHROPIC_API_KEY"):
        import anthropic
        kwargs = {"system": system} if system else {}
        response = anthropic.Anthropic().messages.create(
            model=os.environ.get("CLIPPER_MODEL", "claude-opus-5-5"), max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}], **kwargs)
        if response.stop_reason == "refusal":
            raise RuntimeError("Claude отказался отвечать на этот запрос")
        return "".join(b.text for b in response.content if b.type == "text").strip()
    if shutil.which("claude"):
        env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
        proc = subprocess.run(["claude", "-p", "--output-format", "json"],
                              input=(system + "\n\n" if system else "") + prompt,
                              capture_output=True, text=True, timeout=600, env=env)
        if proc.returncode != 0:
            raise RuntimeError(f"Claude Code завершился с ошибкой: {(proc.stderr or proc.stdout)[-300:]}")
        try:
            return str(json.loads(proc.stdout).get("result", proc.stdout)).strip()
        except json.JSONDecodeError:
            return proc.stdout.strip()
    raise RuntimeError("Нужен Claude: ключ API или вход в Claude Code")
