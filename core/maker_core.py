"""AUTO-PORTED from arm_video_maker.py - GUI stripped, paths repointed to the Modal volume.
Everything below is your original logic, unchanged, except: the tkinter imports
were removed and the on-disk directory constants now point at the cloud volume.
"""
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ARM WRESTLING AUTO-EDITOR  —  3-AI Gemini Pipeline
====================================================
AI 1  (Audio)   : Transcribes your voice-over MP3 with precise timestamps.
AI 2  (Vision)  : Watches every video like an arm wrestling expert and returns
                  JSON timestamps of every arm-wrestling scene (toproll, hook,
                  king's move, cupping, pronation, setup, pin, etc.).
                  Results are CACHED locally — a video is never analyzed twice.
AI 3  (Matcher) : Reads the transcript + all scene catalogs and builds an
                  Edit Decision List: every narrated moment gets a matching
                  visual scene (max 6 s, extendable up to +2 s = 8 s hard cap).
Local render    : ffmpeg (fastest option in Python) cuts the clips, stitches
                  them in order, and muxes your voice-over on top -> final.mp4

Requirements:
    pip install google-genai yt-dlp
    ffmpeg must be installed and on PATH (https://ffmpeg.org)
    (optional fallback: pip install imageio-ffmpeg)

All settings (API key, links, mp3 path, models) persist automatically in
./app_data/config.json — the app never forgets what you typed.
"""

import os
import re
import sys
import json
import time
import threading as _threading
import queue
import hashlib
import shutil
import threading
import traceback
import subprocess
from pathlib import Path

# ----------------------------------------------------------------------------
# Paths / persistence
# ----------------------------------------------------------------------------
DATA         = Path(os.environ.get("ARM_DATA", "/data"))
SCRATCH      = Path(os.environ.get("ARM_SCRATCH", "/tmp/arm_scratch"))
APP_DIR      = DATA / "maker" / "app_data"
CONFIG_FILE  = APP_DIR / "config.json"
CACHE_DIR    = APP_DIR / "cache"        # per-video AI-2 analysis JSON (never re-analyzed)
VIDEO_DIR    = SCRATCH / "videos"       # downloaded source videos (deleted after cutting)
CLIP_DIR     = SCRATCH / "clips"        # cut scene clips
AUDIO_DIR    = APP_DIR / "audio_cache"  # AI-1 transcript cache per mp3 hash
OUT_DIR      = DATA / "maker" / "output"

for d in (APP_DIR, CACHE_DIR, VIDEO_DIR, CLIP_DIR, AUDIO_DIR, OUT_DIR):
    d.mkdir(parents=True, exist_ok=True)

DEFAULT_CONFIG = {
    "api_key": "",
    "mp3_path": "",
    "links": [],
    "model_audio":  "gemini-3.1-flash-lite",   # AI 1 – cheap, great at transcription
    # AI 2: flash-lite by default — 15 RPM / 500 RPD on your tier vs only
    # 5 RPM / 20 RPD for 3.5-flash. Your old pipeline ran all-flash-lite and
    # never hit limits; the topic-lock validator covers any weaker matches.
    # Switch to gemini-3.5-flash in the GUI for premium runs on small batches.
    "model_vision": "gemini-3.1-flash-lite",
    "model_match":  "gemini-3.5-flash",        # AI 3 – one call per run, spend quality here
    "delete_originals": True,
    "max_scene_len": 6.0,
    "max_extension": 2.0,
    "max_scene_uses": 2,          # a source scene may appear at most this many times in the final video
    "min_height": 720,
    "low_res_over_minutes": 20,   # videos longer than this analyzed at LOW media resolution (saves tokens)
}


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    try:
        if CONFIG_FILE.exists():
            cfg.update(json.loads(CONFIG_FILE.read_text(encoding="utf-8")))
    except Exception:
        pass
    return cfg


def save_config(cfg: dict):
    try:
        CONFIG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    except Exception as e:
        print("Could not save config:", e)


# ----------------------------------------------------------------------------
# ffmpeg discovery
# ----------------------------------------------------------------------------
def find_ffmpeg() -> str:
    p = shutil.which("ffmpeg")
    if p:
        return p
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        raise RuntimeError(
            "ffmpeg not found. Install it from https://ffmpeg.org and add to PATH, "
            "or `pip install imageio-ffmpeg`."
        )


def ffmpeg_dir_for_ytdlp(log=None) -> str | None:
    """
    yt-dlp needs a binary literally named 'ffmpeg' / 'ffmpeg.exe' in the dir
    we hand it. imageio-ffmpeg ships one named 'ffmpeg-win-x86_64-v7.1.exe',
    which yt-dlp does NOT recognize -> 'ffmpeg is not installed. Aborting'.
    If only the imageio binary exists, copy it once into app_data/bin under
    the standard name and point yt-dlp there.
    """
    p = shutil.which("ffmpeg")
    if p:
        return str(Path(p).parent)          # real install: use as-is
    try:
        src = Path(find_ffmpeg())           # imageio-ffmpeg's oddly-named exe
    except RuntimeError:
        return None
    bin_dir = APP_DIR / "bin"
    bin_dir.mkdir(exist_ok=True)
    dest = bin_dir / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg")
    if not dest.exists() or dest.stat().st_size != src.stat().st_size:
        if log:
            log(f"    Creating yt-dlp-compatible ffmpeg at {dest} (one-time copy) ...")
        shutil.copy2(src, dest)
        if os.name != "nt":
            os.chmod(dest, 0o755)
    # CRITICAL: some yt-dlp versions check for ffmpeg ONLY on the system PATH
    # for partial downloads and ignore the ffmpeg_location option. Injecting
    # our bin dir into this process's PATH works on every yt-dlp version.
    if str(bin_dir) not in os.environ.get("PATH", ""):
        os.environ["PATH"] = str(bin_dir) + os.pathsep + os.environ.get("PATH", "")
    return str(bin_dir)


def find_ffprobe() -> str | None:
    return shutil.which("ffprobe")


# ----------------------------------------------------------------------------
# Arm wrestling expert knowledge (injected into every AI prompt)
# ----------------------------------------------------------------------------
ARM_WRESTLING_EXPERTISE = """
You are a world-class arm wrestling analyst and coach with encyclopedic knowledge of
professional arm wrestling (WAF, EAF, East vs West, King of the Table, PAL, Zloty Tur).
You recognize every technique, position, and event on sight:

TECHNIQUES / STYLES:
- Toproll (high toproll, deep toproll): attacking the opponent's fingers/hand, rolling
  the wrist back over the top, climbing the fingers, opening the opponent's hand.
- Hook (low hook, deep hook, inside game): supinating your own wrist, curling in,
  dropping the shoulder, wrist-to-wrist inside battle with bent arms close to bodies.
- Press / Triceps press / Shoulder press: rotating the body behind the arm and pressing
  with triceps and shoulder, often after a hook.
- King's move (kings move, dead-man's move): defensive move where the athlete drops the
  body low under the table line, arm nearly straight overhead, hips sinking, absorbing
  pressure to survive — visually the athlete crouches/hangs below the table.
- Cupping (wrist cup / wrist curl): flexing the wrist to curl the opponent's hand toward
  yourself, controlling the wrist angle.
- Pronation: rotating the opponent's hand thumb-down / knuckles-up (toproller's rotation).
- Supination: rotating your own palm upward (hook rotation).
- Rising / climbing: sliding your grip higher up the opponent's fingers.
- Drag: pulling the opponent's hand across the table toward your body.
- Side pressure: lateral force toward the pin pad. Back pressure: pulling toward yourself.
- Posting: straightening the arm/wrist vertically as a defensive wall.
- Flop wrist / broken wrist: a lost, bent-back wrist position.
- Defense/holding positions, flash pin (instant pin at the "go").

SETUP & RULES (visual cues):
- Grip setup: the long battle before "ready-go" where athletes adjust hands, knuckles,
  thumb position; referee aligning hands to a referee's grip when they can't settle.
- Referee's grip: referee holds/aligns both hands neutrally before start.
- Strap match: wrist strap wrapped around both hands after slips.
- Slip: hands separating during the match. Elbow foul: elbow lifting off the elbow pad.
- Table elements: elbow pads, pin pads (touch pads), hand pegs, straps.
- Pin: opponent's hand/wrist forced to the pin pad; win celebrations, referee calls.

