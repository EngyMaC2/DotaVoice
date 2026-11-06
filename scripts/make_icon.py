"""Генератор иконки приложения.

Рисует иконку DotaVoice без внешних сервисов (только Pillow) и сохраняет её в
форматах, которые нужны сборке и ярлыкам:

* ``dota_voice/static/icon.ico`` — многоразмерный ICO для Windows (окно, ярлык,
  установщик);
* ``dota_voice/static/icon.png`` — PNG 256×256 для Linux/macOS и страниц.

Запуск: ``python -m scripts.make_icon``. Файлы кладутся в пакет, поэтому их не
нужно генерировать при каждой сборке — но скрипт остаётся, чтобы иконку можно
было перерисовать одной командой.
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw

REPO_ROOT = Path(__file__).resolve().parent.parent
STATIC = REPO_ROOT / "dota_voice" / "static"

BACKGROUND = (18, 20, 28, 255)  # #12141c
RADIANT = (126, 217, 87, 255)  # #7ed957
DIRE = (255, 93, 93, 255)  # #ff5d5d

ICO_SIZES = [16, 24, 32, 48, 64, 128, 256]


def _draw(size: int) -> Image.Image:
    """Draw the icon at a given size on a supersampled canvas."""
    scale = 4
    canvas = size * scale
    image = Image.new("RGBA", (canvas, canvas), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    pad = canvas * 0.06
    draw.rounded_rectangle(
        [pad, pad, canvas - pad, canvas - pad],
        radius=canvas * 0.22,
        fill=BACKGROUND,
    )

    # Two opposing teams: light top-left, dark bottom-right.
    r = canvas * 0.11
    draw.ellipse(
        [canvas * 0.34 - r, canvas * 0.41 - r, canvas * 0.34 + r, canvas * 0.41 + r],
        fill=RADIANT,
    )
    draw.ellipse(
        [canvas * 0.66 - r, canvas * 0.59 - r, canvas * 0.66 + r, canvas * 0.59 + r],
        fill=DIRE,
    )

    # Connection lines between the teams.
    width = max(2, int(canvas * 0.05))
    draw.line(
        [canvas * 0.22, canvas * 0.72, canvas * 0.42, canvas * 0.62],
        fill=RADIANT,
        width=width,
        joint="curve",
    )
    draw.line(
        [canvas * 0.78, canvas * 0.28, canvas * 0.58, canvas * 0.38],
        fill=DIRE,
        width=width,
        joint="curve",
    )

    return image.resize((size, size), Image.LANCZOS)


def build_icon() -> Image.Image:
    return _draw(256)


def write_icons(static_dir: Path | None = None) -> list[Path]:
    static_dir = static_dir or STATIC
    static_dir.mkdir(parents=True, exist_ok=True)
    icon = build_icon()
    png = static_dir / "icon.png"
    ico = static_dir / "icon.ico"
    icon.save(png, format="PNG")
    icon.save(ico, format="ICO", sizes=[(s, s) for s in ICO_SIZES])
    return [png, ico]


def main(argv: list[str] | None = None) -> int:
    static_dir = Path(argv[0]) if argv else None
    for path in write_icons(static_dir):
        print(f"Иконка записана: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
