"""Make the tray icons from the SMP logo (run again if the logo changes).

    python3 agent/tools/make_tray_icons.py

tray-on-*.png  the original colours: monitoring is active (a teacher sees the screen)
tray-off-*.png black & white: the agent runs, nobody is watching
Sizes 16/20/24/32: Windows picks by the display's scaling (100/125/150/200 %).
"""

from pathlib import Path

from PIL import Image, ImageOps

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "WEBUI/Assets/SMP-transpararent.png"
OUT = ROOT / "agent/smp-agent/assets"
SIZES = (16, 20, 24, 32)

logo = Image.open(SOURCE).convert("RGBA")
logo = logo.crop(logo.getbbox())  # drop the empty margin: a tray icon is tiny
side = max(logo.size)
square = Image.new("RGBA", (side, side))
square.paste(logo, ((side - logo.width) // 2, (side - logo.height) // 2))

r, g, b, a = square.split()
gray = ImageOps.grayscale(Image.merge("RGB", (r, g, b)))
mono = Image.merge("RGBA", (gray, gray, gray, a))

OUT.mkdir(parents=True, exist_ok=True)
for size in SIZES:
    square.resize((size, size), Image.LANCZOS).save(OUT / f"tray-on-{size}.png", optimize=True)
    mono.resize((size, size), Image.LANCZOS).save(OUT / f"tray-off-{size}.png", optimize=True)
print(f"wrote {len(SIZES) * 2} icons to {OUT}")
