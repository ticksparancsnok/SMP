# Copyright (c) 2025-2026 Kiss Benedek, operating as "GG4Ever" (https://github.com/ticksparancsnok/SMP)   
# License: GNU AFFERO GENERAL PUBLIC LICENSE Version 3
# Attribution required in modified versions: see NOTICE

"""Make the tray icons and the lock screen logo from the SMP logo.

Run again if the logo changes.

    python3 -m pip install pillow
    python3 agent/tools/make_tray_icons.py

tray-on-*.png  the original colours: monitoring is active - teacher sees screen
tray-off-*.png black & white: the agent runs, nobody is watching
Sizes 16/20/24/32: Windows picks by the display's scaling (100/125/150/200 %).
lock-logo.png: shown on locked screen when the org hasn't uploaded its own logo
    (Settings -> Monitoring); LOCK_LOGO_SIZE px on its longer side
"""

from pathlib import Path

from PIL import Image, ImageOps
from PIL.Image import Resampling

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "WEBUI/Assets/SMP-transpararent.png"
OUT = ROOT / "agent/smp-agent/assets"
SIZES: tuple[int, ...] = (16, 20, 24, 32)
LOCK_LOGO_SIZE = 384

logo = Image.open(SOURCE).convert("RGBA")
# Drop the empty margin: a tray icon is tiny
logo = logo.crop(logo.getbbox())
side = max(logo.size)
square = Image.new("RGBA", (side, side))
square.paste(logo, ((side - logo.width) // 2, (side - logo.height) // 2))

r, g, b, a = square.split()
gray = ImageOps.grayscale(Image.merge("RGB", (r, g, b)))
mono = Image.merge("RGBA", (gray, gray, gray, a))

OUT.mkdir(parents=True, exist_ok=True)
for size in SIZES:
    square.resize((size, size), Resampling.LANCZOS).save(OUT / f"tray-on-{size}.png", optimize=True)  # pyright: ignore[reportUnknownMemberType] # ruff: ignore[line-too-long]
    mono.resize((size, size), Resampling.LANCZOS).save(OUT / f"tray-off-{size}.png", optimize=True)  # pyright: ignore[reportUnknownMemberType] # ruff: ignore[line-too-long]

# The lock screen: the cropped logo itself (not squared)
# Shrunk once here so the exe carries ~40 KB instead of the 2048 px original.
lock = logo.copy()
lock.thumbnail((LOCK_LOGO_SIZE, LOCK_LOGO_SIZE), Resampling.LANCZOS)
lock.save(OUT / "lock-logo.png", optimize=True)

print(f"wrote {len(SIZES) * 2} icons and lock-logo.png to {OUT}")  # ruff: ignore[print]
