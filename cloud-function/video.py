"""
Daily Reel: Canva plates + a sprite host with generic lip-sync + per-sentence captions.

Timeline: intro card (date) -> host reads the intro and lead with captions -> one "Also notable"
card per item, headline on top and the spoken sentence as caption -> end card for the sign-off.

Speech comes from `synthesize(text) -> 16-bit mono PCM at SAMPLE_RATE`, one call per sentence, so
every caption starts and ends exactly with its sentence. The mouth follows the voice's loudness;
nothing about the host is tied to a particular script.
"""
import math, random, re, subprocess, os
from concurrent.futures import ThreadPoolExecutor

import imageio_ffmpeg
from PIL import Image, ImageDraw, ImageFont, ImageOps

FPS, W, H = 24, 720, 1280
S = W / 1080                      # plates and layout coordinates are 1080x1920 (Canva)
SAMPLE_RATE = 24000
GAP = 0.35                        # pause after each sentence, seconds
INTRO_S, END_S = 1.6, 1.5         # silent intro card; hold on the end card after the voice

# Files under ASSET_PREFIX in the working bucket.
ASSET_PREFIX = "assets/"
ASSET_FILES = [
    "plates/plate_intro.png", "plates/plate_main.png", "plates/plate_notable.png", "plates/plate_end.png",
    "host/sprites/closed.png", "host/sprites/half.png", "host/sprites/open.png", "host/sprites/blink.png",
    "fonts/Jersey10-Regular.ttf", "fonts/Silkscreen-Regular.ttf",
]

CREAM, MUTED = (249, 241, 222), (240, 227, 195)
HOST_BOX = (66, 181, 948, 942)    # host area inside the main plate's orange frame
EYES = (285, 345, 445, 395)       # blink patch, in sprite coordinates
CAPTION = (96, 1372, 800, 4)      # main plate caption box: left, top, width, max lines
NOTABLE_HEAD = (110, 366, 860, 3)
NOTABLE_CAP = (110, 770, 860, 3)     # between the headline (max 3 lines) and the illustration


