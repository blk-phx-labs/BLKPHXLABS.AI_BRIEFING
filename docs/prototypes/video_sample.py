"""Render a ~21 s LabMind sample Reel with the sprite host (generic lip-sync) from the Canva plates.
Usage: python sample2.py <workdir> <unused> <plates_dir> <fonts_dir> <sprites_dir>   (GCP_TOKEN, GCP_PROJECT in env)"""
import base64, json, os, subprocess, sys, wave, struct, math, urllib.request
import imageio_ffmpeg
from PIL import Image, ImageDraw, ImageFont, ImageOps

work, clip, plates, fonts = sys.argv[1:5]
FF = imageio_ffmpeg.get_ffmpeg_exe()
FPS, W, H, S = 24, 720, 1280, 720 / 1080          # render at 720x1280; Canva layout is 1080x1920
VOICE = "en-US-Journey-D"

SENTENCES = [
    "This is LabMind from Black Phoenix Labs, Monday, October 19.",
    "Danaher and NVIDIA have formed a partnership aimed at accelerating what Danaher calls autonomous science.",
    "Our read: lab operations are moving toward automation, and that will shape how cell therapies are made and released.",
]
GAP = 0.35   # pause between sentences, seconds


def run(*args):
    subprocess.run([FF, "-y", "-loglevel", "error", *args], check=True)


