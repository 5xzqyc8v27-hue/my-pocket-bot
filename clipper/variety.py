"""Разнообразие: каждый ролик пачки получает свою вариацию оформления в рамках одного стиля."""
from __future__ import annotations

import random

from .models import Preset

SUB_STYLES = [
    {"subtitle_color": "FFFFFF", "subtitle_highlight": "FFE600", "subtitle_upper": False, "subtitle_words": 3},
    {"subtitle_color": "FFFFFF", "subtitle_highlight": "4AF0B8", "subtitle_upper": True, "subtitle_words": 2},
    {"subtitle_color": "FFE600", "subtitle_highlight": "FFFFFF", "subtitle_upper": True, "subtitle_words": 1},
    {"subtitle_color": "FFFFFF", "subtitle_highlight": "FF8AD8", "subtitle_upper": False, "subtitle_words": 4},
    {"subtitle_color": "FFFFFF", "subtitle_highlight": "35D0FF", "subtitle_upper": True, "subtitle_words": 3},
]
CAMERAS = ["none", "push", "punch", "both"]


def vary(preset: Preset, seed: int) -> Preset:
    """Вариация пресета: раскладка и смысл те же, детали подачи разные."""
    rnd = random.Random(seed)
    p = preset.model_copy(deep=True)
    p.title_y = round(min(0.9, max(0.04, p.title_y + rnd.uniform(-0.025, 0.025))), 3)
    p.subtitle_y = round(min(0.95, max(0.1, p.subtitle_y + rnd.uniform(-0.03, 0.03))), 3)
    p.title_size = int(p.title_size * rnd.uniform(0.92, 1.1))
    p.subtitle_size = int(p.subtitle_size * rnd.uniform(0.92, 1.1))
    for key, value in rnd.choice(SUB_STYLES).items():
        setattr(p, key, value)
    p.subtitle_pop = rnd.random() < 0.5
    p.title_anim = rnd.random() < 0.5
    p.content_zoom = round(max(1.0, p.content_zoom * rnd.uniform(1.0, 1.1)), 2)
    p.background_blur = rnd.randint(22, 40)
    p.speed = round(rnd.uniform(1.0, 1.04), 2)
    p.saturation = round(rnd.uniform(1.0, 1.15), 2)
    p.contrast = round(rnd.uniform(1.0, 1.06), 2)
    p.camera = rnd.choice(CAMERAS)
    if not p.look and not p.lut:  # лёгкий стиль картинки, если свой не выбран
        p.look, p.look_strength = rnd.choice(["", "", "cinematic", "warm", "cold", "hdr", "sunset"]), round(rnd.uniform(0.2, 0.45), 2)
    return p
