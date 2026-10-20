"""Render a ~15 s LabMind sample Reel in two host styles (cream, orange) from the Canva plates.
Usage: python sample.py <workdir> <host_clip.mp4> <plates_dir> <fonts_dir>   (GCP_TOKEN, GCP_PROJECT in env)"""
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

# 2. Host frames: crop head-and-shoulders, style, upscale with hard pixels.
os.makedirs(f"{work}/hf", exist_ok=True)
if not os.listdir(f"{work}/hf"):
    run("-i", clip, f"{work}/hf/%04d.png")
host_paths = sorted(os.listdir(f"{work}/hf"))
CLOSED = 36   # frame at 1.5 s: mouth closed (picked from the contact sheet)

BOX = [round(v * S) for v in (66, 181, 948, 942)]          # host area inside the orange frame
side = min(BOX[2], BOX[3])

def styled(path, style):
    im = Image.open(path).convert("RGB").crop((0, 40, 720, 760))
    if style == "orange":
        im = ImageOps.colorize(ImageOps.grayscale(im), black=(10, 10, 9), mid=(232, 82, 10),
                               white=(240, 237, 232), midpoint=150)
    return im.resize((side, side), Image.NEAREST)

# 3. Text helpers matching the Canva layout.
serif = ImageFont.truetype("C:/Windows/Fonts/georgia.ttf", round(62 * S))
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
main_base = plate("main")

INTRO_S, END_S = 1.6, 2.4

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
        if f < len(talking) and talking[f]:
            hi = (hi + 1) % len(host_paths); idx = hi
        else:
            idx = CLOSED
        if idx not in cache:
            cache[idx] = styled(f"{work}/hf/{host_paths[idx]}", style)
        img = main_base.copy()
        img.paste(cache[idx], (BOX[0] + (BOX[2] - side) // 2, BOX[1] + (BOX[3] - side) // 2))
        now = f / FPS
        cap = next((s for a, b, s in timings if a <= now < b + GAP), "")
        dd = ImageDraw.Draw(img)
        y = round(1372 * S)
        for ln in wrap(dd, cap, serif, round(800 * S))[:5]:
            dd.text((round(96 * S), y), ln, font=serif, fill=(249, 241, 222)); y += round(72 * S)
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

for st in ("cream", "orange"):
    render(st)