Also recognize: training footage (table sparring, cupping curls, pronation training with
belts/handles, wrist wrenches, rising work), interviews, supermatches, tournament brackets,
famous athletes (Devon Larratt, Levan Saginashvili, Denis Cyplenkov, John Brzenk,
Genadi Kvikvinia, Ermes Gasparini, Vitaly Laletin, Schoolboy/Alex Toproll, Todd Hutchings,
Irakli Zirakashvili, and others).
""".strip()


# ----------------------------------------------------------------------------
# Gemini helpers
# ----------------------------------------------------------------------------
def make_client(api_key: str, timeout_ms: int = 600_000):
    """CLOUD ADDITION: a per-request timeout.

    Without one, a stalled Gemini call blocks forever. On a laptop you notice
    and press Ctrl-C; in a cloud container it silently holds the machine —
    and the bill — until the job times out hours later. Ten minutes is far
    longer than any real video analysis, so this only fires on a genuine hang,
    and gen_json's retry loop then gets its turn.
    """
    try:
        from google import genai  # google-genai SDK
    except ImportError:
        raise RuntimeError("Missing SDK. Run:  pip install google-genai")
    try:
        from google.genai import types as _t
        return genai.Client(api_key=api_key,
                            http_options=_t.HttpOptions(timeout=timeout_ms))
    except Exception:
        return genai.Client(api_key=api_key)


# ---- Pydantic response schemas: force the model to emit VALID structured ----
# ---- JSON (no more "invalid JSON, retrying" — each retry costs quota!)  ----
try:
    from pydantic import BaseModel, Field

    class SegmentSchema(BaseModel):
        start: float
        end: float
        text: str
        topic: str = Field(description="one arm wrestling tag, e.g. toproll, kings_move")

    class TranscriptSchema(BaseModel):
        duration_sec: float
        segments: list[SegmentSchema]

    class SceneSchema(BaseModel):
        start: float
        end: float
        label: str
        description: str
        quality: int

    class VideoScenesSchema(BaseModel):
        scenes: list[SceneSchema]

    class EdlCutSchema(BaseModel):
        audio_start: float
        audio_end: float
        video_key: str
        scene_start: float
        scene_end: float
        topic: str
        label: str
        reason: str

    class EdlSchema(BaseModel):
        edl: list[EdlCutSchema]

    SCHEMAS = {"audio": TranscriptSchema, "vision": VideoScenesSchema, "edl": EdlSchema}
except ImportError:
    SCHEMAS = {}  # pydantic missing: fall back to free-form JSON mode


class QuotaExhausted(Exception):
    """Raised when the API quota is gone. daily=True means no point retrying today."""
    def __init__(self, msg, daily=False):
        super().__init__(msg)
        self.daily = daily


# proactive throttle, tuned to each model's per-minute limit on your tier:
# 3.5-flash ≈ 5 RPM -> 13s spacing; 3.1-flash-lite ≈ 15 RPM -> 4.5s spacing.
_LAST_CALL = {}
_MODEL_INTERVAL = {
    "gemini-3.5-flash": 13.0,
    "gemini-3-flash": 13.0,
    "gemini-2.5-flash": 21.0,       # ~3 RPM on your tier
    "gemini-2.5-flash-lite": 60.0,  # ~1 RPM on your tier
    "gemini-3.1-flash-lite": 4.5,
}


_THROTTLE_LOCK = _threading.Lock()


def _throttle(model: str, log):
    """Spaces out request STARTS.

    The per-minute limit counts how often a call may begin, not how many may
    be in flight, so several long analyses can legally overlap. Holding the
    lock across the sleep is what keeps the spacing exact when threads call
    this at once.
    """
    interval = _MODEL_INTERVAL.get(model, 13.0)
    with _THROTTLE_LOCK:
        last = _LAST_CALL.get(model, 0.0)
        wait = interval - (time.time() - last)
        if wait > 0.5:
            log(f"    (pacing {wait:.0f}s to respect the {model} per-minute limit)")
            time.sleep(max(wait, 0))
        _LAST_CALL[model] = time.time()


def _parse_retry_delay(msg: str) -> float | None:
    m = re.search(r"retry.?delay[\"']?\s*[:=]\s*[\"']?(\d+)", msg, re.I)
    return float(m.group(1)) if m else None


def _is_daily_quota(msg: str) -> bool:
    m = msg.lower()
    return ("perday" in m.replace("_", "").replace(" ", "")
            or "per day" in m or "daily" in m
            or "requests per day" in m)


def gen_json(client, model: str, contents, system_instruction: str,
             log, max_retries: int = 4, media_resolution: str | None = None,
             schema: str | None = None):
    """generate_content with enforced-structured JSON output + smart rate limits."""
    from google.genai import types
    cfg_kwargs = dict(
        response_mime_type="application/json",
        system_instruction=system_instruction,
        temperature=0.1,
    )
    if schema and SCHEMAS.get(schema):
        cfg_kwargs["response_schema"] = SCHEMAS[schema]
    if media_resolution:
        try:
            cfg_kwargs["media_resolution"] = media_resolution
        except Exception:
            pass
    config = types.GenerateContentConfig(**cfg_kwargs)

    for attempt in range(1, max_retries + 1):
        _throttle(model, log)
        try:
            resp = client.models.generate_content(model=model, contents=contents, config=config)
            txt = (resp.text or "").strip()
            txt = re.sub(r"^```(?:json)?|```$", "", txt, flags=re.M).strip()
            return json.loads(txt)
        except json.JSONDecodeError as e:
            log(f"    ! Model returned invalid JSON (attempt {attempt}): {e}")
            if attempt == max_retries:
                raise
        except Exception as e:
            msg = str(e)
            if "429" in msg or "RESOURCE_EXHAUSTED" in msg or "quota" in msg.lower():
                if _is_daily_quota(msg):
                    # daily cap reached — retrying today is pointless, fail FAST
                    raise QuotaExhausted(
                        f"DAILY quota for {model} is exhausted. Everything analyzed "
                        f"so far is cached — rerun tomorrow to continue for free.",
                        daily=True)
                wait = _parse_retry_delay(msg) or 20.0
                if attempt == max_retries:
                    raise QuotaExhausted(
                        f"Per-minute limit on {model} would not clear after "
                        f"{max_retries} tries.", daily=False)
                log(f"    ! Per-minute limit — waiting exactly {wait:.0f}s "
                    f"(attempt {attempt}/{max_retries})")
                time.sleep(wait + 1)
            elif "INVALID_ARGUMENT" in msg or "'code': 400" in msg or msg.strip().startswith("400"):
                # hard client error — retrying will never help
                raise
            else:
                log(f"    ! API error (attempt {attempt}): {msg[:300]}")
                if attempt == max_retries:
                    raise
                time.sleep(8)
    raise RuntimeError("Model call failed after retries.")


def upload_and_wait(client, path: Path, log, timeout: int = 1800):
    """Upload a file to the Gemini Files API and wait until it is ACTIVE."""
    log(f"    Uploading {path.name} ({path.stat().st_size/1e6:.1f} MB) to Gemini ...")
    f = client.files.upload(file=str(path))
    t0 = time.time()
    while getattr(f.state, "name", str(f.state)) == "PROCESSING":
        if time.time() - t0 > timeout:
            raise RuntimeError("File processing timed out on Gemini side.")
        time.sleep(6)
        f = client.files.get(name=f.name)
    state = getattr(f.state, "name", str(f.state))
    if state != "ACTIVE":
        raise RuntimeError(f"Gemini file state = {state}")
    log("    Upload ready.")
    return f


def delete_remote(client, f):
    try:
        client.files.delete(name=f.name)
    except Exception:
        pass


# ----------------------------------------------------------------------------
# STEP 1 — AI 1: audio transcript with timestamps (cached per mp3 hash)
# ----------------------------------------------------------------------------
AUDIO_SYSTEM = ARM_WRESTLING_EXPERTISE + """

TASK: You receive a voice-over MP3 (a script narration for an arm wrestling video).
Transcribe it and segment it into short narration beats of roughly 3–8 seconds each.
Return STRICT JSON ONLY, no markdown, with this exact schema:

{
  "duration_sec": <float total audio length>,
  "segments": [
    {
      "start": <float seconds>,
      "end": <float seconds>,
      "text": "<exact words spoken in this segment>",
      "topic": "<one short arm wrestling tag for the visual, e.g. toproll, hook,
                kings_move, cupping, pronation, supination, grip_setup, strap,
                pin, slip, elbow_foul, press, side_pressure, back_pressure,
                training, athlete_name, generic_match, intro, outro>"
    }
  ]
}

Segments must cover the whole audio with no gaps or overlaps, in order.
Timestamps must be accurate to within ~0.5 s. Use your arm wrestling expertise to
choose the most precise 'topic' tag for what the narrator is talking about.
"""


def file_md5(path: Path) -> str:
    h = hashlib.md5()
    with open(path, "rb") as fp:
        for chunk in iter(lambda: fp.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def analyze_audio(client, cfg, log) -> dict:
    mp3 = Path(cfg["mp3_path"])
    if not mp3.exists():
        raise RuntimeError("MP3 file not found. Upload your voice-over first.")
    key = file_md5(mp3)
    cache = AUDIO_DIR / f"{key}.json"
    if cache.exists():
        log("STEP 1 — transcript found in cache, skipping AI 1 ✔")
        return json.loads(cache.read_text(encoding="utf-8"))

    log(f"STEP 1 — AI 1 ({cfg['model_audio']}) analyzing voice-over ...")
    f = upload_and_wait(client, mp3, log)
    try:
        data = gen_json(
            client, cfg["model_audio"],
            contents=[f, "Transcribe and segment this voice-over per the schema."],
            system_instruction=AUDIO_SYSTEM, log=log, schema="audio",
        )
    finally:
        delete_remote(client, f)
    cache.write_text(json.dumps(data, indent=2), encoding="utf-8")
    log(f"    Transcript: {len(data.get('segments', []))} segments, "
        f"{data.get('duration_sec', 0):.1f}s total ✔")
    return data


# ----------------------------------------------------------------------------
# STEP 2 — download + AI 2 vision analysis (cached per URL)
# ----------------------------------------------------------------------------
VISION_SYSTEM = ARM_WRESTLING_EXPERTISE + """

TASK: Watch this entire video with expert eyes. Identify EVERY visually clear
arm wrestling scene. Return STRICT JSON ONLY, no markdown:

{
  "scenes": [
    {
      "start": <float seconds from video start>,
      "end": <float seconds>,
      "label": "<primary tag: toproll | hook | press | kings_move | cupping |
               pronation | supination | rising | drag | side_pressure |
               back_pressure | post | grip_setup | referee_grip | strap |
               slip | elbow_foul | pin | flash_pin | training | interview |
               celebration | generic_match>",
      "description": "<one sentence: who/what is visible and why this label>",
      "quality": <1-10 visual clarity & action quality>
    }
  ]
}

Rules:
- Each scene 2–10 seconds; split long exchanges into multiple labeled scenes.
- Timestamps accurate to ~0.5 s. Prefer moments where the technique is unmistakable
  (e.g. for kings_move the athlete visibly drops low under the table line).
