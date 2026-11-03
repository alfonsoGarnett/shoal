"""Assemble the README GIF from the frames tools/screenshots.mjs captured, and shrink the stills.

    python tools/screenshots_gif.py docs/screenshots     (needs Pillow; deletes the frames afterwards)
"""
import shutil
import sys
from pathlib import Path

from PIL import Image

out = Path(sys.argv[1] if len(sys.argv) > 1 else "docs/screenshots")
frames = sorted((out / "frames").glob("*.png"))
imgs = [Image.open(f).convert("RGB").resize((960, 600), Image.LANCZOS)
        .quantize(colors=96, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE) for f in frames]
durations = [110] * len(imgs)
durations[-1] = 1800
imgs[0].save(out / "demo.gif", save_all=True, append_images=imgs[1:], duration=durations, loop=0,
             optimize=True, disposal=1)
for name, size in (("index", (1200, 750)), ("control", (1200, 750)), ("network-paper", (1200, 750)),
                   ("phone", (390, 844))):
    Image.open(out / f"{name}.png").convert("RGB").resize(size, Image.LANCZOS).save(out / f"{name}.png", optimize=True)
shutil.rmtree(out / "frames")
shutil.rmtree(out / "cdpmedia", ignore_errors=True)
print(f"{out / 'demo.gif'}: {(out / 'demo.gif').stat().st_size // 1024} KB, {len(imgs)} frames")
