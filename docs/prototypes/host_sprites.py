"""Sprite set from Gemini's cowboy_final.mp4: one base frame plus aligned mouth and eye patches,
then a denoise (median + shared palette) so compression speckle doesn't shimmer between sprites."""
import numpy as np
from PIL import Image, ImageDraw, ImageFilter

RAW = "frames/{:04d}.png"   # ffmpeg -i Photos-1-001/cowboy_final.mp4 frames/%04d.png
BASE, PICKS = 7, {"half": 43, "open": 25}
BLINK = "g_blink.png"   # Gemini edit of the base frame; the clip's own blink (151) tilts the head
MOUTH = (320, 440, 420, 520)      # in base-frame coordinates
EYES = (285, 345, 445, 395)

def L(im): return np.asarray(im.convert("L"), float)

def offset(img, base, box):
    # Phase correlation on a region around the patch: how far the head moved between frames.
    x0, y0, x1, y1 = box
    a, b = L(img)[y0:y1, x0:x1], L(base)[y0:y1, x0:x1]
    R = np.fft.fft2(a - a.mean()) * np.conj(np.fft.fft2(b - b.mean()))
    r = np.fft.ifft2(R / (np.abs(R) + 1e-9)).real
    dy, dx = np.unravel_index(r.argmax(), r.shape)
    dy = dy - r.shape[0] if dy > r.shape[0] // 2 else dy
    dx = dx - r.shape[1] if dx > r.shape[1] // 2 else dx
    return int(dx), int(dy)

def patched(base, src_idx, patch, align_box):
    src = Image.open(RAW.format(src_idx) if isinstance(src_idx, int) else src_idx).convert("RGB").resize(base.size, Image.LANCZOS)
    dx, dy = offset(src, base, align_box)
    moved = src.transform(src.size, Image.AFFINE, (1, 0, dx, 0, 1, dy))   # shift src back onto base
    mask = Image.new("L", base.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle(patch, radius=12, fill=255)
    print(f"frame {src_idx}: head offset dx={dx} dy={dy}")
    return Image.composite(moved, base, mask.filter(ImageFilter.GaussianBlur(4)))

base = Image.open(RAW.format(BASE)).convert("RGB")
sprites = {"closed": base}
for name, idx in PICKS.items():
    # Align on the whole lower face (cheeks, beard edge), not the mouth that changes.
    sprites[name] = patched(base, idx, MOUTH, (260, 400, 480, 600))
sprites["blink"] = patched(base, BLINK, EYES, (240, 300, 500, 560))   # whole face; the nose box locks onto the wrong feature

# Denoise: median removes speckle; one palette shared by all sprites keeps colours identical between them.
clean = {k: v.filter(ImageFilter.MedianFilter(3)) for k, v in sprites.items()}
palette = clean["closed"].quantize(colors=48, method=Image.Quantize.MEDIANCUT)
for k, v in clean.items():
    v.quantize(palette=palette, dither=Image.Dither.NONE).convert("RGB").save(f"sprites/{k}.png")

sheet = Image.new("RGB", (220 * 4, 260))
for i, k in enumerate(("closed", "half", "open", "blink")):
    sheet.paste(Image.open(f"sprites/{k}.png").crop((250, 300, 470, 560)), (220 * i, 0))
sheet.save("sprites_sheet.png")