def download_assets(bucket, dest: str) -> str:
    for name in ASSET_FILES:
        path = os.path.join(dest, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        bucket.blob(ASSET_PREFIX + name).download_to_filename(path)
    return dest


def split_script(script: str, notable_headlines: list) -> list:
    """[(kind, sentence, headline)]: kind is host | notable | outro, following the script's paragraphs."""
    out = []
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", script) if p.strip()]
    for p in paragraphs:
        # A sentence can end inside quotes: ...bioprocessing." Waters ...
        sentences = [s for s in re.split(r"(?:(?<=[.!?])|(?<=[.!?][\"'”’]))\s+(?=[A-Z0-9\"'“])", p) if s.strip()]
        if p.startswith("Also notable"):
            # "Also notable." is the card's own title; each following sentence is one item, in order.
            for i, s in enumerate(sentences):
                head = notable_headlines[i - 1] if 0 < i <= len(notable_headlines) else ""
                out.append(("notable", s, head))
        elif p.startswith("Sources"):
            out.extend(("outro", s, "") for s in sentences)
        else:
            out.extend(("host", s, "") for s in sentences)
    return out


def _wrap(draw, text, font, width):
    lines, cur = [], ""
    for word in text.split():
        trial = (cur + " " + word).strip()
        if draw.textlength(trial, font=font) > width and cur:
            lines.append(cur); cur = word
        else:
            cur = trial
    return lines + ([cur] if cur else [])


def _pages(draw, text, font, width, max_lines, start, end):
    """Split a sentence into balanced caption pages (5 lines -> 3 + 2), timed by character share."""
    lines = _wrap(draw, text, font, width)
    if not lines:
        return []
    per = math.ceil(len(lines) / math.ceil(len(lines) / max_lines))
    chunks = [lines[i:i + per] for i in range(0, len(lines), per)]
    total = sum(len(" ".join(c)) for c in chunks)
    pages, t0 = [], start
    for i, c in enumerate(chunks):
        t1 = end if i == len(chunks) - 1 else t0 + (end - start) * len(" ".join(c)) / total
        pages.append((t0, t1, c)); t0 = t1
    return pages


def render_reel(script: str, notable_headlines: list, date_label: str, synthesize, assets: str,
                out_mp4: str, out_mp3: str) -> dict:
    A = lambda p: os.path.join(assets, p)
    caption_font = ImageFont.truetype(A("fonts/Jersey10-Regular.ttf"), round(86 * S))
    notable_cap_font = ImageFont.truetype(A("fonts/Jersey10-Regular.ttf"), round(64 * S))
    head_font = ImageFont.truetype(A("fonts/Jersey10-Regular.ttf"), round(104 * S))
    signoff_font = ImageFont.truetype(A("fonts/Jersey10-Regular.ttf"), round(76 * S))
    date_font = ImageFont.truetype(A("fonts/Silkscreen-Regular.ttf"), round(70 * S))
    sc = lambda v: round(v * S)

    # 1. Voice, one sentence at a time (in parallel; order is kept).
    segments = split_script(script, notable_headlines)
    with ThreadPoolExecutor(max_workers=6) as ex:
        pcms = list(ex.map(lambda seg: synthesize(seg[1]), segments))
    timeline, voice, t = [], bytearray(), 0.0
    for (kind, sentence, head), pcm in zip(segments, pcms):
        pcm = pcm[44:] if pcm[:4] == b"RIFF" else pcm          # LINEAR16 responses carry a WAV header
        dur = len(pcm) / 2 / SAMPLE_RATE
        timeline.append((kind, sentence, head, t, t + dur + GAP))
        voice += pcm + b"\x00\x00" * int(GAP * SAMPLE_RATE)
        t += dur + GAP
    speech_len = t

    # 2. Mouth state per frame from loudness; thresholds come from this voice, held 2 frames.
    n = len(voice) // 2
    samples = memoryview(bytes(voice)).cast("h")
    per = SAMPLE_RATE // FPS
    loud = []
    for i in range(0, n, per):
        chunk = samples[i:i + per]
        loud.append(math.sqrt(sum(x * x for x in chunk) / len(chunk)) if len(chunk) else 0)
    voiced = sorted(v for v in loud if v > 900)
    t_open = voiced[len(voiced) * 3 // 5] if voiced else 3000
    mouth_at, cur, held = [], "closed", 0
    for v in loud:
        want = "open" if v > t_open else "half" if v > 900 else "closed"
        if want != cur and held >= 2:
            cur, held = want, 0
        held += 1
        mouth_at.append(cur)
    rng, blinks, tb = random.Random(7), set(), 1.2
    while tb < speech_len + 5:
        f0 = int(tb * FPS); blinks.update(range(f0, f0 + 3)); tb += rng.uniform(2.5, 4.5)

    # 3. Static art.
    plate = lambda name: Image.open(A(f"plates/plate_{name}.png")).convert("RGB").resize((W, H), Image.LANCZOS)
    intro = plate("intro")
    ImageDraw.Draw(intro).text((W / 2, sc(1180)), date_label, font=date_font, fill=(240, 237, 232), anchor="mm")
    end = plate("end")
    d = ImageDraw.Draw(end)
    d.text((W / 2, sc(1193)), "Sources and full analysis", font=caption_font, fill=MUTED, anchor="mm")
    d.text((W / 2, sc(1433)), "See you tomorrow", font=signoff_font, fill=MUTED, anchor="mm")
    main_base, notable_base = plate("main"), plate("notable")

    box = [sc(v) for v in HOST_BOX]
    side = min(box[2], box[3])
    host_xy = (box[0] + (box[2] - side) // 2, box[1] + (box[3] - side) // 2)
    mouths = {m: Image.open(A(f"host/sprites/{m}.png")).convert("RGB") for m in ("closed", "half", "open")}
    blink_eyes = Image.open(A("host/sprites/blink.png")).convert("RGB").crop(EYES)
    host_cache = {}

    def host(mouth, blink):
        key = (mouth, blink)
        if key not in host_cache:
            im = mouths[mouth].copy()
            if blink:
                im.paste(blink_eyes, EYES[:2])
            im = im.crop((0, 40, 720, 760))                    # head and shoulders
            im = ImageOps.colorize(ImageOps.grayscale(im), black=(10, 10, 9), mid=(232, 82, 10),
                                   white=(240, 237, 232), midpoint=150)
            host_cache[key] = im.resize((side, side), Image.NEAREST)
        return host_cache[key]

    # Caption pages per segment, and the notable card (headline) per segment.
    measure = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    pages = []          # (start, end, kind, lines, segment index)
    for idx, (kind, sentence, head, a, b) in enumerate(timeline):
        if kind == "host":
            left, top, width, lines = CAPTION
            pages += [(p0, p1, kind, ln, idx) for p0, p1, ln in
                      _pages(measure, sentence, caption_font, sc(width), lines, a, b)]
        elif kind == "notable":
            left, top, width, lines = NOTABLE_CAP
            pages += [(p0, p1, kind, ln, idx) for p0, p1, ln in
                      _pages(measure, sentence, notable_cap_font, sc(width), lines, a, b)]
        else:
            pages.append((a, b, kind, [], idx))

    notable_cards = {}

    def notable_card(idx):
        if idx not in notable_cards:
            im = notable_base.copy()
            head = timeline[idx][2]
            if head:
                left, top, width, lines = NOTABLE_HEAD
                dd = ImageDraw.Draw(im)
                y = sc(top)
                for ln in _wrap(dd, head, head_font, sc(width))[:lines]:
                    dd.text((sc(left), y), ln, font=head_font, fill=CREAM); y += sc(118)
            notable_cards[idx] = im
        return notable_cards[idx]

    # 4. Frames stream into ffmpeg; identical frames are reused, not redrawn.
    ff = imageio_ffmpeg.get_ffmpeg_exe()
    raw_voice = out_mp4 + ".pcm"
    with open(raw_voice, "wb") as f:
        f.write(voice)
    proc = subprocess.Popen(
        [ff, "-y", "-loglevel", "error",
         "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-",
         "-f", "s16le", "-ar", str(SAMPLE_RATE), "-ac", "1", "-i", raw_voice,
         "-filter_complex", f"[1:a]adelay={int(INTRO_S * 1000)},apad[a]", "-map", "0:v", "-map", "[a]",
         "-shortest", "-c:v", "libx264", "-preset", "medium", "-crf", "26", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart", out_mp4],
        stdin=subprocess.PIPE)
    frame_cache, frames = {}, 0

    def emit(key, draw):
        nonlocal frames
        if key not in frame_cache:
            if len(frame_cache) > 400:
                frame_cache.clear()
            frame_cache[key] = draw().tobytes()
        proc.stdin.write(frame_cache[key]); frames += 1

    intro_bytes = intro.tobytes()
    for _ in range(int(INTRO_S * FPS)):
        proc.stdin.write(intro_bytes); frames += 1

    page_i = 0
    for f in range(int(speech_len * FPS)):
        now = f / FPS
        while page_i < len(pages) - 1 and now >= pages[page_i][1]:
            page_i += 1
        p0, p1, kind, lines, idx = pages[page_i]
        if kind == "outro":
            emit(("outro",), lambda: end)
        elif kind == "notable":
            def draw_notable():
                im = notable_card(idx).copy()
                dd = ImageDraw.Draw(im)
                left, top, width, _ = NOTABLE_CAP
                y = sc(top)
                for ln in lines:
                    dd.text((sc(left), y), ln, font=notable_cap_font, fill=MUTED); y += sc(76)
                return im
            emit(("notable", page_i), draw_notable)
        else:
            mouth = mouth_at[f] if f < len(mouth_at) else "closed"
            blink = f in blinks

            def draw_host():
                im = main_base.copy()
                im.paste(host(mouth, blink), host_xy)
                dd = ImageDraw.Draw(im)
                left, top, width, _ = CAPTION
                y = sc(top)
                for ln in lines:
                    dd.text((sc(left), y), ln, font=caption_font, fill=CREAM); y += sc(100)
                return im
            emit(("host", page_i, mouth, blink), draw_host)

    end_bytes = end.tobytes()
    for _ in range(int(END_S * FPS)):
        proc.stdin.write(end_bytes); frames += 1
    proc.stdin.close()
    if proc.wait() != 0:
        raise RuntimeError("ffmpeg failed while encoding the Reel")

    # Audio-only file for the page's player (no intro silence).
    subprocess.run([ff, "-y", "-loglevel", "error", "-f", "s16le", "-ar", str(SAMPLE_RATE), "-ac", "1",
                    "-i", raw_voice, "-c:a", "libmp3lame", "-b:a", "64k", out_mp3], check=True)
    os.remove(raw_voice)
    return {"seconds": round(frames / FPS, 1), "frames": frames, "segments": len(segments),
            "bytes": os.path.getsize(out_mp4)}
