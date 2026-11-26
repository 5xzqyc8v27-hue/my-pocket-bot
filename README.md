# my-pocket-bot — авто-нарезчик

Берёт длинное видео (файл или ссылку), находит сильные моменты и монтирует из них
вертикальные ролики 9:16 с надписью-хуком и субтитрами.

## Установка (macOS)

```bash
brew install ffmpeg
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

## Запуск

```bash
# найти 5 моментов и смонтировать
python -m clipper run video.mp4 --count 5 --preset center

# по ссылке, сплит-экран с залипалкой снизу
python -m clipper run "https://youtu.be/..." --preset split --filler loops/satisfying.mp4

# один отрезок вручную
python -m clipper clip video.mp4 --start 120 --end 150 --title "Надпись на экране"
```

Ролики и `moments.json` (оценка, название, подпись, хэштеги) появляются в `out/<имя видео>/`.

## Выбор моментов

Четыре способа, `--picker auto` (по умолчанию) берёт первый доступный и при сбое
переходит к следующему:

| `--picker` | На чём работает | Что нужно |
|---|---|---|
| `claude` | Claude API, тратит кредиты | `export ANTHROPIC_API_KEY=...` (модель — `CLIPPER_MODEL`) |
| `claude-cli` | Подписка Claude через Claude Code, кредиты не тратит | Установить Claude Code и один раз войти (`claude`, затем `/login`) |
| `ollama` | Локальная нейросеть, бесплатно и без интернета | `brew install ollama && ollama pull qwen2.5:7b` (модель — `CLIPPER_OLLAMA_MODEL`) |
| `heuristic` | Без нейросети: всплески громкости и плотность речи | Ничего |

Расшифровка речи и монтаж всегда идут локально и ничего не стоят.

## Раскладки (`presets/*.json`)

| Пресет | Что делает |
|---|---|
| `center` | Видео 16:9 по центру на размытом фоне |
| `square` | Квадрат по центру |
| `split` | Сверху ролик, снизу залипалка (`--filler`) |
| `reaction` | Вебка стримера и игра друг над другом; область вебки задаётся в `webcam` |

В пресете настраиваются шрифт, размер, цвет и положение надписи и субтитров,
масштаб, размытие фона, баннер. Свой пресет — скопировать json и передать `--preset путь.json`.

## Тесты

```bash
python -m pytest tests
```
