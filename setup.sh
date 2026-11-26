#!/usr/bin/env bash
# Автоустановка на macOS: bash setup.sh
set -euo pipefail
cd "$(dirname "$0")"

if ! command -v brew >/dev/null; then
  echo "Нужен Homebrew: https://brew.sh — установите его и запустите скрипт снова."; exit 1
fi
command -v ffmpeg >/dev/null || brew install ffmpeg
command -v python3 >/dev/null || brew install python

[ -d .venv ] || python3 -m venv .venv
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