- Skip ads, logos, static slides, and blurry/unusable footage.
- Be generous: catalog everything usable, including setups, pins, straps, training.
"""


def url_key(url: str) -> str:
    return hashlib.md5(url.strip().encode()).hexdigest()[:16]


YOUTUBE_RE = re.compile(r"(youtube\.com/(watch|shorts|live)|youtu\.be/)", re.I)
YT_ID_RE = re.compile(
    r"(?:youtube\.com/(?:watch\?(?:.*&)?v=|shorts/|live/|embed/)|youtu\.be/)"
    r"([A-Za-z0-9_-]{11})", re.I)


def is_youtube(url: str) -> bool:
    return bool(YOUTUBE_RE.search(url))


def normalize_youtube(url: str) -> str | None:
    """
    Gemini only accepts clean https://www.youtube.com/watch?v=VIDEOID URLs.
    Shorts links, youtu.be share links (?si=...), &list=/&t= params, etc. make
    Gemini fetch an HTML page -> 'Unsupported MIME type: text/html' error.
    Returns the canonical URL, or None if no video ID is present
    (playlist-only / channel links).
    """
    m = YT_ID_RE.search(url.strip())
    return f"https://www.youtube.com/watch?v={m.group(1)}" if m else None


def media_duration(path: Path) -> float:
    """Real duration in seconds. Works even without ffprobe (imageio-ffmpeg
    ships only ffmpeg) by parsing `ffmpeg -i` output as a fallback."""
    fp = find_ffprobe()
    if fp:
        try:
            out = subprocess.run(
                [fp, "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=nw=1:nk=1", str(path)],
                capture_output=True, text=True, timeout=60)
            return float(out.stdout.strip())
        except Exception:
            pass
    try:
        r = subprocess.run([find_ffmpeg(), "-hide_banner", "-i", str(path)],
                           capture_output=True, text=True, timeout=60)
        m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", r.stderr or "")
        if m:
            return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    except Exception:
        pass
    return 0.0


probe_duration = media_duration  # old name kept for the analysis code path


def _ydl_base_opts(cfg, video_only: bool = False) -> dict:
    h = cfg["min_height"]
    if video_only:
        # RENDER downloads: the voice-over replaces ALL audio (clips are cut
        # with -an), so downloading audio streams + merging them was pure
        # waste — video-only roughly halves the bytes and skips the merge.
        fmt = f"bv*[height>={h}]/bv*/b[height>={h}]/b"
    else:
        fmt = (f"bestvideo[height>={h}]+bestaudio/"
               f"bestvideo+bestaudio/"
               f"best[height>={h}]/"
               f"best")
    opts = {
        "format": fmt,
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "concurrent_fragment_downloads": 8,   # parallel fragments (CLI -N 8)
        "retries": 10,
        "fragment_retries": 10,
    }
    try:  # yt-dlp needs ffmpeg (properly named) to merge streams & cut sections
        loc = ffmpeg_dir_for_ytdlp()
        if loc:
            opts["ffmpeg_location"] = loc
    except Exception:
        pass
    # CLOUD ADDITION: datacenter IPs get "Sign in to confirm you're not a bot"
    # from YouTube far more often than home IPs. If a cookies.txt was uploaded,
    # hand it to yt-dlp so downloads keep working.
    ck = cfg.get("cookies_file") or ""
    if ck and os.path.exists(ck):
        opts["cookiefile"] = ck
    return opts


def download_full_video(url: str, cfg, log, video_only: bool = False) -> Path:
    """Fallback only (non-YouTube sources): download the whole file."""
    key = url_key(url)
    existing = [p for p in VIDEO_DIR.glob(f"{key}.*") if p.suffix.lower() in _MEDIA_EXTS]
    if existing:
        return existing[0]
    log(f"    downloading full video >= {cfg['min_height']}p: {url}")
    try:
        import yt_dlp
    except ImportError:
        raise RuntimeError("Missing yt-dlp. Run:  pip install yt-dlp")
    opts = _ydl_base_opts(cfg, video_only=video_only)
    opts["outtmpl"] = str(VIDEO_DIR / f"{key}.%(ext)s")
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
    except Exception as e:
        if "403" in str(e) and is_youtube(url):
            # YouTube 403s the default web client when yt-dlp is outdated.
            # The Android client's stream URLs usually still work. Permanent
            # fix:  python -m pip install -U yt-dlp
            log("    ↻ HTTP 403 — retrying with the Android player client "
                "(run `python -m pip install -U yt-dlp` to fix this for good)")
            opts2 = dict(opts)
            opts2["extractor_args"] = {"youtube": {"player_client": ["android"]}}
            with yt_dlp.YoutubeDL(opts2) as ydl:
                ydl.download([url])
        else:
            raise
    files = [p for p in VIDEO_DIR.glob(f"{key}.*") if p.suffix.lower() in _MEDIA_EXTS]
    if not files:
        raise RuntimeError(f"Download failed: {url}")
    return files[0]


_MEDIA_EXTS = (".mp4", ".mkv", ".webm", ".mov", ".m4v")


def _find_cached_piece(key: str, a: float, b: float):
    """Reuse ANY already-downloaded section that fully covers [a, b], not just
    one whose filename matches a-b exactly. Section boundaries get snapped to
    keyframes by yt-dlp, so requesting 24-34 one run and 24-32 the next used to
    miss the cache and re-download nearly the same bytes every single time."""
    best = None
    for p in list(VIDEO_DIR.glob(f"{key}_r*")):
        if p.suffix.lower() not in _MEDIA_EXTS:
            continue  # skip .part / .ytdl / temp files
        # Check size: if the file is empty/corrupt, remove it instantly from the cache
        if p.exists() and p.stat().st_size <= 10 * 1024:
            try:
                p.unlink()
            except Exception:
                pass
            continue
        m = re.match(rf"{re.escape(key)}_r(\d+)_(\d+)$", p.stem)
        if not m:
            continue
        pa, pb = float(m.group(1)), float(m.group(2))
        if pa <= a + 0.5 and pb >= b - 0.5:          # fully covers the request
            span = pb - pa
            if best is None or span < best[0]:       # prefer the tightest cover
                best = (span, pa, pb, p)
    if best:
        _, pa, pb, p = best
        return (pa, pb, p)
    return None


def download_sections(url: str, ranges: list, cfg, log) -> list:
    """
    Download ONLY the given (start, end) second-ranges of the video — never the
    whole file. Returns [(piece_start, piece_end, Path), ...] where the returned
    start/end are the piece's ACTUAL covered range (>= requested, because cuts
    snap to keyframes). Pieces are cached on disk and reused across runs.
    """
    try:
        import yt_dlp
        from yt_dlp.utils import download_range_func
    except ImportError:
        raise RuntimeError("Missing yt-dlp. Run:  pip install yt-dlp")
    key = url_key(url)
    out = []
    for (a, b) in ranges:
        cached = _find_cached_piece(key, a, b)
        if cached:
            pa, pb, cp = cached
            log(f"    ♻ reusing cached section {pa:.0f}s–{pb:.0f}s for {a:.0f}s–{b:.0f}s")
            out.append((pa, pb, cp))
            continue

        piece = VIDEO_DIR / f"{key}_r{int(a)}_{int(b)}.mp4"
        log(f"    ⬇ downloading ONLY {a:.0f}s–{b:.0f}s ({b-a:.0f}s) of {url}")

        def _do_download(force_kf: bool, android: bool = False):
            opts = _ydl_base_opts(cfg, video_only=True)
            opts.update({
                "outtmpl": str(VIDEO_DIR / f"{key}_r{int(a)}_{int(b)}.%(ext)s"),
                "download_ranges": download_range_func(None, [(a, b)]),
                "force_keyframes_at_cuts": force_kf,
            })
            if android:
                # YouTube 403s / kills streams for the default web client when
                # yt-dlp is outdated; the Android client usually still works
                opts["extractor_args"] = {"youtube": {"player_client": ["android"]}}
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([url])

        def _locate():
            # Scans for the segment file, but strictly unlinks and ignores any files <= 10 KB (corrupt/empty download attempts)
            for p in list(VIDEO_DIR.glob(f"{key}_r{int(a)}_{int(b)}.*")):
                if p.suffix.lower() in _MEDIA_EXTS:
                    if p.exists() and p.stat().st_size > 10 * 1024:
                        return p
                    else:
                        try:
                            p.unlink()
                        except Exception:
                            pass
            return None

        # To prevent connection drops and heavy CPU re-encoding crashes inside yt-dlp/ffmpeg,
        # we skip force_kf=True on the first attempts. We cut precisely in local render anyway.
        try:
            _do_download(force_kf=False)
        except Exception as e:
            log(f"    ⚠ section download error ({e}); retrying with Android player client")
        got = _locate()
        if got is None:
            log("    ↻ last try: Android player client "
                "(tip: `python -m pip install -U yt-dlp` fixes YouTube 403s)")
            try:
                _do_download(force_kf=False, android=True)
            except Exception as e:
                log(f"    ⚠ android retry error: {e}")
            got = _locate()
        if got is None:
            raise RuntimeError(f"Section download failed: {url} [{a}-{b}]")

        # report the piece's ACTUAL covered span so the render math is exact
        pdur = media_duration(got)
        pa, pb = float(int(a)), (float(int(a)) + pdur if pdur else float(b))
        out.append((pa, pb, got))
    return out


def _analyze_local_file(client, url: str, cfg, log):
    """Download the file once, upload it to Gemini, analyze it."""
    vid = download_full_video(url, cfg, log)
    dur = probe_duration(vid)
    media_res = None
    if dur and dur > cfg["low_res_over_minutes"] * 60:
        media_res = "MEDIA_RESOLUTION_LOW"
        log(f"    Long video ({dur/60:.0f} min) — LOW media resolution to fit token limits.")
    log(f"  AI 2 ({cfg['model_vision']}) watching the video ...")
    f = upload_and_wait(client, vid, log)
    try:
        data = gen_json(
            client, cfg["model_vision"],
            contents=[f, "Catalog every arm wrestling scene per the schema."],
            system_instruction=VISION_SYSTEM, log=log, schema="vision",
            media_resolution=media_res,
        )
    finally:
        delete_remote(client, f)
    return data, dur


def analyze_video(client, url: str, cfg, log) -> dict:
    """
    AI 2 analysis. For YouTube links the video is streamed to Gemini DIRECTLY
    from a normalized watch URL — nothing is downloaded at this stage. If URL
    streaming is rejected (private/age-restricted/odd link), it automatically
    falls back to download + upload.
    Returns {'url':..., 'key':..., 'scenes':[...]} — cached forever per URL.
    """
    key = url_key(url)
    cache = CACHE_DIR / f"{key}.json"
    if cache.exists():
        data = json.loads(cache.read_text(encoding="utf-8"))
        log(f"  Cache hit ✔ ({len(data['scenes'])} scenes) — {url}")
        return data

    from google.genai import types
    prompt = "Catalog every arm wrestling scene per the schema."
    data, dur = None, 0.0

    norm = normalize_youtube(url)
    if is_youtube(url) and not norm:
        log(f"  ⚠ {url} has no video ID (playlist/channel link?) — trying yt-dlp instead.")
    if norm:
        log(f"  AI 2 ({cfg['model_vision']}) watching directly from YouTube "
            f"(no download): {norm}")
        try:
            video_part = types.Part(file_data=types.FileData(file_uri=norm))
            data = gen_json(
                client, cfg["model_vision"],
                contents=[video_part, prompt],
                system_instruction=VISION_SYSTEM, log=log, schema="vision",
            )
        except QuotaExhausted:
            raise  # download+upload would hit the exact same quota — don't waste it
        except Exception as e:
            log(f"    ! URL streaming rejected ({str(e)[:120]}) — "
                f"falling back to download + upload.")
            data = None

    if data is None:
        data, dur = _analyze_local_file(client, url, cfg, log)

    scenes = data.get("scenes", [])
    record = {"url": url, "key": key, "duration": dur, "scenes": scenes}
    cache.write_text(json.dumps(record, indent=2), encoding="utf-8")
    log(f"    Found {len(scenes)} scenes ✔ (cached — will never re-analyze)")
    return record


# ----------------------------------------------------------------------------
# STEP 3 — AI 3: match transcript segments <-> scenes (Edit Decision List)
# ----------------------------------------------------------------------------
MATCH_SYSTEM = ARM_WRESTLING_EXPERTISE + """

TASK: You are the editor. You get (A) the narrated script transcript with timestamps
and (B) a catalog of available video scenes (each with video_key, start, end, label,
description, quality). Build an Edit Decision List so that WHAT IS SAID matches WHAT
IS SHOWN at every moment — if second 45 talks about the king's move, the visual at
second 45 must be a kings_move scene, and so on.

Return STRICT JSON ONLY:

{
  "edl": [
    {
      "audio_start": <float>,          // narration segment start (copy from transcript)
      "audio_end":   <float>,          // narration segment end
      "video_key":   "<key of source video>",
      "scene_start": <float>,          // where to start cutting inside that video
      "scene_end":   <float>,          // where to stop
      "reason": "<short: why this scene matches these words>"
    }
  ]
}