# 1. Voice: one TTS call per sentence, so each caption's start and end are exact.
def tts(text, path):
    req = urllib.request.Request(
        "https://texttospeech.googleapis.com/v1/text:synthesize",
        data=json.dumps({"input": {"text": text},
                         "voice": {"languageCode": "en-US", "name": VOICE},
                         "audioConfig": {"audioEncoding": "LINEAR16", "sampleRateHertz": 24000}}).encode(),
        headers={"Authorization": f"Bearer {os.environ['GCP_TOKEN']}",
                 "x-goog-user-project": os.environ["GCP_PROJECT"],
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req) as r:
        open(path, "wb").write(base64.b64decode(json.load(r)["audioContent"]))

pcm, timings, t = [], [], 0.0
for i, s in enumerate(SENTENCES):
    p = f"{work}/s{i}.wav"
    if not os.path.exists(p):
        tts(s, p)
    with wave.open(p) as w:
        rate = w.getframerate()
        frames = w.readframes(w.getnframes())
        frames = frames[44:] if frames[:4] == b"RIFF" else frames
    dur = len(frames) / 2 / rate
    timings.append((t, t + dur, s))
    pcm.append(frames + b"\x00\x00" * int(GAP * rate))
    t += dur + GAP
voice = b"".join(pcm)
speech_len = len(voice) / 2 / rate

# Loudness per video frame drives the mouth: talk while loud, hold a closed mouth in pauses.
samples = struct.unpack(f"<{len(voice) // 2}h", voice)
per = rate // FPS
loud = [math.sqrt(sum(x * x for x in samples[i:i + per]) / per) if samples[i:i + per] else 0
        for i in range(0, len(samples), per)]
talking = [v > 900 for v in loud]

# 2. Host: sprite set (closed, half, open mouth; blink), switched by voice loudness.
SPR = sys.argv[5]
EYES = (285, 345, 445, 395)
BOX = [round(v * S) for v in (66, 181, 948, 942)]          # host area inside the orange frame
side = min(BOX[2], BOX[3])

def styled(im, style):
    im = im.crop((0, 40, 720, 760))                          # head-and-shoulders, same framing as before
    if style == "orange":
        im = ImageOps.colorize(ImageOps.grayscale(im), black=(10, 10, 9), mid=(232, 82, 10),
                               white=(240, 237, 232), midpoint=150)
    return im.resize((side, side), Image.NEAREST)

mouths = {m: Image.open(f"{SPR}/{m}.png").convert("RGB") for m in ("closed", "half", "open")}
blink_eyes = Image.open(f"{SPR}/blink.png").convert("RGB").crop(EYES)
def sprite(mouth, blink):
    im = mouths[mouth].copy()
    if blink:
        im.paste(blink_eyes, EYES[:2])
    return im

# Mouth state per frame: thresholds from this voice's own loudness, held 2 frames so it doesn't flicker.
voiced = sorted(v for v in loud if v > 900)
T_HALF, T_OPEN = 900, voiced[len(voiced) * 3 // 5] if voiced else 3000
mouth_at, cur, held = [], "closed", 0
for v in loud:
    want = "open" if v > T_OPEN else "half" if v > T_HALF else "closed"
    if want != cur and held >= 2:
        cur, held = want, 0
    held += 1
    mouth_at.append(cur)

# Blinks every 2.5-4.5 s, 3 frames each (fixed seed so renders are repeatable).
import random
rng, blinks, t_b = random.Random(7), set(), 1.2
while t_b < 120:
    f0 = int(t_b * FPS); blinks.update(range(f0, f0 + 3)); t_b += rng.uniform(2.5, 4.5)

# 3. Text helpers matching the Canva layout.
# Jersey 10 matches the plates' blocky lettering and stays readable at caption size.
caption = ImageFont.truetype("fonts/Jersey10-Regular.ttf", round(86 * S))
pixel = ImageFont.truetype(f"{fonts}/silkscreen-latin-400-normal.woff2", round(70 * S))

def wrap(d, text, font, width):
    lines, cur = [], ""
    for w in text.split():
        t2 = (cur + " " + w).strip()
        if d.textlength(t2, font=font) > width and cur:
            lines.append(cur); cur = w
        else:
            cur = t2
    return lines + [cur]

def plate(name):
    return Image.open(f"{plates}/plate_{name}.png").convert("RGB").resize((W, H), Image.LANCZOS)

intro = plate("intro")
d = ImageDraw.Draw(intro)
date = "[ MON · OCT 19 ]"
d.text((W / 2, round(1180 * S)), date, font=pixel, fill=(240, 237, 232), anchor="mm")
end = plate("end")
d = ImageDraw.Draw(end)
# The Canva end plate leaves these two lines out so they share the caption font.
d.text((W / 2, round(1193 * S)), "Sources and full analysis", font=caption, fill=(240, 227, 195), anchor="mm")
d.text((W / 2, round(1433 * S)), "See you tomorrow", font=ImageFont.truetype("fonts/Jersey10-Regular.ttf", round(76 * S)),
       fill=(240, 227, 195), anchor="mm")
main_base = plate("main")

INTRO_S, END_S = 1.6, 2.4

# Caption pages: a long sentence is split into pages of up to 4 lines, each shown for its share of the
# sentence's speech time (by character count), so text never overflows the caption box.
MAX_LINES = 4
_d = ImageDraw.Draw(Image.new("RGB", (1, 1)))
pages = []
for a, b, sent in timings:
    lines = wrap(_d, sent, caption, round(800 * S))
    per = math.ceil(len(lines) / math.ceil(len(lines) / MAX_LINES))   # balanced: 5 lines -> 3 + 2, not 4 + 1
    chunks = [lines[i:i + per] for i in range(0, len(lines), per)]
    total = sum(len(" ".join(c)) for c in chunks)
    t0 = a
    for i, c in enumerate(chunks):
        t1 = b + GAP if i == len(chunks) - 1 else t0 + (b - a) * len(" ".join(c)) / total
        pages.append((t0, t1, c)); t0 = t1

def render(style):
    out = f"{work}/sample_{style}.mp4"
    frames_dir = f"{work}/frames_{style}"
    os.makedirs(frames_dir, exist_ok=True)
    cache = {}
    n = 0
    def emit(img):
        nonlocal n
        img.save(f"{frames_dir}/{n:05d}.png"); n += 1
    for _ in range(int(INTRO_S * FPS)):
        emit(intro)
    hi = 0
    for f in range(int(speech_len * FPS)):
        key = (mouth_at[f] if f < len(mouth_at) else "closed", f in blinks)
        if key not in cache:
            cache[key] = styled(sprite(*key), style)
        idx = key
        img = main_base.copy()
        img.paste(cache[idx], (BOX[0] + (BOX[2] - side) // 2, BOX[1] + (BOX[3] - side) // 2))
        now = f / FPS
        lines = next((ln for a, b, ln in pages if a <= now < b), [])
        dd = ImageDraw.Draw(img)
        y = round(1372 * S)
        for ln in lines:
            dd.text((round(96 * S), y), ln, font=caption, fill=(249, 241, 222)); y += round(100 * S)
        emit(img)
    for _ in range(int(END_S * FPS)):
        emit(end)
    # Voice starts after the intro card; pad silence to the end.
    open(f"{work}/voice.raw", "wb").write(voice)
    run("-framerate", str(FPS), "-i", f"{frames_dir}/%05d.png",
        "-f", "s16le", "-ar", str(rate), "-ac", "1", "-i", f"{work}/voice.raw",
        "-filter_complex", f"[1:a]adelay={int(INTRO_S * 1000)},apad[a]", "-map", "0:v", "-map", "[a]",
        "-shortest", "-c:v", "libx264", "-preset", "medium", "-crf", "26", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart", out)
    print(style, out, os.path.getsize(out) // 1024, "KB", f"{n / FPS:.1f}s")

render("orange")
