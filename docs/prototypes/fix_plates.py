"""Replace the doubled phoenix (template silhouette + official mark) with the official mark alone.
The phoenix area comes from Gemini's cleaned plate; the rest of the plate stays pixel-identical."""
import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageChops

MARK = Image.open("plates/mark_orange_1400.png").convert("RGBA")
MARK = MARK.crop(MARK.getbbox())
AREA = {"intro": (150, 120, 930, 900), "end": (140, 380, 940, 1130)}   # phoenix + glow, 1080x1920 coords

for name, area in AREA.items():
    plate = Image.open(f"plates/plate_{name}.png").convert("RGB")
    a = np.asarray(plate).astype(int)
    x0, y0, x1, y1 = area
    sub = a[y0:y1, x0:x1]
    orange = (sub[..., 0] > 180) & (sub[..., 1] < 130) & (sub[..., 2] < 70)
    ys, xs = np.where(orange)
    box = (x0 + xs.min(), y0 + ys.min(), x0 + xs.max(), y0 + ys.max())   # where the official mark sits now
    print(name, "mark box", box)

    clean = Image.open(f"cleaned/g_{name}.png").convert("RGB").resize(plate.size, Image.LANCZOS)
    mask = Image.new("L", plate.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle(area, radius=60, fill=255)
    out = Image.composite(clean, plate, mask.filter(ImageFilter.GaussianBlur(20)))

    w, h = box[2] - box[0] + 1, box[3] - box[1] + 1
    mark = MARK.resize((w, h), Image.NEAREST)
    # Glow: blurred orange copy of the mark's shape, screened onto the background.
    glow = Image.new("RGBA", plate.size, (0, 0, 0, 0))
    glow.paste(Image.new("RGBA", (w, h), (232, 82, 10, 255)), box[:2], mark.split()[3])
    glow = glow.filter(ImageFilter.GaussianBlur(28))
    g = Image.new("RGB", plate.size, (0, 0, 0)); g.paste(glow.convert("RGB"), (0, 0), glow.split()[3].point(lambda v: int(v * 0.55)))
    out = ImageChops.screen(out, g)
    out.paste(mark, box[:2], mark)
    out.save(f"out/plate_{name}.png")

sheet = Image.new("RGB", (720, 640))
for i, n in enumerate(AREA):
    sheet.paste(Image.open(f"out/plate_{n}.png").resize((360, 640)), (360 * i, 0))
sheet.save("out/fixed_sheet.png")