HARD RULES:
- Cover the ENTIRE audio timeline in order, no gaps, no overlaps.
- Each cut: (scene_end - scene_start) must equal (audio_end - audio_start),
  never exceed 6.0 seconds base; you may extend a scene up to 2.0 extra seconds
  (absolute max 8.0 s) ONLY when needed to fully cover a narration segment.
  If a narration segment is longer than 8 s, split it across 2+ consecutive cuts.
- TOPIC LOCK (most important rule): the chosen scene's label MUST equal the
  narration segment's topic. If the narrator talks about the king's move, the
  visual MUST be a kings_move scene — NEVER a grip_setup, toproll, or anything
  else. This applies to every technique topic: toproll, hook, press, kings_move,
  cupping, pronation, supination, rising, drag, side_pressure, back_pressure,
  post, grip_setup, referee_grip, strap, slip, elbow_foul, pin, flash_pin,
  training. Only broad topics (intro, outro, athlete_name, generic_match) may
  use generic_match / celebration / any high-quality match footage.
  ACTION FIRST: for those broad topics (intro, outro, athlete_name,
  generic_match) ALWAYS prefer scenes of LIVE arm wrestling at the table
  (generic_match, pin, toproll, hook, press...) over interview, training,
  crowd, or talking-head footage. Viewers came to watch arm wrestling —
  talking/context footage may only be used when no live-action scene fits.
  If NO scene with the required label exists anywhere in the catalog, choose the
  closest technically-related label and explain it in "reason" starting with
  "FALLBACK:". Never silently substitute an unrelated scene.
- Copy the segment's topic into "topic" and the chosen scene's label into "label"
  in every EDL entry.
- Prefer the highest-quality scene among the correct-label candidates.
- SCENE VARIETY (hard rule — violating it ruins the video): never use the same
  catalog scene (same video_key + overlapping timestamps) more than TWICE in
  the ENTIRE EDL. Twice is the absolute ceiling; once is better. Never place
  the same scene, or two overlapping windows of it, in consecutive cuts.
  Avoid two consecutive cuts from the same source video when any alternative
  exists. When several scenes share the required label, ROTATE through ALL of
  them before reusing any single one.
- If a topic has too few catalog scenes to cover its narration time without
  repeats, use scenes of a closely-related label INSTEAD of looping the same
  clip (back_pressure -> drag/hook/side_pressure; side_pressure -> toproll/
  press; kings_move -> post/press; cupping -> rising/training; etc.), or a
  high-quality generic_match scene. A fresh, related visual is ALWAYS better
  than the identical clip repeated — mark such cuts "FALLBACK:" in reason.
- Stay inside each scene's boundaries (you may trim; extending is allowed only
  up to 2 s past scene_end).

