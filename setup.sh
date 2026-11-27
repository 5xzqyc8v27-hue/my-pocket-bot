#!/usr/bin/env bash
# Автоустановка на macOS: bash setup.sh
set -euo pipefail
cd "$(dirname "$0")"

if ! command -v brew >/dev/null; then
  echo "Нужен Homebrew: https://brew.sh — установите его и запустите скрипт снова."; exit 1
fi
command -v ffmpeg >/dev/null || brew install ffmpeg

# Нужен Python 3.10+: системный в macOS — 3.9, с ним панель не запускается.
ok() { "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; }
PY=""
for c in python3.13 python3.12 python3.11 python3.10 python3; do
  if command -v "$c" >/dev/null && ok "$c"; then PY="$c"; break; fi
done
if [ -z "$PY" ]; then
  brew install python@3.12
  PY="$(brew --prefix python@3.12)/bin/python3.12"
fi
echo "Python: $("$PY" --version)"

# окружение, собранное старым Python, пересоздаём
if [ -x .venv/bin/python ] && ! ok .venv/bin/python; then rm -rf .venv; fi
[ -d .venv ] || "$PY" -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

if [ "${1:-}" = "--local-ai" ]; then
  command -v ollama >/dev/null || brew install ollama
  (ollama list >/dev/null 2>&1) || (brew services start ollama && sleep 3)
  ollama pull "${CLIPPER_OLLAMA_MODEL:-qwen2.5:7b}"
fi

echo
echo "Готово. Запуск панели:"
echo "  source .venv/bin/activate && python -m clipper.server"
echo "Затем откройте http://127.0.0.1:8765 и вкладку «Настройка»."