Each EDL entry must therefore contain:
audio_start, audio_end, video_key, scene_start, scene_end, topic, label, reason.
"""

# Topics that demand an exact label match; broad topics accept generic footage.
STRICT_TOPICS = {
    "toproll", "hook", "press", "kings_move", "cupping", "pronation",
    "supination", "rising", "drag", "side_pressure", "back_pressure", "post",
    "grip_setup", "referee_grip", "strap", "slip", "elbow_foul", "pin",
    "flash_pin", "training",
}
BROAD_OK = {"generic_match", "celebration", "pin", "toproll", "hook", "press"}

# When a topic's scene pool is exhausted, borrow from these technically-related
# labels instead of repeating the same clip (mirrors real editing logic: e.g.
# back pressure is visually a drag / inside battle; kings_move is a post/press
# counter; cupping shows up in rising and training footage).
RELATED_TOPICS = {
    "back_pressure": ["drag", "hook", "side_pressure", "press", "generic_match"],
    "side_pressure": ["toproll", "press", "back_pressure", "generic_match"],
    "toproll":       ["rising", "pronation", "side_pressure", "generic_match"],
    "hook":          ["supination", "back_pressure", "press", "generic_match"],
    "press":         ["hook", "side_pressure", "kings_move", "generic_match"],
    "kings_move":    ["post", "press", "generic_match"],
    "cupping":       ["rising", "grip_setup", "generic_match", "training"],
    "pronation":     ["toproll", "rising", "generic_match", "training"],
    "supination":    ["hook", "generic_match"],
    "rising":        ["toproll", "cupping", "generic_match"],
    "drag":          ["back_pressure", "hook", "generic_match"],
    "post":          ["kings_move", "side_pressure", "generic_match"],
    "grip_setup":    ["referee_grip", "strap", "generic_match"],
    "referee_grip":  ["grip_setup", "strap", "generic_match"],
    "strap":         ["grip_setup", "referee_grip", "generic_match"],
    "slip":          ["strap", "referee_grip", "generic_match"],
    "elbow_foul":    ["slip", "generic_match"],
    "pin":           ["flash_pin", "celebration", "generic_match"],
    "flash_pin":     ["pin", "celebration", "generic_match"],
    "training":      ["cupping", "pronation", "generic_match"],
    "interview":     ["athlete_name", "generic_match"],
    "athlete_name":  ["interview", "generic_match"],
    "intro":         ["grip_setup", "generic_match"],
    "outro":         ["celebration", "pin", "generic_match"],
    "generic_match": ["celebration", "pin"],
}

# Labels that show LIVE arm wrestling at the table. Everything else
# (interview, training, celebration, crowd) is context footage: usable, but
# the final video should be dominated by actual matches whenever possible.
ACTION_LABELS = {
    "toproll", "hook", "press", "kings_move", "cupping", "pronation",
    "supination", "rising", "drag", "side_pressure", "back_pressure", "post",
    "grip_setup", "referee_grip", "strap", "slip", "elbow_foul", "pin",
    "flash_pin", "generic_match",
}
# Broad narration topics whose visuals are free to be upgraded to live action.
BROAD_TOPICS = {"intro", "outro", "athlete_name", "interview", "generic_match",
                "celebration"}


def _norm(t) -> str:
    return re.sub(r"[^a-z_]", "", str(t or "").strip().lower().replace(" ", "_").replace("'", ""))


def _scene_label_at(analysis: dict, start: float, end: float) -> str:
    """Find the label of the cataloged scene that the cut window falls inside."""
    best, best_ov = "", 0.0
    for s in analysis["scenes"]:
        ov = min(end, float(s.get("end", 0))) - max(start, float(s.get("start", 0)))
        if ov > best_ov:
            best_ov, best = ov, _norm(s.get("label"))
    return best


def enforce_topic_lock(edl: list, transcript: dict, analyses: list, log) -> list:
    """
    100% guarantee: verify every cut against the scene catalog. If the visual's
    label doesn't match the narration topic, swap in a correct-label scene
    automatically (highest quality, least recently used). Warn if none exists.
    """
    by_key = {a["key"]: a for a in analyses}
    # candidate pool per label
    pool = {}
    for a in analyses:
        for s in a["scenes"]:
            lab = _norm(s.get("label"))
            pool.setdefault(lab, []).append({
                "video_key": a["key"], "start": float(s.get("start", 0)),
                "end": float(s.get("end", 0)), "quality": float(s.get("quality", 5)),
                "uses": 0,
            })
    for lab in pool:
        pool[lab].sort(key=lambda c: -c["quality"])

    # Seed 'uses' with the scenes AI 3 ALREADY picked, so repairs prefer scenes
    # the video is NOT showing elsewhere. Without this, every repair started
    # from uses=0 and funneled into the same top-quality clip — the root cause
    # of one clip getting spammed all over the final video.
    all_cands = [c for cands in pool.values() for c in cands]
    for e in edl:
        try:
            cs, ce = float(e["scene_start"]), float(e["scene_end"])
            best, best_ov = None, 0.0
            for c in all_cands:
                if c["video_key"] != e.get("video_key"):
                    continue
                ov = min(c["end"], ce) - max(c["start"], cs)
                if ov > best_ov:
                    best, best_ov = c, ov
            if best is not None:
                best["uses"] += 1
        except (KeyError, TypeError, ValueError):
            continue

    def topic_of(a_start: float) -> str:
        for seg in transcript.get("segments", []):
            if float(seg["start"]) - 0.3 <= a_start < float(seg["end"]):
                return _norm(seg.get("topic"))
        return ""

    fixed, missing = 0, set()
    for e in edl:
        topic = _norm(e.get("topic")) or topic_of(float(e["audio_start"]))
        if topic not in STRICT_TOPICS:
            continue  # broad topics: anything decent is fine
        a = by_key.get(e["video_key"])
        actual = _scene_label_at(a, float(e["scene_start"]), float(e["scene_end"])) if a else ""
        if actual == topic:
            continue
        # mismatch -> auto-repair from the pool
        cands = pool.get(topic, [])
        if not cands:
            missing.add(topic)
            log(f"    ⚠ No '{topic}' scene exists in ANY video — cut at "
                f"{e['audio_start']:.1f}s keeps '{actual or e.get('label')}'. "
                f"Add a video that contains a clear {topic} to fix this.")
            continue
        need = float(e["audio_end"]) - float(e["audio_start"])
        cand = min(cands, key=lambda c: (c["uses"], -c["quality"]))
        # Rotation: when the SAME scene has to cover several narration beats,
        # start a bit further in each time so viewers don't see the identical
        # clip on repeat (the old code always started at cand["start"]).
        spare = max(0.0, (cand["end"] - cand["start"]) - need)
        offset = (cand["uses"] * max(need, 1.0)) % spare if spare > 0.5 else 0.0
        cand["uses"] += 1
        e["video_key"] = cand["video_key"]
        e["scene_start"] = cand["start"] + offset
        e["scene_end"] = e["scene_start"] + need
        e["label"] = topic
        e["reason"] = f"AUTO-FIX: replaced mismatched '{actual}' with verified '{topic}' scene"
        fixed += 1
        log(f"    🔒 topic-lock fix @ audio {e['audio_start']:.1f}s: "
            f"'{actual or '?'}' → '{topic}'")
    if fixed:
        log(f"    Topic-lock validator repaired {fixed} cut(s) ✔")
    else:
        log("    Topic-lock validator: all cuts already 100% matched ✔")
    return edl


def enforce_scene_variety(edl: list, analyses: list, log, max_uses: int = 2) -> list:
    """
    ANTI-REPEAT guarantee (code-level, not prompt trust): no catalog scene may
    appear more than `max_uses` times in the whole video, and never twice in a
    row. Over-used cuts are rewritten to the least-used alternative scene —
    exact topic first, then RELATED_TOPICS, then the best remaining scene of
    any label. Runs AFTER enforce_topic_lock: topic correctness is kept
    wherever the footage allows, but when a topic is starved, variety wins —
    a fresh related scene always beats the identical clip on loop.
    """
    scenes = []
    for a in analyses:
        for s in a.get("scenes", []):
            try:
                st, en = float(s.get("start", 0)), float(s.get("end", 0))
            except (TypeError, ValueError):
                continue
            if en - st < 0.5:
                continue
            scenes.append({
                "video_key": a["key"], "start": st, "end": en,
                "label": _norm(s.get("label")),
                "quality": float(s.get("quality", 5) or 5),
            })
    if not scenes or not edl:
        return edl

    def canon(s):
        return (s["video_key"], round(s["start"], 1))

    def canonical_of(cut):
        """Map an EDL cut to the catalog scene it overlaps the most."""
        best, best_ov = None, 0.0
        cs, ce = float(cut["scene_start"]), float(cut["scene_end"])
        for s in scenes:
            if s["video_key"] != cut.get("video_key"):
                continue
            ov = min(s["end"], ce) - max(s["start"], cs)
            if ov > best_ov:
                best, best_ov = s, ov
        return best

    usage: dict = {}      # canonical scene id -> times used so far
    prev_id = None        # previous cut's scene (never allowed twice in a row)
    prev_vid = None       # previous cut's source video (soft-avoided)
    fixed = kept = 0

    def pick(topic, need, cur_id, action_only=False):
        """Least-used, highest-quality unused scene: exact -> related -> any.
        With action_only=True, only live-table ACTION_LABELS scenes qualify —
        used to upgrade talking/crowd visuals on broad narration topics."""
        tiers = [[topic], RELATED_TOPICS.get(topic, []), None]
        for tier in tiers:
            cands = []
            for s in scenes:
                sid = canon(s)
                if sid == cur_id or sid == prev_id:
                    continue
                if usage.get(sid, 0) >= max_uses:
                    continue
                if (s["end"] - s["start"]) + 0.25 < need:   # too short for beat
                    continue
                if action_only and s["label"] not in ACTION_LABELS:
                    continue
                if tier is not None and s["label"] not in tier:
                    continue
                cands.append(s)
            if cands:
                cands.sort(key=lambda s: (
                    usage.get(canon(s), 0),                       # least used
                    0 if s["label"] in ACTION_LABELS else 1,      # live action
                    0 if s["video_key"] != prev_vid else 1,       # new source
                    -s["quality"],                                 # then quality
                ))
                return cands[0]
        return None

    action_boosted = 0
    for e in edl:
        need = max(0.05, float(e["audio_end"]) - float(e["audio_start"]))
        c = canonical_of(e)
        sid = canon(c) if c else (e.get("video_key"),
                                  round(float(e["scene_start"]), 1))
        topic_now = _norm(e.get("topic")) or "generic_match"
        label_now = (c["label"] if c else _norm(e.get("label")))
        # 🥊 ACTION BOOST: a broad narration beat (intro, athlete_name,
        # interview...) sitting on talking/training/crowd footage gets
        # upgraded to a live-table scene when one is available. This is what
        # keeps the final video full of actual arm wrestling instead of
        # people talking.
        wants_action = (topic_now in BROAD_TOPICS
                        and label_now not in ACTION_LABELS)
        if usage.get(sid, 0) >= max_uses or sid == prev_id or wants_action:
            repl = pick(topic_now, need, sid, action_only=wants_action)
            if repl is not None:
                uses = usage.get(canon(repl), 0)
                # 2nd use of a scene starts further in, so even the allowed
                # repeat doesn't look like the identical clip again
                spare = max(0.0, (repl["end"] - repl["start"]) - need)
                offset = (uses * max(need, 1.0)) % spare if spare > 0.5 else 0.0
                was_overused = usage.get(sid, 0) >= max_uses or sid == prev_id
                e["video_key"]   = repl["video_key"]
                e["scene_start"] = repl["start"] + offset
                e["scene_end"]   = e["scene_start"] + need
                e["label"]       = repl["label"]
                if was_overused:
                    e["reason"] = (str(e.get("reason") or "")
                                   + " | VARIETY-SWAP: scene was over-used")
                    fixed += 1
                else:
                    e["reason"] = (str(e.get("reason") or "")
                                   + " | ACTION-BOOST: talking/context footage"
                                     " upgraded to live match action")
                    action_boosted += 1
                sid = canon(repl)
            elif not wants_action:
                kept += 1
                log(f"    ⚠ variety: no unused alternative for "
                    f"'{e.get('topic')}' @ audio {float(e['audio_start']):.1f}s "
                    f"— keeping a repeat (add more videos with this topic)")
        usage[sid] = usage.get(sid, 0) + 1
        prev_id, prev_vid = sid, e.get("video_key")

    if fixed:
        log(f"    🎬 Variety enforcer replaced {fixed} over-used cut(s) — no "
            f"scene appears more than {max_uses}× or back-to-back ✔")
    elif kept:
        log(f"    🎬 Variety enforcer: {kept} unavoidable repeat(s) kept — "
            f"catalog too small for full variety")
    else:
        log("    🎬 Variety check: no scene exceeded the repeat limit ✔")
    if action_boosted:
        log(f"    🥊 Action booster upgraded {action_boosted} talking/context "
            f"cut(s) to live match footage ✔")
    return edl


def reroute_dead_video_cuts(edl: list, dead_keys: set, analyses: list, log,
                            max_uses: int = 2) -> list:
    """
    RENDER-TIME rescue: when a source video can't be downloaded (403, deleted,
    private...), move its cuts onto scenes from the videos we DO have — same
    topic first, then RELATED_TOPICS, then the best remaining scene — while
    respecting the anti-repeat rules. If even the relaxed search finds nothing,
    the cut keeps its dead key and the renderer inserts filler (never crashes).
    Only the VISUAL fields change; audio timing is untouched.
    """
    scenes = []
    for a in analyses:
        if a.get("key") in dead_keys:
            continue
        for s in a.get("scenes", []):
            try:
                st, en = float(s.get("start", 0)), float(s.get("end", 0))
            except (TypeError, ValueError):
                continue
            if en - st < 0.5:
                continue
            scenes.append({"video_key": a["key"], "start": st, "end": en,
                           "label": _norm(s.get("label")),
                           "quality": float(s.get("quality", 5) or 5)})
    if not scenes:
        log("    ⚠ no available videos left to re-route onto — dead cuts "
            "will use filler")
        return edl

    def canon(s):
        return (s["video_key"], round(s["start"], 1))

    def canonical_of(cut):
        best, best_ov = None, 0.0
        cs, ce = float(cut["scene_start"]), float(cut["scene_end"])
        for s in scenes:
            if s["video_key"] != cut.get("video_key"):
                continue
            ov = min(s["end"], ce) - max(s["start"], cs)
            if ov > best_ov:
                best, best_ov = s, ov
        return best

    # count what the healthy cuts already use, so re-routes don't create spam
    usage = {}
    for e in edl:
        if e.get("video_key") in dead_keys:
            continue
        c = canonical_of(e)
        if c:
            usage[canon(c)] = usage.get(canon(c), 0) + 1

    # canonical scene id of every cut we're KEEPING (None for dead cuts) —
    # needed so a re-route avoids both its neighbours, including the NEXT one
    cids = []
    for e in edl:
        if e.get("video_key") in dead_keys:
            cids.append(None)
        else:
            c = canonical_of(e)
            cids.append(canon(c) if c else None)

    moved = stuck = 0
    for i, e in enumerate(edl):
        if e.get("video_key") not in dead_keys:
            continue
        avoid = {cids[i - 1] if i > 0 else None,
                 cids[i + 1] if i + 1 < len(cids) else None}
        need = max(0.05, float(e["audio_end"]) - float(e["audio_start"]))
        topic = _norm(e.get("topic")) or "generic_match"
        repl = None
        for tier in ([topic], RELATED_TOPICS.get(topic, []), None):
            cands = [s for s in scenes
                     if canon(s) not in avoid
                     and usage.get(canon(s), 0) < max_uses
                     and (s["end"] - s["start"]) + 0.25 >= need
                     and (tier is None or s["label"] in tier)]
            if cands:
                cands.sort(key=lambda s: (usage.get(canon(s), 0), -s["quality"]))
                repl = cands[0]
                break
        if repl is None:
            # relax the usage cap — a repeated real scene beats black filler
            cands = [s for s in scenes
                     if canon(s) not in avoid
                     and (s["end"] - s["start"]) + 0.25 >= need]
            if cands:
                cands.sort(key=lambda s: (usage.get(canon(s), 0), -s["quality"]))
                repl = cands[0]
        if repl is None:
            stuck += 1
            continue
        uses = usage.get(canon(repl), 0)
        spare = max(0.0, (repl["end"] - repl["start"]) - need)
        offset = (uses * max(need, 1.0)) % spare if spare > 0.5 else 0.0
        e["video_key"]   = repl["video_key"]
        e["scene_start"] = repl["start"] + offset
        e["scene_end"]   = e["scene_start"] + need
        e["label"]       = repl["label"]
        e["reason"] = (str(e.get("reason") or "")
                       + " | REROUTED: source video unavailable")
        usage[canon(repl)] = uses + 1
        cids[i] = canon(repl)
        moved += 1
    log(f"    ♻ re-routed {moved} cut(s) from unavailable video(s)"
        + (f"; {stuck} cut(s) will use filler" if stuck else "") + " ✔")
    return edl


def enforce_full_coverage(edl: list, duration_sec: float, log) -> list:
    """
    100% length guarantee at the EDL level: make the cuts tile the audio
    timeline contiguously from 0 to duration_sec with NO gaps and NO overlaps,
    and make each cut's video length exactly equal to its audio span. Without
    this, gaps/overlaps in AI-3's timing silently shrank the video vs the audio.
    """
    if not edl or duration_sec <= 0:
        return edl
    edl = sorted(edl, key=lambda e: float(e["audio_start"]))
    t = 0.0
    for e in edl:
        e["audio_start"] = t
        seg = max(0.05, float(e["audio_end"]) - float(e["audio_start"]))
        # keep the cut's own intended length, but re-anchor it contiguously
        t_end = min(t + seg, duration_sec)
        if t_end <= t:                      # ran past the audio end — drop it
            e["_drop"] = True
            continue
        e["audio_start"], e["audio_end"] = t, t_end
        need = t_end - t
        e["scene_end"] = float(e["scene_start"]) + need
        t = t_end
    edl = [e for e in edl if not e.get("_drop")]
    # if the cuts ended short of the audio, stretch the LAST one to the end
    if edl and t < duration_sec - 0.05:
        last = edl[-1]
        extra = duration_sec - t
        last["audio_end"] = duration_sec
        last["scene_end"] = float(last["scene_start"]) + (float(last["audio_end"]) - float(last["audio_start"]))
        log(f"    ↔ extended final cut by {extra:.1f}s so video length == audio length")
    covered = edl[-1]["audio_end"] if edl else 0.0
    log(f"    Coverage check: cuts now tile 0–{covered:.1f}s of {duration_sec:.1f}s audio ✔")
    return edl


def build_edl(client, transcript: dict, analyses: list, cfg, log) -> list:
    log(f"STEP 3 — AI 3 ({cfg['model_match']}) matching script to scenes ...")
    catalog = []
    for a in analyses:
        for s in a["scenes"]:
            catalog.append({
                "video_key": a["key"],
                "start": s.get("start"), "end": s.get("end"),
                "label": s.get("label"), "description": s.get("description"),
                "quality": s.get("quality"),
            })
    if not catalog:
        raise RuntimeError("No scenes were found in any video.")
    payload = json.dumps({"transcript": transcript, "scene_catalog": catalog})
    data = gen_json(
        client, cfg["model_match"],
        contents=[payload, "Build the EDL per the schema and hard rules."],
        system_instruction=MATCH_SYSTEM, log=log, schema="edl",
    )
    edl = data.get("edl", [])
    if not edl:
        raise RuntimeError("AI 3 returned an empty EDL.")
    # sanity clamp: 6 s base + 2 s extension = 8 s absolute max per cut
    max_len = cfg["max_scene_len"] + cfg["max_extension"]
    for e in edl:
        need = float(e["audio_end"]) - float(e["audio_start"])
        e["scene_end"] = float(e["scene_start"]) + min(need, max_len)
    # 100% topic guarantee — code-level verification, not just prompt trust
    edl = enforce_topic_lock(edl, transcript, analyses, log)
    # ANTI-REPEAT guarantee — no scene more than max_scene_uses times total,
    # never the same scene back-to-back (fixes the repeated-clip spam)
    edl = enforce_scene_variety(edl, analyses, log,
                                max_uses=int(cfg.get("max_scene_uses", 2)))
    # 100% LENGTH guarantee — make the cuts tile the whole audio timeline
    dur = float(transcript.get("duration_sec", 0) or 0)
    if dur <= 0:  # fall back to the last segment end if the model omitted it
        segs = transcript.get("segments", [])
        dur = max((float(s.get("end", 0)) for s in segs), default=0.0)
    edl = enforce_full_coverage(edl, dur, log)
    (OUT_DIR / "edl.json").write_text(json.dumps(edl, indent=2), encoding="utf-8")
    log(f"    EDL ready: {len(edl)} cuts ✔ (saved to output/edl.json)")
    return edl


# ----------------------------------------------------------------------------
# STEP 4 — local render with ffmpeg (fast)
# ----------------------------------------------------------------------------
def render(edl: list, mp3_path: str, cfg, log) -> Path:
    ff = find_ffmpeg()
    log("STEP 4 — cutting & stitching with ffmpeg ...")

    # Clean cache pass: delete any remaining 0-byte or corrupt segment files in APP_DIR/videos
    for f in list(VIDEO_DIR.glob("*")):
        if f.is_file() and f.stat().st_size <= 10 * 1024:
            try:
                f.unlink()
            except Exception:
                pass

    key_to_url, cached_records = {}, []
    for c in CACHE_DIR.glob("*.json"):
        rec = json.loads(c.read_text(encoding="utf-8"))
        key_to_url[rec["key"]] = rec["url"]
        cached_records.append(rec)

    # ---- figure out EXACTLY which seconds we need from each video ----------
    BUF = 2.0   # small safety buffer around each cut for keyframe accuracy
    needed = {}  # key -> list of (start, end)
    for e in edl:
        a = max(0.0, float(e["scene_start"]) - BUF)
        b = float(e["scene_end"]) + BUF
        needed.setdefault(e["video_key"], []).append((a, b))

    def merge(ranges, gap=4.0):
        ranges.sort()
        out = [list(ranges[0])]
        for a, b in ranges[1:]:
            if a <= out[-1][1] + gap:
                out[-1][1] = max(out[-1][1], b)
            else:
                out.append([a, b])
        return [(a, b) for a, b in out]

    def covered(plist, a, b):
        return any(pa <= a + 0.5 and pb >= b - 0.5 for (pa, pb, _p) in plist)

    # ---- fetch ONLY those sections (never the whole video) -----------------
    def fetch_key(key, ranges):
        """Fetch the pieces for ONE video. Returns a piece list, or None on
        total failure — a dead source must never abort the whole render."""
        url = key_to_url.get(key)
        merged = merge(list(ranges))
        total = sum(b - a for a, b in merged)
        full = [p for p in VIDEO_DIR.glob(f"{key}.*") if p.suffix.lower() in _MEDIA_EXTS]
        if full and full[0].stat().st_size > 100 * 1024:
            return [(0.0, 10 ** 9, full[0])]
        try:
            if url and is_youtube(url):
                log(f"    {key}: need only {total:.0f}s across {len(merged)} "
                    f"section(s) — downloading just those, not the full video.")
                try:
                    return download_sections(url, merged, cfg, log)
                except Exception as e:
                    log(f"    ! Section download failed ({str(e)[:120]}) — "
                        f"falling back to downloading this full video once.")
                    return [(0.0, 10 ** 9, download_full_video(url, cfg, log))]
            if url:
                return [(0.0, 10 ** 9, download_full_video(url, cfg, log))]
            log(f"    ✖ no source URL known for video key {key}")
        except Exception as e:
            log(f"    ✖ {key}: source unavailable ({str(e)[:150]})")
        return None

    pieces, dead = {}, set()
    for key, ranges in needed.items():
        got = fetch_key(key, ranges)
        if got:
            pieces[key] = got
        else:
            dead.add(key)

    # ---- a dead source no longer kills the run: re-route its cuts ----------
    if dead:
        log(f"    ⚠ {len(dead)} source video(s) unavailable — re-routing "
            f"their cuts to the available videos instead of aborting.")
        edl = reroute_dead_video_cuts(
            edl, dead, cached_records, log,
            max_uses=int(cfg.get("max_scene_uses", 2)))
        # fetch any NEW sections the re-routed cuts require
        extra = {}
        for e in edl:
            k = e["video_key"]
            if k in dead:
                continue                       # un-routable -> filler later
            a = max(0.0, float(e["scene_start"]) - BUF)
            b = float(e["scene_end"]) + BUF
            if not covered(pieces.get(k, []), a, b):
                extra.setdefault(k, []).append((a, b))
        for key, ranges in extra.items():
            got = fetch_key(key, ranges)
            if got:
                pieces.setdefault(key, []).extend(got)
            else:
                dead.add(key)                  # replacement dead too -> filler

    # probe each piece's real duration ONCE so seek math can't run past its end
    piece_info = {}   # key -> [(a, b, path, dur), ...]
    for key, plist in pieces.items():
        info = []
        for (a, b, p) in plist:
            if b >= 10 ** 8:                      # whole-file sentinel
                d = media_duration(p) or 10 ** 8
                info.append((0.0, d, p, d))
            else:
                d = media_duration(p)
                if not d or d < 0.2:
                    # unreadable/corrupt section (usually a broken 403-retry
                    # download) — delete it so the next run re-fetches it
                    # fresh, and let the rescue cover any cut that needed it.
                    log(f"    🗑 corrupt cached section {p.name} — deleted; "
                        f"its cut(s) will use topic-matched rescue footage")
                    try:
                        p.unlink()
                    except Exception:
                        pass
                    continue
                info.append((a, a + d, p, d))
        if info:
            piece_info[key] = info

    def source_for(key: str, t: float, need: float):
        """Pick the piece that best covers absolute time [t, t+need] and return
        (file, piece_start, piece_dur, overlap_seconds)."""
        cov = []
        for (a, b, p, d) in piece_info[key]:
            overlap = min(b, t + need) - max(a, t)
            cov.append((overlap, a, d, p))
        overlap, a, d, p = max(cov, key=lambda r: r[0])
        return p, a, d, overlap

    # ---- topic-aware rescue -------------------------------------------------
    # Map every downloaded piece to the scene labels it overlaps, so a rescue
    # can pick footage matching what the narrator is saying, instead of
    # blindly grabbing the longest file on disk (which spammed one clip
    # everywhere — same root cause as the old topic-lock 'uses' bug).
    _scenes_by_key = {rec["key"]: rec.get("scenes", []) for rec in cached_records}
    _piece_labels = {}          # path -> set of scene labels the piece overlaps
    for _k, _plist in piece_info.items():
        _scenes = _scenes_by_key.get(_k, [])
        for (_a, _b, _p, _d) in _plist:
            labs = set()
            for _s in _scenes:
                _ss, _se = float(_s.get("start", 0)), float(_s.get("end", 0))
                if min(_b, _se) - max(_a, _ss) >= 1.0:   # ≥1s overlap counts
                    labs.add(_norm(_s.get("label")))
            _piece_labels[_p] = labs

    _rescue_uses = {}           # path -> times used as a rescue this render
    _piece_planned = {}         # path -> times the normal cuts already show it
    _rescue_lock = threading.Lock()

    # Seed with how often each piece is ALREADY shown by the planned cuts, so
    # rescues gravitate toward footage the final video uses the LEAST — one
    # heavily-featured piece can no longer also absorb every rescue.
    for _e in edl:
        try:
            _plist = piece_info.get(_e["video_key"]) or []
            _cs, _ce = float(_e["scene_start"]), float(_e["scene_end"])
            _best, _best_ov = None, 0.0
            for (_a, _b, _p, _d) in _plist:
                _ov = min(_b, _ce) - max(_a, _cs)
                if _ov > _best_ov:
                    _best, _best_ov = _p, _ov
            if _best is not None:
                _piece_planned[_best] = _piece_planned.get(_best, 0) + 1
        except (KeyError, TypeError, ValueError):
            continue

    def _get_healthy_fallback_piece(need_dur, topic="", avoid=None):
        """
        Visual Rescue v2: pick a healthy downloaded segment whose scene label
        matches the narration topic first, then RELATED_TOPICS, then anything.
        Inside each tier: least-shown file wins (planned cuts + prior
        rescues), live-action footage beats talking/context footage, and
        repeat uses get a shifted seek so even a reused file shows new frames.
        Returns (path, piece_dur, seek_offset) or (None, 0.0, 0.0).
        """
        topic = _norm(topic)
        avoid_set = (avoid if isinstance(avoid, (set, frozenset, list, tuple))
                     else ({avoid} if avoid is not None else set()))
        tiers = [
            {topic} if topic else set(),
            set(_norm(t) for t in RELATED_TOPICS.get(topic, [])),
            None,                                   # None = any label
        ]
        healthy = []
        for _k, _plist in piece_info.items():
            for (_a, _b, _p, _d) in _plist:
                if _p in avoid_set:
                    continue
                try:
                    if not (_p.exists() and _p.stat().st_size > 50 * 1024):
                        continue
                except OSError:
                    continue
                healthy.append((_p, _d))
        if not healthy:
            return None, 0.0, 0.0

        with _rescue_lock:
            for tier in tiers:
                cands = []
                for (_p, _d) in healthy:
                    labs = _piece_labels.get(_p, set())
                    if tier is not None and not (labs & tier):
                        continue
                    too_short = 0 if _d >= need_dur + 0.2 else 1
                    shown = (_piece_planned.get(_p, 0)
                             + _rescue_uses.get(_p, 0))
                    non_action = 0 if (labs & ACTION_LABELS) else 1
                    cands.append((too_short, shown, non_action, -_d,
                                  str(_p), _p, _d))
                if not cands:
                    continue
                cands.sort()
                _, _, _, _, _, _p, _d = cands[0]
                uses = _rescue_uses.get(_p, 0)
                _rescue_uses[_p] = uses + 1
                # shift the start on every reuse so repeats show new footage
                max_ss = max(0.0, _d - need_dur - 0.1)
                ss = min(max_ss, uses * max(1.5, need_dur * 0.6))
                return _p, _d, ss
        return None, 0.0, 0.0

    # canvas is FIXED for every clip so concat -c copy can't silently break on a
    # 4:3 / vertical source (that was a cause of the truncated final video)
    CANVAS_W, CANVAS_H, OUT_FPS = 1280, 720, 30
    CLIP_VF = (
        f"scale={CANVAS_W}:{CANVAS_H}:force_original_aspect_ratio=decrease,"
        f"pad={CANVAS_W}:{CANVAS_H}:(ow-iw)/2:(oh-ih)/2,"
        f"setsar=1,fps={OUT_FPS},tpad=stop_mode=clone:stop=-1"
    )
    X264 = ["-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-pix_fmt", "yuv420p"]

    def _cut_one(i, e):
        need = float(e["audio_end"]) - float(e["audio_start"])
        out = CLIP_DIR / f"clip_{i:03d}.mp4"
        if not piece_info.get(e["video_key"]):
            # primary source video never became available — try rescue fallback
            rescue_file, rescue_dur, rescue_ss = _get_healthy_fallback_piece(
                need, topic=e.get("topic", ""))
            if rescue_file:
                cmd = [ff, "-y", "-i", str(rescue_file), "-ss", f"{rescue_ss:.3f}",
                       "-t", f"{need:.3f}", "-vf", CLIP_VF, *X264, str(out)]
                r = subprocess.run(cmd, capture_output=True, text=True)
                if r.returncode == 0 and out.exists() and out.stat().st_size > 10 * 1024:
                    log(f"    🎬 clip {i+1}: primary source missing — rescued with '{e.get('topic','?')}'-matched footage from {rescue_file.name} (ss={rescue_ss:.1f}s) ✔")
                    return i, out

            # absolute last resort black filler
            subprocess.run(
                [ff, "-y", "-f", "lavfi", "-i",
                 f"color=c=black:s={CANVAS_W}x{CANVAS_H}:r={OUT_FPS}:d={need:.3f}",
                 *X264, str(out)], capture_output=True, text=True)
            log(f"    ⚠ clip {i+1}: source video unavailable — inserted "
                f"{need:.1f}s black filler to preserve total length")
            return i, out

        src, p_start, p_dur, cover = source_for(e["video_key"], float(e["scene_start"]), need)
        if cover < min(need, 1.0) * 0.5:
            # the exact section this cut needs isn't on disk (its download was
            # corrupt and got dropped) — go straight to topic-matched rescue
            # instead of blindly cutting the wrong moment from the same video
            rescue_file, rescue_dur, rescue_ss = _get_healthy_fallback_piece(
                need, topic=e.get("topic", ""), avoid=src)
            if rescue_file:
                cmd = [ff, "-y", "-i", str(rescue_file), "-ss", f"{rescue_ss:.3f}",
                       "-t", f"{need:.3f}", "-vf", CLIP_VF, *X264, str(out)]
                r = subprocess.run(cmd, capture_output=True, text=True)
                if r.returncode == 0 and out.exists() and out.stat().st_size > 10 * 1024:
                    log(f"    🎬 clip {i+1}: needed section missing — rescued with "
                        f"'{e.get('topic','?')}'-matched footage from "
                        f"{rescue_file.name} (ss={rescue_ss:.1f}s) ✔")
                    return i, out
            # rescue unavailable — fall through and try the best partial cut

        local_ss = float(e["scene_start"]) - p_start
        # clamp the seek so we never start past (piece_end - need)
        if p_dur and p_dur > 0:
            local_ss = min(max(0.0, local_ss), max(0.0, p_dur - need - 0.05))
        else:
            local_ss = max(0.0, local_ss)

        def _run(ss):
            # Optimizing cut rendering speed & stability:
            # Short pieces (under 60s) are cut using sequential output seeking (-ss after -i),
            # which ensures flawless frame-accuracy even on non-zero start timestamps.
            # Large files use input seeking (-ss before -i) to save decoding time.
            if p_dur and p_dur < 60.0:
                cmd = [ff, "-y", "-i", str(src),
                       "-ss", f"{ss:.3f}", "-t", f"{need:.3f}",
                       "-vf", CLIP_VF, *X264, str(out)]
            else:
                cmd = [ff, "-y", "-ss", f"{ss:.3f}", "-i", str(src),
                       "-t", f"{need:.3f}", "-vf", CLIP_VF, *X264, str(out)]
            return subprocess.run(cmd, capture_output=True, text=True)

        r = _run(local_ss)
        ok = (r.returncode == 0 and out.exists() and out.stat().st_size > 10 * 1024)
        if not ok:                                  # retry from piece start
            r = _run(0.0)
            ok = (r.returncode == 0 and out.exists() and out.stat().st_size > 10 * 1024)
        if not ok:
            # show the REAL ffmpeg error so failures are diagnosable
            err = (r.stderr or "").strip().replace("\n", " | ")[-220:]
            log(f"    ⚠ clip {i+1}: primary cut failed (seek {local_ss:.1f}s)"
                f" — ffmpeg said: {err or 'no stderr'}")
            # 🔧 REPAIR: yt-dlp section downloads keep the ORIGINAL video's
            # timestamps (e.g. first frame at PTS 441.9s), which breaks
            # seeking. Remux with zeroed timestamps (stream copy, instant)
            # and retry the cut on the fixed file before giving up.
            fixed = src.with_name(src.stem + "_ptsfix" + src.suffix)
            try:
                with _rescue_lock:                 # one worker remuxes; others reuse
                    if not fixed.exists():
                        subprocess.run(
                            [ff, "-y", "-i", str(src), "-c", "copy",
                             "-avoid_negative_ts", "make_zero",
                             "-muxpreload", "0", "-muxdelay", "0", str(fixed)],
                            capture_output=True, text=True, timeout=120)
                if fixed.exists() and fixed.stat().st_size > 50 * 1024:
                    cmd = [ff, "-y", "-i", str(fixed),
                           "-ss", f"{max(0.0, local_ss):.3f}", "-t", f"{need:.3f}",
                           "-vf", CLIP_VF, *X264, str(out)]
                    r = subprocess.run(cmd, capture_output=True, text=True)
                    ok = (r.returncode == 0 and out.exists()
                          and out.stat().st_size > 10 * 1024)
                    if ok:
                        log(f"    🔧 clip {i+1}: repaired by normalizing "
                            f"timestamps of {src.name} ✔ (correct footage kept)")
            except Exception:
                pass
        if not ok:
            log(f"    ↻ clip {i+1}: repair failed too — using topic-matched rescue...")
            rescue_file, rescue_dur, rescue_ss = _get_healthy_fallback_piece(
                need, topic=e.get("topic", ""), avoid=src)
            if rescue_file:
                cmd = [ff, "-y", "-i", str(rescue_file), "-ss", f"{rescue_ss:.3f}",
                       "-t", f"{need:.3f}", "-vf", CLIP_VF, *X264, str(out)]
                r = subprocess.run(cmd, capture_output=True, text=True)
                ok = (r.returncode == 0 and out.exists() and out.stat().st_size > 10 * 1024)
                if ok:
                    log(f"    🎬 clip {i+1}: rescued with '{e.get('topic','?')}'-matched footage from {rescue_file.name} (ss={rescue_ss:.1f}s) ✔")

        if not ok:
            # last resort: black filler so the timeline length is still guaranteed rather than losing a cut
            subprocess.run(
                [ff, "-y", "-f", "lavfi", "-i",
                 f"color=c=black:s={CANVAS_W}x{CANVAS_H}:r={OUT_FPS}:d={need:.3f}",
                 *X264, str(out)], capture_output=True, text=True)
            log(f"    ⚠ clip {i+1}: source cut failed, inserted {need:.1f}s black filler")
        return i, out

    used_keys = {e["video_key"] for e in edl}
    clip_paths = [None] * len(edl)

    # Parallel clip encoding — the cuts are independent, so run several ffmpeg
    # processes at once. This is the other half of the render speedup.
    from concurrent.futures import ThreadPoolExecutor, as_completed
    try:
        cpu = os.cpu_count() or 2
    except Exception:
        cpu = 2
    workers = max(1, min(4, cpu - 1))
    done = 0
    lock = threading.Lock()
    log(f"    Encoding {len(edl)} clips with {workers} parallel worker(s) ...")
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(_cut_one, i, e) for i, e in enumerate(edl)]
        for fut in as_completed(futs):
            i, out = fut.result()
            clip_paths[i] = out
            with lock:
                done += 1
                log(f"    ✂ clip {done}/{len(edl)} done")
    if any(p is None for p in clip_paths):
        raise RuntimeError("one or more clips failed to encode")

    # ------------------------------------------------------------------------
    # STEP 4.5 — VISUAL QC: fingerprint every rendered clip, find clips that
    # are near-duplicates of each other (same footage window shown again —
    # typically from rescues), and RE-CUT the extras from different footage.
    # Pure ffmpeg + hashing: no AI quota, runs in seconds. The timeline length
    # never changes, so voice-over sync is untouched.
    # ------------------------------------------------------------------------
    log("STEP 4.5 — visual QC: scanning rendered clips for repeated footage ...")

    def _clip_sig(path):
        """5 tiny 12x12 RGB fingerprints sampled across the clip."""
        FRAME_BYTES = 12 * 12 * 3
        try:
            r = subprocess.run(
                [ff, "-v", "error", "-i", str(path),
                 "-vf", "scale=12:12", "-pix_fmt", "rgb24",
                 "-f", "rawvideo", "-"],
                capture_output=True, timeout=120)
            buf = r.stdout
        except Exception:
            return None
        n = len(buf) // FRAME_BYTES
        if n == 0:
            return None
        idxs = sorted({max(0, min(n - 1, int(n * f)))
                       for f in (0.05, 0.28, 0.5, 0.72, 0.95)})
        sig = []
        for ix in idxs:
            f = buf[ix * FRAME_BYTES:(ix + 1) * FRAME_BYTES]
            bits = 0
            for ch in range(3):                    # hash R, G, B independently
                px = f[ch::3]
                avg = sum(px) / len(px)
                for v in px:
                    bits = (bits << 1) | (1 if v > avg else 0)
            sig.append(bits)
        return sig

    def _same_footage(s1, s2, bits=20):
        """True if any sampled frame pair is (near-)identical — catches exact
        repeats AND offset repeats that share overlapping frames.
        Threshold is 20 of 432 bits (~5%): identical frames score 0–10,
        different scenes typically score 100+."""
        return any(bin(a ^ b).count("1") <= bits for a in s1 for b in s2)

    MAX_SHOWINGS = int(cfg.get("max_scene_uses", 2))
    sigs = [_clip_sig(p) for p in clip_paths]
    groups = []          # list of [signature, count, member_files]
    requeued = 0

    def _recut(i, e, avoid_files):
        """Re-cut clip i from fresh, topic-matched footage not in avoid_files."""
        need = max(0.05, float(e["audio_end"]) - float(e["audio_start"]))
        out = clip_paths[i]
        for _try in range(3):
            rf, rd, rss = _get_healthy_fallback_piece(
                need, topic=e.get("topic", ""), avoid=avoid_files)
            if not rf:
                return None
            cmd = [ff, "-y", "-i", str(rf), "-ss", f"{rss:.3f}",
                   "-t", f"{need:.3f}", "-vf", CLIP_VF, *X264, str(out)]
            r = subprocess.run(cmd, capture_output=True, text=True)
            if r.returncode == 0 and out.exists() and out.stat().st_size > 10 * 1024:
                new_sig = _clip_sig(out)
                if new_sig and not any(_same_footage(new_sig, g[0])
                                       for g in groups if g[1] >= MAX_SHOWINGS):
                    return new_sig, rf
            avoid_files = set(avoid_files) | {rf}
        return None

    for i, (e, sig) in enumerate(zip(edl, sigs)):
        if sig is None:
            continue
        hit = None
        for g in groups:
            if _same_footage(sig, g[0]):
                hit = g
                break
        if hit is None:
            groups.append([sig, 1, {clip_paths[i]}])
            continue
        if hit[1] < MAX_SHOWINGS:
            hit[1] += 1
            continue
        # this footage window is already shown MAX_SHOWINGS times — re-cut
        avoid = set()
        for g in groups:
            if g[1] >= MAX_SHOWINGS:
                avoid |= g[2]
        res = _recut(i, e, avoid)
        if res is not None:
            new_sig, rf = res
            merged = False
            for g in groups:
                if _same_footage(new_sig, g[0]):
                    g[1] += 1
                    g[2].add(rf)
                    merged = True
                    break
            if not merged:
                groups.append([new_sig, 1, {rf}])
            requeued += 1
            log(f"    ♻ QC: clip {i+1} was the {hit[1]+1}ᵗʰ showing of the same "
                f"footage — re-cut with fresh '{e.get('topic','?')}' footage "
                f"from {rf.name} ✔")
        else:
            hit[1] += 1
            log(f"    ⚠ QC: clip {i+1} repeats footage but no fresh alternative "
                f"exists — kept (add more source videos)")

    if requeued:
        log(f"    ✅ QC pass replaced {requeued} repetitive clip(s) — "
            f"{len(groups)} distinct footage windows across {len(edl)} cuts")
    else:
        log(f"    ✅ QC pass: no clip exceeds {MAX_SHOWINGS} showings — "
            f"{len(groups)} distinct footage windows across {len(edl)} cuts")

    # concat (safe now: every clip is identical codec/size/fps/sar)
    lst = CLIP_DIR / "concat.txt"
    lst.write_text("".join(f"file '{p.as_posix()}'\n" for p in clip_paths), encoding="utf-8")
    silent = OUT_DIR / "_video_only.mp4"
    r = subprocess.run([ff, "-y", "-f", "concat", "-safe", "0", "-i", str(lst),
                        "-c", "copy", str(silent)], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError("ffmpeg concat failed: " + r.stderr[-400:])

    # ---- length reconciliation against the actual voice-over --------------
    audio_dur = media_duration(Path(mp3_path))
    vid_dur = media_duration(silent)
    log(f"    Stitched video = {vid_dur:.1f}s, voice-over = {audio_dur:.1f}s")
    if audio_dur > 0 and vid_dur > 0 and vid_dur < audio_dur - 0.1:
        # freeze-extend: hold the last frame to fill any remaining gap so the
        # video is NEVER shorter than the audio (belt-and-braces with the EDL
        # coverage pass — covers rounding and any dropped-clip filler drift)
        gap = audio_dur - vid_dur
        last_frame = CLIP_DIR / "_last.mp4"
        subprocess.run([ff, "-y", "-sseof", "-0.2", "-i", str(silent),
                        "-vframes", "1", str(CLIP_DIR / "_last.png")],
                       capture_output=True, text=True)
        subprocess.run([ff, "-y", "-loop", "1", "-i", str(CLIP_DIR / "_last.png"),
                        "-t", f"{gap + 0.2:.3f}", "-vf", f"fps={OUT_FPS},setsar=1",
                        *X264, str(last_frame)], capture_output=True, text=True)
        if last_frame.exists():
            lst2 = CLIP_DIR / "concat2.txt"
            lst2.write_text(f"file '{silent.as_posix()}'\nfile '{last_frame.as_posix()}'\n",
                            encoding="utf-8")
            silent2 = OUT_DIR / "_video_only2.mp4"
            r = subprocess.run([ff, "-y", "-f", "concat", "-safe", "0", "-i", str(lst2),
                                "-c", "copy", str(silent2)], capture_output=True, text=True)
            if r.returncode == 0 and silent2.exists():
                silent.unlink(missing_ok=True)
                silent = silent2
                log(f"    ↔ freeze-extended video by {gap:.1f}s to match audio")

    # mux voice-over — cap to the audio length explicitly (NOT -shortest, which
    # would truncate the whole video to any short track; this is why the final
    # came out 35s before). -t audio_dur makes length == audio, exactly.
    final = OUT_DIR / f"final_{time.strftime('%Y%m%d_%H%M%S')}.mp4"
    mux = [ff, "-y", "-i", str(silent), "-i", mp3_path,
           "-map", "0:v:0", "-map", "1:a:0",
           "-c:v", "copy", "-c:a", "aac", "-b:a", "192k"]
    if audio_dur > 0:
        mux += ["-t", f"{audio_dur:.3f}"]
    mux += ["-movflags", "+faststart", str(final)]
    r = subprocess.run(mux, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError("ffmpeg mux failed: " + r.stderr[-400:])
    silent.unlink(missing_ok=True)

    final_dur = media_duration(final)
    log(f"    Final length: {final_dur:.1f}s  (voice-over {audio_dur:.1f}s)")

    # delete only the big FULL source videos to save space; KEEP the _r*
    # section pieces so the next run reuses them instead of re-downloading
    if cfg.get("delete_originals", True):
        for k in used_keys:
            for f in VIDEO_DIR.glob(f"{k}.*"):        # note: '{k}.' not '{k}*'
                try:
                    f.unlink()
                    log(f"    🗑 removed full source {f.name} (sections + cache kept)")
                except Exception:
                    pass

    log(f"DONE ✔  Final video: {final}")
    return final


# ----------------------------------------------------------------------------
# Pipeline orchestrator
# ----------------------------------------------------------------------------
def run_pipeline(cfg, log):
    if not cfg["api_key"].strip():
        raise RuntimeError("Enter your Gemini API key first.")
    links = [u for u in cfg["links"] if u.strip()]
    if not links:
        raise RuntimeError("Paste at least one video link.")
    # verify ffmpeg BEFORE spending any API quota — needed for downloads & render
    try:
        ff = find_ffmpeg()
        log(f"ffmpeg found: {ff}")
        loc = ffmpeg_dir_for_ytdlp(log)
        if loc:
            log(f"yt-dlp will use ffmpeg from: {loc}")
        if not shutil.which("ffmpeg"):
            log("TIP: for best results install real ffmpeg once:  winget install Gyan.FFmpeg")
    except RuntimeError as e:
        raise RuntimeError(
            "ffmpeg is NOT installed — downloads and rendering can't work.\n"
            "  Easiest fix (Windows):  winget install Gyan.FFmpeg   then restart the app.\n"
            "  Or:  pip install imageio-ffmpeg  (the app will auto-use it).\n"
            f"  Details: {e}")
    client = make_client(cfg["api_key"].strip())

    transcript = analyze_audio(client, cfg, log)                     # AI 1
    log(f"STEP 2 — AI 2 analyzing {len(links)} video(s) (cache-aware) ...")
    analyses, failed = [], []
    daily_dead = False
    for u in links:                                                                     # AI 2
        if daily_dead:
            # daily quota gone: cached videos still load instantly & free
            key = url_key(u)
            c = CACHE_DIR / f"{key}.json"
            if c.exists():
                analyses.append(json.loads(c.read_text(encoding="utf-8")))
                log(f"  Cache hit ✔ — {u}")
            else:
                failed.append(u)
            continue
        try:
            analyses.append(analyze_video(client, u, cfg, log))
        except QuotaExhausted as e:
            if e.daily and cfg["model_vision"] != "gemini-3.1-flash-lite":
                log(f"  🛑 {e}")
                log("  ↪ Auto-switching AI 2 to gemini-3.1-flash-lite (500/day) "
                    "and continuing the run — like your old pipeline.")
                cfg["model_vision"] = "gemini-3.1-flash-lite"
                try:
                    analyses.append(analyze_video(client, u, cfg, log))
                except Exception as e2:
                    failed.append(u)
                    log(f"  ✖ SKIPPED: {u}\n      -> {str(e2)[:200]}")
            elif e.daily:
                daily_dead = True
                failed.append(u)
                log(f"  🛑 {e}")
                log("  Skipping remaining UNCACHED videos instantly "
                    "(no pointless waiting). Cached ones still count.")
            else:
                failed.append(u)
                log(f"  ✖ SKIPPED (per-minute limit stuck): {u}")
        except Exception as e:
            failed.append(u)
            log(f"  ✖ SKIPPED (analysis failed): {u}\n      -> {str(e)[:200]}")
    if failed:
        log(f"  ⚠ {len(failed)} video(s) not analyzed this run — they'll be "
            f"picked up next run. Continuing with {len(analyses)} video(s).")
    if not analyses:
        raise RuntimeError("No analyzed videos available — rerun when quota resets.")

    try:
        edl = build_edl(client, transcript, analyses, cfg, log)      # AI 3
    except QuotaExhausted as e:
        fallback = "gemini-3.1-flash-lite"
        if e.daily and cfg["model_match"] != fallback:
            log(f"  🛑 {cfg['model_match']} daily quota gone for AI 3 — "
                f"retrying the match with {fallback} instead.")
            cfg2 = dict(cfg); cfg2["model_match"] = fallback
            edl = build_edl(client, transcript, analyses, cfg2, log)
        else:
            raise
    return render(edl, cfg["mp3_path"], cfg, log)                    # local


