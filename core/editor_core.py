"""AUTO-PORTED from arm_video_editor.py - GUI stripped, paths repointed to the Modal volume.
Everything below is your original logic, unchanged, except: the tkinter imports
were removed and the on-disk directory constants now point at the cloud volume.
"""
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ARM WRESTLING FX EDITOR v2 — AI-Directed Cinematic Effects
===========================================================
Pipeline:
  1. Load a local MP4 in the GUI.
  2. Gemini watches it like a broadcast editor and returns a DENSE Effect
     Decision List (strict JSON timestamps). Cached per file hash (v2 cache —
     old v1 analyses are re-done because the effect vocabulary grew).
  3. MediaPipe extracts FULL 21-landmark hand skeletons + per-frame motion
     energy (cached). Zooms lock onto the grip AND a cyberpunk neon skeleton
     overlay is rendered on the hands (AI-decided spans / always-on / off).
  4. Local auto-suggest (optional) adds extra effects from the motion data:
     impact combos on motion spikes, neon hand spans where tracking is solid.
  5. Single-pass OpenCV -> ffmpeg renderer.
     VOICE-OVER SAFE MODE (default ON): the audio track is NEVER warped.
     Slow-motion / freeze become professional speed ramps — the video slows
     while the narration keeps playing, then briefly fast-forwards (▶▶) to
     catch back up. Net timeline change is zero, audio is muxed untouched.

Effects: zoom_in, zoom_out, zoom_punch, slow_motion, speed_up, impact_flash,
camera_shake, freeze_frame, dramatic_grade, whip_transition, label, title,
hand_glow (neon skeleton), rgb_split, glitch, letterbox.

Requirements:
    pip install google-genai opencv-python mediapipe numpy pydantic
    ffmpeg on PATH (https://ffmpeg.org)   (fallback: pip install imageio-ffmpeg)

Settings persist in ./fx_app_data/config.json.
"""

import os
import re
import sys
import json
import time
import math
import queue
import shutil
import hashlib
import threading
import traceback
import subprocess
from pathlib import Path

import numpy as np

try:
    import cv2
except ImportError:
    print("Missing OpenCV. Run:  pip install opencv-python")
    sys.exit(1)

# ----------------------------------------------------------------------------
# Paths / persistence
# ----------------------------------------------------------------------------
DATA        = Path(os.environ.get("ARM_DATA", "/data"))
SCRATCH     = Path(os.environ.get("ARM_SCRATCH", "/tmp/arm_scratch"))
APP_DIR     = DATA / "editor" / "fx_app_data"
CONFIG_FILE = APP_DIR / "config.json"
CACHE_DIR   = APP_DIR / "analysis_cache"
TRACK_DIR   = APP_DIR / "track_cache"
TMP_DIR     = SCRATCH / "tmp"
OUT_DIR     = DATA / "editor" / "fx_output"

for d in (APP_DIR, CACHE_DIR, TRACK_DIR, TMP_DIR, OUT_DIR):
    d.mkdir(parents=True, exist_ok=True)

DEFAULT_CONFIG = {
    "api_key": "",
    "model_vision": "gemini-3.5-flash",
    "last_video": "",
    "hand_fx_mode": "AI-decided",     # AI-decided | Always on | Off
    "voice_safe": True,               # NEVER warp the audio track
    "auto_suggest": True,             # local motion-based extra effects
    "film_grain": True,               # subtle organic grain over everything
    "draw_hud": True,
    "draw_labels": True,
    "cap_1080p": True,
    "low_res_over_minutes": 20,
}

MODELS = ["gemini-3.5-flash", "gemini-3.1-flash-lite", "gemini-3-flash",
          "gemini-2.5-flash", "gemini-2.5-flash-lite"]


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
# ffmpeg discovery + hardware encoder autodetect
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
            "ffmpeg not found. Install it from https://ffmpeg.org and add to "
            "PATH, or `pip install imageio-ffmpeg`.")


_ENCODER_CACHE = None


def pick_encoder(ff: str, log) -> list:
    """TEST each hardware encoder with a tiny encode — being listed in
    `-encoders` does not mean the GPU is actually present."""
    global _ENCODER_CACHE
    if _ENCODER_CACHE is not None:
        return _ENCODER_CACHE
    candidates = [
        ("h264_nvenc",        ["-c:v", "h264_nvenc", "-preset", "p4",
                               "-rc", "vbr", "-cq", "21", "-b:v", "0"]),
        ("h264_qsv",          ["-c:v", "h264_qsv", "-global_quality", "22"]),
        ("h264_videotoolbox", ["-c:v", "h264_videotoolbox", "-q:v", "60"]),
    ]
    for name, args in candidates:
        try:
            r = subprocess.run(
                [ff, "-hide_banner", "-loglevel", "error",
                 "-f", "lavfi", "-i", "color=black:s=256x256:d=0.2:r=30",
                 *args, "-f", "null", "-"],
                capture_output=True, text=True, timeout=30)
            if r.returncode == 0:
                log(f"    ⚡ hardware encoder detected: {name}")
                _ENCODER_CACHE = args
                return args
        except Exception:
            pass
    log("    encoder: libx264 veryfast (no usable GPU encoder found)")
    _ENCODER_CACHE = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20"]
    return _ENCODER_CACHE


def has_audio_stream(ff: str, path: Path) -> bool:
    try:
        r = subprocess.run([ff, "-hide_banner", "-i", str(path)],
                           capture_output=True, text=True, timeout=60)
        return "Audio:" in (r.stderr or "")
    except Exception:
        return False


def media_duration(ff: str, path: Path) -> float:
    fp = shutil.which("ffprobe")
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
        r = subprocess.run([ff, "-hide_banner", "-i", str(path)],
                           capture_output=True, text=True, timeout=60)
        m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", r.stderr or "")
        if m:
            return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    except Exception:
        pass
    return 0.0


def file_md5(path: Path) -> str:
    h = hashlib.md5()
    with open(path, "rb") as fp:
        for chunk in iter(lambda: fp.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


# ----------------------------------------------------------------------------
# Arm wrestling expertise + FX director prompt
# ----------------------------------------------------------------------------
ARM_WRESTLING_EXPERTISE = """
You are a world-class arm wrestling analyst AND a professional sports video
editor (think official WAF / East vs West / King of the Table broadcast edits
mixed with modern hype-reel YouTube editing). You recognize on sight: toproll
(climbing fingers, wrist rolled back over the top), hook (supinated inside
battle), press (triceps/shoulder rotation behind the arm), king's move
(athlete drops low under the table line, arm nearly straight), cupping,
pronation, supination, rising, drag, side/back pressure, posting, flop wrist,
grip setup battles, referee's grip, strap matches, slips, elbow fouls, flash
pins, pins, celebrations, table elements (elbow pads, pin pads, pegs, straps),
training footage, and elite-level athletes — though you NEVER output any
person's name (see the NAMES rule below).
""".strip()

FX_SYSTEM = ARM_WRESTLING_EXPERTISE + """

TASK: Watch this ENTIRE arm wrestling video and direct a DENSE, modern,
hype-reel edit. Decide exactly where a local rendering engine applies effects.
Return STRICT JSON ONLY, no markdown:

{
  "duration_sec": <float total video length>,
  "effects": [
    {
      "start": <float seconds from video start>,
      "end": <float seconds (end > start)>,
      "effect": "<one of: zoom_in | zoom_out | zoom_punch | slow_motion |
                 speed_up | impact_flash | camera_shake | freeze_frame |
                 dramatic_grade | action_grade | whip_transition | zoom_blur |
                 dip_to_black | cross_dissolve | match_cut | jump_cut |
                 frame_block | light_leak | film_grain | split_screen |
                 kinetic_text | label | title | hand_text | hand_glow |
                 rgb_split | glitch | letterbox>",
      "intensity": <1-10>,
      "text": "<ONLY for label/title/hand_text/freeze_frame: short uppercase
               caption. Else ''>",
      "direction": "<ONLY for whip_transition: left | right | up | down |
                    auto. Else ''>",
      "reason": "<one short sentence: what happens here and why this effect>"
    }
  ]
}

EFFECT MEANINGS (zooms, hand_glow and hand_text auto-lock onto the tracked
hands via MediaPipe):
- zoom_in        : slow cinematic push-in, 2-5s. Grip setups, referee aligning
                   hands, king's move sinking, long stalemates.
- zoom_out       : pull-back reveal, 1.5-4s. After a pin, revealing the venue.
- zoom_punch     : fast snap zoom 0.4-0.9s at "GO!", counters, flash pins.
- slow_motion    : the engine renders a SPEED RAMP (slow then auto catch-up,
                   narration untouched). Use on THE decisive beats: pin,
                   brutal press, slip, comeback. 1-4s. Leave >=6s of normal
                   footage after it for the catch-up ramp.
- speed_up       : timelapse of long grip negotiations / strap wrapping /
                   breaks, 4-30s.
- impact_flash   : white flash 0.1-0.3s at "GO!", pin touch, slips.
- camera_shake   : 0.4-1.2s at explosive starts / violent hits.
- freeze_frame   : hold ~1-2s (end-start = hold) at a peak pose, WITH 'text'.
                   Leave >=5s of normal footage after it.
- dramatic_grade : CINEMATIC FILM look — contrast + vignette. Pair with
                   letterbox for tension, setups, stare-downs (3-15s).
- action_grade   : HOLLYWOOD ACTION look — punchy warm orange-teal contrast
                   + sharpening. Use on explosive exchanges, hits, flurries
                   (2-10s). Alternate looks scene by scene; never both at once.
- whip_transition: 0.3-0.6s directional camera whip (push + motion blur) that
                   masks scene/camera changes. SET 'direction': at a pin, the
                   whip must follow the pin — the side the losing hand was
                   driven toward from the VIEWER's perspective (or 'auto' and
                   the engine reads it from the hand tracking). Use at EVERY
                   scene change after a pin/win.
- zoom_blur      : 0.3-0.6s crash-zoom blur burst — hype transition into
                   replays, round changes, big reveals.
- dip_to_black   : 0.5-1.2s fade through black — chapter changes, before the
                   final match, after a victory celebration.
- cross_dissolve : 0.5-2.0s soft morph between moments. The narration flows
                   uncut underneath, so this doubles as a J-cut/L-cut feel —
                   sound bridging the visual change. Place ON scene changes.
- match_cut      : 0.5-1.5s morph where the outgoing frame zooms into the
                   incoming one — use when two shots share a shape/motion
                   (arm to arm, grip to grip). Place ON the cut.
- jump_cut       : 0.8-3.0s stutter (frames held in bursts) — urgency,
                   time-compression feel on repetitive action. Sparingly.
- frame_block    : 0.4-0.8s pass-by wipe, as if something crosses the lens —
                   masks a scene change with a sweeping blurred band.
- light_leak     : 0.6-2.0s warm organic flare washing across the frame —
                   dreamy transition into replays, celebrations, intros.
- film_grain     : organic monochromatic noise over a stretch (5-60s) for a
                   filmic, tactile texture. Great over cinematic sections.
- split_screen   : 2-8s multi-cam feel: wide shot + magnified GRIP CAM
                   side-by-side (the grip panel auto-tracks the hands). Use
                   when the grip detail matters during a wide shot.
- kinetic_text   : 1.5-4s word-by-word animated caption whose pops follow the
                   VOICE — words land on the narration's cadence. MUST have
                   'text' (3-6 punchy words, e.g. 'THE GRIP IS EVERYTHING').
                   This is a hook weapon: use it in the first 5s and on big
                   statements.
- label          : broadcast lower-third naming technique/athlete, 2-4s.
- title          : BIG lower-third glowing title 1.5-3s for major moments:
                   'ROUND 2', 'THE KING'S MOVE', athlete names, 'FINAL'.
- hand_text      : small professional tag that FOLLOWS THE HANDS on screen,
                   1.5-4s, MUST have 'text'. This is your retention weapon —
                   the caption must have a smart PURPOSE: anticipation
                   ('WATCH THE WRIST'), stakes ('MATCH POINT'), analysis
                   ('GRIP SLIPPING'), identity ('LEVAN'S HOOK'). Max ~1 per
                   scene, only where the hands ARE the story.
- hand_glow      : neon skeleton overlay tracked onto the hands. Use
                   SELECTIVELY on 3-6 standout moments only (grip close-up,
                   technique demo, inside battle), 3-6s each. Do NOT cover
                   the video with it — rarity keeps it special.
- rgb_split      : chromatic-aberration punch 0.3-0.8s on hits and beat drops.
- glitch         : digital glitch burst 0.3-0.8s for hype transitions and
                   replays of brutal moments.
- letterbox      : cinema bars over a whole dramatic section (intro, final
                   battle, slow-motion sequences), 5-40s.

RETENTION RULES — this edit must HOOK the viewer and hold them:
- THE HOOK: the first 5 seconds get the strongest stack of the whole video —
  3-5 effects (title + zoom + a grade + letterbox + one more). If the video
  opens weak, still make the edit open strong.
- NAMES: NEVER put any person's name in ANY caption (label, title,
  hand_text, kinetic_text, freeze_frame). Use roles, techniques and stakes
  instead: 'THE CHAMPION', 'THE CHALLENGER', 'RIGHT-HAND WAR', 'TOPROLL',
  'MATCH POINT'. No exceptions, even if you are confident of the identity.
- EVERY scene gets at least one effect — no naked scenes — but placement must
  feel NATURAL and motivated by what happens on screen, never random.
- PATTERN INTERRUPTS: something must change every 15-25 seconds (a caption,
  a transition, a look change) so attention never settles.
- VARIETY: never repeat the same effect combo twice in a row. Alternate the
  cinematic look and the action look between scenes.
- TEXT WITH PURPOSE: every caption (label/title/hand_text/freeze) must earn
  its place — create anticipation, stakes, identity or payoff. No filler.
- Stack 2-4 effects at key moments ("GO!" = zoom_punch + impact_flash +
  camera_shake + rgb_split; a pin = slow_motion + action_grade + title,
  then whip_transition or match_cut out of it; a celebration = light_leak +
  film_grain + kinetic_text). BE DENSE: one effect every 3-6 seconds,
  50-90 effects for a typical 3-8 minute video.
- Still leave >=25% of the timeline untouched — density at the right
  moments, not constant noise.
- Time-warp effects (slow_motion, speed_up, freeze_frame) must NEVER overlap
  each other and need >=6 seconds of gap between them.
- Timestamps accurate to ~0.3s. All times within the video duration.
- Skip ads, static logo slides, unusable footage.
"""

# ---- Pydantic schema: force VALID structured JSON ---------------------------
try:
    from pydantic import BaseModel

    class FxEventSchema(BaseModel):
        start: float
        end: float
        effect: str
        intensity: int
        text: str = ""
        direction: str = ""
        reason: str = ""

    class FxListSchema(BaseModel):
        duration_sec: float
        effects: list[FxEventSchema]

    FX_SCHEMA = FxListSchema
except ImportError:
    FX_SCHEMA = None


# ----------------------------------------------------------------------------
# Gemini helpers (quota-aware, same pattern as the 3-AI pipeline)
# ----------------------------------------------------------------------------
class QuotaExhausted(Exception):
    def __init__(self, msg, daily=False):
        super().__init__(msg)
        self.daily = daily


_LAST_CALL = {}
_MODEL_INTERVAL = {
    "gemini-3.5-flash": 13.0,
    "gemini-3-flash": 13.0,
    "gemini-2.5-flash": 21.0,
    "gemini-2.5-flash-lite": 60.0,
    "gemini-3.1-flash-lite": 4.5,
}


def make_client(api_key: str):
    try:
        from google import genai
    except ImportError:
        raise RuntimeError("Missing SDK. Run:  pip install google-genai")
    return genai.Client(api_key=api_key)


def _throttle(model, log):
    interval = _MODEL_INTERVAL.get(model, 13.0)
    wait = interval - (time.time() - _LAST_CALL.get(model, 0.0))
    if wait > 0.5:
        log(f"    (pacing {wait:.0f}s to respect the {model} per-minute limit)")
        time.sleep(max(wait, 0))
    _LAST_CALL[model] = time.time()


def _parse_retry_delay(msg):
    m = re.search(r"retry.?delay[\"']?\s*[:=]\s*[\"']?(\d+)", msg, re.I)
    return float(m.group(1)) if m else None


def _is_daily_quota(msg):
    m = msg.lower()
    return ("perday" in m.replace("_", "").replace(" ", "")
            or "per day" in m or "daily" in m or "requests per day" in m)


def gen_json(client, model, contents, system_instruction, log,
             max_retries=4, media_resolution=None):
    from google.genai import types
    kw = dict(response_mime_type="application/json",
              system_instruction=system_instruction, temperature=0.15)
    if FX_SCHEMA:
        kw["response_schema"] = FX_SCHEMA
    if media_resolution:
        kw["media_resolution"] = media_resolution
    config = types.GenerateContentConfig(**kw)

    for attempt in range(1, max_retries + 1):
        _throttle(model, log)
        try:
            resp = client.models.generate_content(
                model=model, contents=contents, config=config)
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
                    raise QuotaExhausted(
                        f"DAILY quota for {model} exhausted. All analysis is "
                        f"cached — rerun tomorrow for free.", daily=True)
                wait = _parse_retry_delay(msg) or 20.0
                if attempt == max_retries:
                    raise QuotaExhausted(
                        f"Per-minute limit on {model} would not clear.", False)
                log(f"    ! Per-minute limit — waiting {wait:.0f}s "
                    f"(attempt {attempt}/{max_retries})")
                time.sleep(wait + 1)
            elif "INVALID_ARGUMENT" in msg or "'code': 400" in msg:
                raise
            else:
                log(f"    ! API error (attempt {attempt}): {msg[:300]}")
                if attempt == max_retries:
                    raise
                time.sleep(8)
    raise RuntimeError("Model call failed after retries.")


def upload_and_wait(client, path: Path, log, timeout=1800):
    log(f"    Uploading {path.name} ({path.stat().st_size/1e6:.1f} MB) to Gemini ...")
    f = client.files.upload(file=str(path))
    t0 = time.time()
    while getattr(f.state, "name", str(f.state)) == "PROCESSING":
        if time.time() - t0 > timeout:
            raise RuntimeError("File processing timed out on Gemini side.")
        time.sleep(6)
        f = client.files.get(name=f.name)
    if getattr(f.state, "name", str(f.state)) != "ACTIVE":
        raise RuntimeError(f"Gemini file state = {f.state}")
    log("    Upload ready.")
    return f


def delete_remote(client, f):
    try:
        client.files.delete(name=f.name)
    except Exception:
        pass


# ----------------------------------------------------------------------------
# STEP 1 — Gemini analysis (cached; v2 cache since the vocabulary changed)
# ----------------------------------------------------------------------------
TIMEWARP = {"slow_motion", "speed_up", "freeze_frame"}
ALL_FX = {"zoom_in", "zoom_out", "zoom_punch", "slow_motion", "speed_up",
          "impact_flash", "camera_shake", "freeze_frame", "dramatic_grade",
          "action_grade", "whip_transition", "zoom_blur", "dip_to_black",
          "cross_dissolve", "match_cut", "jump_cut", "frame_block",
          "light_leak", "film_grain", "split_screen", "kinetic_text",
          "label", "title", "hand_text", "hand_glow", "rgb_split",
          "glitch", "letterbox"}
DIRECTIONS = {"left", "right", "up", "down", "auto", ""}
WARP_GAP = 6.0     # required source-time gap between time-warps (catch-up room)
GLOW_MAX_SPAN = 6.0    # hand_glow: keep it special, not spammed
GLOW_MIN_GAP = 8.0
GLOW_MAX_COUNT = 8
HANDTEXT_MAX_COUNT = 10
# duration clamps + per-video count caps for the new effects
FX_CLAMP = {  # effect: (min_dur, max_dur, max_count)
    "cross_dissolve": (0.4, 2.0, 12),
    "match_cut":      (0.4, 1.6, 8),
    "jump_cut":       (0.6, 3.0, 6),
    "frame_block":    (0.3, 0.9, 10),
    "light_leak":     (0.5, 2.5, 10),
    "split_screen":   (2.0, 8.0, 6),
    "kinetic_text":   (1.2, 5.0, 6),
}


def analysis_cache_path(video: Path) -> Path:
    return CACHE_DIR / f"{file_md5(video)}_v5.json"


def analyze_video(cfg, video: Path, log, force=False) -> dict:
    cache = analysis_cache_path(video)
    if cache.exists() and not force:
        log("STEP 1 — effect EDL found in cache, skipping Gemini ✔")
        log("    (cache is keyed to this exact file's fingerprint — a new or "
            "edited video always gets a fresh analysis)")
        return json.loads(cache.read_text(encoding="utf-8"))

    if not cfg.get("api_key"):
        raise RuntimeError("Enter your Gemini API key first.")
    client = make_client(cfg["api_key"])
    ff = find_ffmpeg()
    dur = media_duration(ff, video)
    media_res = None
    if dur and dur > cfg["low_res_over_minutes"] * 60:
        media_res = "MEDIA_RESOLUTION_LOW"
        log(f"    long video ({dur/60:.1f} min) — analyzing at LOW media "
            f"resolution to save tokens")

    log(f"STEP 1 — Gemini ({cfg['model_vision']}) directing the edit ...")
    f = upload_and_wait(client, video, log)
    try:
        data = gen_json(
            client, cfg["model_vision"],
            contents=[f, f"This arm wrestling video is {dur:.1f} seconds "
                         f"long. Produce a DENSE effect decision list per the "
                         f"schema — remember the 4-8 second pacing target and "
                         f"stacked combos at key moments."],
            system_instruction=FX_SYSTEM, log=log, media_resolution=media_res)
    finally:
        delete_remote(client, f)

    data = validate_effects(data, dur, log)
    cache.write_text(json.dumps(data, indent=2), encoding="utf-8")
    per_min = len(data["effects"]) / max(dur / 60.0, 1e-6)
    log(f"    ✔ {len(data['effects'])} effects planned "
        f"({per_min:.1f}/min, cached: {cache.name})")
    if per_min < 9.0:
        log(f"    ⚠ sparse plan — 'lite' models direct thin edits and guess "
            f"names. Use gemini-3.5-flash for analysis. The local "
            f"auto-enhance pass will still fill hook/scenes/transitions at "
            f"render time.")
    return data


def validate_effects(data: dict, dur: float, log) -> dict:
    """Clamp/clean the plan so the renderer can never crash on it."""
    out, warps = [], []
    events = sorted(data.get("effects", []),
                    key=lambda e: float(e.get("start", 0) or 0))
    for e in events:
        try:
            fx = str(e.get("effect", "")).strip().lower()
            a = float(e.get("start", 0)); b = float(e.get("end", 0))
        except Exception:
            continue
        if fx not in ALL_FX:
            continue
        if dur:
            a = max(0.0, min(a, dur - 0.05)); b = max(0.0, min(b, dur))
        if b <= a + 0.05:
            continue
        d = str(e.get("direction", "") or "").strip().lower()
        ev = {"start": round(a, 3), "end": round(b, 3), "effect": fx,
              "intensity": int(max(1, min(10, int(e.get("intensity", 5) or 5)))),
              "text": str(e.get("text", "") or "")[:40].strip(),
              "direction": d if d in DIRECTIONS else "auto",
              "reason": str(e.get("reason", "") or "")[:200]}
        if fx == "hand_text":
            if not ev["text"]:
                continue                      # pointless without a caption
            ev["end"] = min(ev["end"], ev["start"] + 4.0)
            if ev["end"] - ev["start"] < 1.0:
                ev["end"] = ev["start"] + 1.5
        if fx == "kinetic_text" and not ev["text"]:
            continue
        if fx in FX_CLAMP:
            lo, hi, _ = FX_CLAMP[fx]
            d0 = ev["end"] - ev["start"]
            if d0 < lo:
                ev["end"] = round(min(ev["start"] + lo, dur or 1e9), 3)
            elif d0 > hi:
                ev["end"] = round(ev["start"] + hi, 3)
        if fx in TIMEWARP:
            if any(not (b + WARP_GAP <= wa or a >= wb + WARP_GAP)
                   for wa, wb in warps):
                log(f"    ⚠ dropped time-warp {fx} @ {a:.1f}s "
                    f"(too close to another time-warp)")
                continue
            if fx == "slow_motion":
                ev["end"] = min(ev["end"], ev["start"] + 4.5)
            if fx == "freeze_frame":
                ev["end"] = min(ev["end"], ev["start"] + 2.2)
            warps.append((ev["start"], ev["end"]))
        out.append(ev)
    out.sort(key=lambda e: e["start"])

    # ---- anti-spam pass: hand_glow must stay special ----
    kept, last_glow_end, n_glow, n_htext = [], -1e9, 0, 0
    fx_counts = {}
    for e in out:
        if e["effect"] in FX_CLAMP:
            cap = FX_CLAMP[e["effect"]][2]
            fx_counts[e["effect"]] = fx_counts.get(e["effect"], 0) + 1
            if fx_counts[e["effect"]] > cap:
                continue
        if e["effect"] == "hand_glow":
            e["end"] = min(e["end"], e["start"] + GLOW_MAX_SPAN)
            if (e["start"] - last_glow_end < GLOW_MIN_GAP
                    or n_glow >= GLOW_MAX_COUNT):
                log(f"    ✂ trimmed hand_glow @ {e['start']:.1f}s "
                    f"(keeping the neon rare)")
                continue
            last_glow_end = e["end"]
            n_glow += 1
        elif e["effect"] == "hand_text":
            if n_htext >= HANDTEXT_MAX_COUNT:
                continue
            n_htext += 1
        kept.append(e)
    data["effects"] = kept
    return data


# ----------------------------------------------------------------------------
# STEP 2 — MediaPipe: full hand skeletons + motion energy (cached, v2)
# ----------------------------------------------------------------------------
MAX_HANDS = 4
HAND_CONNECTIONS = [
    (0, 1), (1, 2), (2, 3), (3, 4),
    (0, 5), (5, 6), (6, 7), (7, 8),
    (5, 9), (9, 10), (10, 11), (11, 12),
    (9, 13), (13, 14), (14, 15), (15, 16),
    (13, 17), (17, 18), (18, 19), (19, 20),
    (0, 17),
]


def track_cache_path(video: Path) -> Path:
    return TRACK_DIR / f"{file_md5(video)}_v3.npz"


def audio_envelope(video: Path, n_frames: int, fps: float) -> np.ndarray:
    """Per-video-frame voice loudness 0..1 (RMS of mono 8kHz PCM). Drives
    kinetic typography so word pops land on the narration's cadence."""
    env = np.zeros(max(n_frames, 1), dtype=np.float32)
    try:
        ff = find_ffmpeg()
        r = subprocess.run(
            [ff, "-hide_banner", "-loglevel", "error", "-i", str(video),
             "-vn", "-ac", "1", "-ar", "8000", "-f", "s16le", "-"],
            capture_output=True, timeout=900)
        pcm = np.frombuffer(r.stdout, np.int16).astype(np.float32)
        spf = max(1, int(8000 / fps))
        k = min(n_frames, len(pcm) // spf)
        if k > 2:
            rms = np.sqrt((pcm[:k * spf].reshape(k, spf) ** 2).mean(axis=1))
            p95 = float(np.percentile(rms, 95)) + 1e-6
            env[:k] = np.clip(rms / p95, 0, 1)
            a = 0.35                       # light smoothing, keep the punch
            for i in range(1, k):
                env[i] = a * env[i] + (1 - a) * env[i - 1]
    except Exception:
        pass
    return env


def track_hands(video: Path, log, stride=2, det_w=448) -> dict:
    """Per-frame arrays, all cached as one .npz:
        track  (n, 3)  grip centroid cx, cy, conf (normalized, smoothed)
        lms    (n, MAX_HANDS, 21, 2) float16 — full skeleton landmarks
        pres   (n, MAX_HANDS) uint8 — which hand slots are valid
        motion (n,) float32 — frame-diff energy (drives auto-suggest)
    Detection on downscaled frames every `stride` frames; skeleton data is
    forward-filled across the stride so overlays don't flicker."""
    cache = track_cache_path(video)
    if cache.exists():
        log("STEP 2 — hand track + motion data found in cache ✔")
        z = np.load(cache)
        return {k: z[k] for k in z.files}

    try:
        import mediapipe as mp
    except ImportError:
        raise RuntimeError("Missing MediaPipe. Run:  pip install mediapipe")

    log("STEP 2 — MediaPipe tracking hand skeletons + motion ...")
    cap = cv2.VideoCapture(str(video))
    n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 1
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)) or det_w
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)) or det_w
    det_h = max(2, int(det_w * h / max(w, 1)))

    hands = mp.solutions.hands.Hands(
        static_image_mode=False, max_num_hands=MAX_HANDS, model_complexity=0,
        min_detection_confidence=0.35, min_tracking_confidence=0.35)

    lms = np.zeros((n, MAX_HANDS, 21, 2), dtype=np.float16)
    pres = np.zeros((n, MAX_HANDS), dtype=np.uint8)
    motion = np.zeros(n, dtype=np.float32)
    cents = {}
    prev_gray = None
    idx, t0 = 0, time.time()
    while idx < n:
        ok, frame = cap.read()
        if not ok:
            break
        if idx % stride == 0:
            small = cv2.resize(frame, (det_w, det_h),
                               interpolation=cv2.INTER_AREA)
            gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
            if prev_gray is not None:
                motion[idx] = float(cv2.absdiff(gray, prev_gray).mean())
            prev_gray = gray
            res = hands.process(cv2.cvtColor(small, cv2.COLOR_BGR2RGB))
            if res.multi_hand_landmarks:
                xs, ys = [], []
                for hi, hl in enumerate(res.multi_hand_landmarks[:MAX_HANDS]):
                    for li, lm in enumerate(hl.landmark):
                        lms[idx, hi, li, 0] = lm.x
                        lms[idx, hi, li, 1] = lm.y
                        xs.append(lm.x); ys.append(lm.y)
                    pres[idx, hi] = 1
                cents[idx] = (float(np.mean(xs)), float(np.mean(ys)))
        idx += 1
        if idx % 600 == 0:
            log(f"    tracking ... {idx}/{n} frames "
                f"({idx/max(time.time()-t0, 1e-6):.0f} fps)")
    cap.release()
    hands.close()
    n_read = idx

    # forward-fill skeletons + motion across the stride (kills flicker)
    for i in range(1, n_read):
        if not pres[i].any() and pres[i - 1].any() and (i % stride):
            pres[i] = pres[i - 1]
            lms[i] = lms[i - 1]
        if motion[i] == 0 and (i % stride):
            motion[i] = motion[i - 1]

    track = np.zeros((max(n_read, 1), 3), dtype=np.float32)
    track[:, 0], track[:, 1] = 0.5, 0.45
    if cents:
        keys = sorted(cents)
        ks = np.array(keys, dtype=np.float32)
        f_idx = np.arange(len(track), dtype=np.float32)
        track[:, 0] = np.interp(f_idx, ks, [cents[k][0] for k in keys])
        track[:, 1] = np.interp(f_idx, ks, [cents[k][1] for k in keys])
        track[:, 2] = np.interp(f_idx, ks, np.ones(len(ks), np.float32))
        a = 0.12
        for i in range(1, len(track)):
            track[i, :2] = a * track[i, :2] + (1 - a) * track[i - 1, :2]

    coverage = 100.0 * pres[:n_read].any(axis=1).mean() if n_read else 0.0
    log(f"    ✔ hand skeletons on ~{coverage:.0f}% of frames")
    fps = max(cv2.VideoCapture(str(video)).get(cv2.CAP_PROP_FPS) or 30.0, 1.0)
    log("    extracting voice envelope for kinetic typography ...")
    aud = audio_envelope(video, n_read, fps)
    data = {"track": track, "lms": lms[:n_read], "pres": pres[:n_read],
            "motion": motion[:n_read], "aud": aud}
    np.savez_compressed(cache, **data)
    return data


# ----------------------------------------------------------------------------
# Local auto-suggest: extra effects from motion + hand data (no AI cost)
# ----------------------------------------------------------------------------
def auto_suggest(effects: list, tdata: dict, fps: float, dur: float,
                 log) -> list:
    add = []
    pres, motion = tdata["pres"], tdata["motion"]
    n = len(pres)
    if n < int(fps * 4):
        return effects

    # 1) neon hand spans — ONLY if the AI barely used them (rarity > spam)
    have_glow = [(e["start"], e["end"]) for e in effects
                 if e["effect"] == "hand_glow"]
    glow_cov = sum(b - a for a, b in have_glow)
    glow_budget = 0 if glow_cov > 0.10 * dur else 3
    cov = pres.any(axis=1).astype(np.float32)
    k = max(2, int(fps))
    cov_s = np.convolve(cov, np.ones(k) / k, mode="same")
    spans, in_span, a = [], False, 0
    on = cov_s > 0.55
    for i in range(n):
        if on[i] and not in_span:
            in_span, a = True, i
        elif not on[i] and in_span:
            in_span = False
            if (i - a) / fps >= 3.0:
                spans.append((a / fps, i / fps))
    if in_span and (n - a) / fps >= 3.0:
        spans.append((a / fps, n / fps))
    glow_added, last_end = 0, -1e9
    for (sa, sb) in spans:
        if glow_added >= glow_budget:
            break
        if sa - last_end < GLOW_MIN_GAP + 6.0:
            continue
        if any(not (sb <= ga - GLOW_MIN_GAP or sa >= gb + GLOW_MIN_GAP)
               for ga, gb in have_glow):
            continue
        add.append({"start": round(sa, 2),
                    "end": round(min(sb, sa + 5.0), 2),
                    "effect": "hand_glow", "intensity": 6, "text": "",
                    "direction": "",
                    "reason": "auto: hands tracked confidently here"})
        last_end = min(sb, sa + 5.0)
        glow_added += 1

    # 2) impact combos on motion spikes the AI missed
    m = motion.astype(np.float64)
    hot = [(e["start"], e["end"]) for e in effects
           if e["effect"] in ("zoom_punch", "camera_shake", "impact_flash")
           or e["effect"] in TIMEWARP]
    combos = 0
    if m.std() > 1e-6:
        z = (m - m.mean()) / m.std()
        last = -1e9
        for i in np.where(z > 2.4)[0]:
            t = float(i) / fps
            if t - last < 10.0 or t > dur - 1.5:
                continue
            last = t
            if any(a - 2.0 <= t <= b + 2.0 for a, b in hot):
                continue
            add += [
                {"start": round(t - 0.1, 2), "end": round(t + 0.5, 2),
                 "effect": "zoom_punch", "intensity": 8, "text": "",
                 "reason": "auto: motion spike"},
                {"start": round(t, 2), "end": round(t + 0.16, 2),
                 "effect": "impact_flash", "intensity": 7, "text": "",
                 "reason": "auto: motion spike"},
                {"start": round(t, 2), "end": round(t + 0.7, 2),
                 "effect": "camera_shake", "intensity": 6, "text": "",
                 "reason": "auto: motion spike"},
                {"start": round(t, 2), "end": round(t + 0.4, 2),
                 "effect": "rgb_split", "intensity": 6, "text": "",
                 "reason": "auto: motion spike"},
            ]
            combos += 1
            if combos >= 8:
                break
    if add:
        log(f"    ➕ auto-suggest: {glow_added} neon hand spans, "
            f"{combos} impact combos added locally")
    return effects + add


# ----------------------------------------------------------------------------
# Quality floor — guaranteed hook, scene coverage, transitions at cuts.
# Purely local (motion data), zero API cost: even a lazy AI plan renders dense.
# ----------------------------------------------------------------------------
def detect_scene_cuts(motion: np.ndarray, fps: float, min_gap=1.5) -> list:
    """Hard camera cuts = single-frame motion spikes way above baseline."""
    m = motion.astype(np.float64)
    if len(m) < fps * 4 or m.std() < 1e-6:
        return []
    med = np.median(m)
    mad = np.median(np.abs(m - med)) + 1e-6
    cuts, last = [], -1e9
    for i in np.where((m - med) / mad > 9.0)[0]:
        t = float(i) / fps
        if t - last >= min_gap:
            cuts.append(round(t, 2))
            last = t
    return cuts


def enrich_plan(effects: list, tdata: dict, fps: float, dur: float,
                log) -> list:
    add = []
    motion = tdata["motion"]
    cuts = detect_scene_cuts(motion, fps)
    m_mean = float(motion.mean()) if len(motion) else 0.0

    def mk(start, end, fx, i, text="", why=""):
        return {"start": round(max(0.0, start), 2),
                "end": round(min(dur, end), 2), "effect": fx, "intensity": i,
                "text": text, "direction": "auto", "reason": "auto: " + why}

    # 1) GUARANTEED HOOK — the first seconds must hit, no matter the plan
    strong = ("title", "zoom_in", "zoom_punch", "action_grade",
              "dramatic_grade", "letterbox", "hand_glow")
    early = sum(1 for e in effects
                if e["start"] < 4.0 and e["effect"] in strong)
    hook = False
    if early < 3:
        hook = True
        add += [mk(0.0, 3.6, "zoom_in", 7, why="hook"),
                mk(0.0, 4.8, "action_grade", 7, why="hook"),
                mk(0.0, 6.0, "letterbox", 6, why="hook")]
        # snap on the first motion beat of the video
        if len(motion) > int(3 * fps):
            beat = int(np.argmax(motion[int(0.4 * fps):int(6 * fps)])
                       + 0.4 * fps)
            bt = beat / fps
            add += [mk(bt - 0.1, bt + 0.5, "zoom_punch", 8, why="hook beat"),
                    mk(bt, bt + 0.15, "impact_flash", 6, why="hook beat")]

    # 2) SCENE COVERAGE — every scene gets a look (cinematic <-> action)
    bounds = [0.0] + [c for c in cuts if 2.0 < c < dur - 2.0] + [dur]
    scenes = [(a, b) for a, b in zip(bounds, bounds[1:]) if b - a >= 4.0]

    def covered(a, b):
        return any(e["start"] < b - 0.5 and e["end"] > a + 0.5
                   for e in effects + add
                   if e["effect"] not in ("whip_transition", "zoom_blur"))

    last_look, n_scene = "dramatic_grade", 0
    for (a, b) in scenes:
        if n_scene >= 14 or covered(a, b):
            continue
        seg = motion[int(a * fps):int(b * fps)]
        hot = len(seg) > 0 and float(seg.mean()) > m_mean * 1.1
        look = "action_grade" if hot else (
            "action_grade" if last_look == "dramatic_grade" and hot else
            "dramatic_grade")
        if look == last_look:      # force alternation when motion is ambiguous
            look = ("action_grade" if last_look == "dramatic_grade"
                    else "dramatic_grade")
        last_look = look
        ga, gb = a + 0.2, min(b - 0.2, a + 10.0)
        add.append(mk(ga, gb, look, 6, why="scene look"))
        if not hot and (b - a) >= 7.0:
            add.append(mk(a + 0.8, a + 4.4, "zoom_in", 6, why="calm scene"))
        if not hot and (b - a) >= 10.0:
            add.append(mk(ga, gb, "letterbox", 5, why="cinematic scene"))
        n_scene += 1

    # 3) TRANSITIONS at hard cuts the AI didn't cover — rotating styles so
    #    it never feels repetitive (whip follows the pin direction via track)
    trans = [e for e in effects + add
             if e["effect"] in ("whip_transition", "zoom_blur",
                                "dip_to_black", "glitch", "cross_dissolve",
                                "match_cut", "light_leak", "frame_block")]
    rotation = ["whip_transition", "cross_dissolve", "light_leak",
                "whip_transition", "frame_block", "cross_dissolve"]
    n_t = 0
    for c in cuts:
        if n_t >= 10 or c < 1.0 or c > dur - 1.0:
            continue
        if any(abs(e["start"] - c) < 1.2 for e in trans):
            continue
        style = rotation[n_t % len(rotation)]
        span = 0.5 if style == "whip_transition" else \
            (0.6 if style == "frame_block" else 1.0)
        ev = mk(c - span * 0.45, c + span * 0.55, style, 7, why="scene cut")
        add.append(ev)
        trans.append(ev)
        n_t += 1

    if add:
        log(f"    🎬 quality floor: hook {'injected' if hook else 'ok'}, "
            f"{n_scene} scene looks, {n_t} cut transitions "
            f"({len(cuts)} cuts detected)")
    return effects + add


# ----------------------------------------------------------------------------
# Timeline math — VOICE-SAFE speed ramps
# ----------------------------------------------------------------------------
CATCH_SPEED = 1.75      # ▶▶ catch-up speed after slow-mo / freeze


def ease_in_out(p):
    return 0.5 - 0.5 * math.cos(math.pi * max(0.0, min(1.0, p)))


def ease_out(p):
    p = max(0.0, min(1.0, p))
    return 1.0 - (1.0 - p) ** 3


def env(t, a, b, attack=0.15, release=0.3):
    if t <= a or t >= b:
        return 0.0
    d = b - a
    atk = min(attack, d * 0.5); rel = min(release, d * 0.5)
    if t < a + atk:
        return (t - a) / atk
    if t > b - rel:
        return (b - t) / rel
    return 1.0


def slow_speed(intensity):  # 1..10 -> 0.75..0.30
    return 0.75 - 0.05 * intensity


def fast_speed(intensity):  # 1..10 -> ~1.33..2.50
    return 1.2 + 0.13 * intensity


def build_speed_segments(effects, dur, voice_safe, log=lambda m: None):
    """Returns (segs, freezes).
    segs   : non-overlapping (a, b, speed) covering [0, dur]
    freezes: (t, hold_seconds, text)
    voice_safe=True: audio is NEVER warped. Every slow_motion / freeze_frame
    is followed by an auto catch-up segment at CATCH_SPEED so the net timeline
    change is ZERO and the untouched audio stays in perfect sync. speed_up is
    dropped (it would desync narration)."""
    raw = []
    for e in effects:
        if e["effect"] == "slow_motion":
            raw.append((e["start"], e["end"], "slow",
                        slow_speed(e["intensity"]), ""))
        elif e["effect"] == "speed_up":
            raw.append((e["start"], e["end"], "fast",
                        fast_speed(e["intensity"]), ""))
        elif e["effect"] == "freeze_frame":
            raw.append((e["start"], e["end"], "freeze",
                        e["end"] - e["start"], e["text"]))
    raw.sort()
    segs, freezes, t = [], [], 0.0
    save = 1.0 - 1.0 / CATCH_SPEED       # output seconds saved per catch second

    for i, (a, b, kind, val, text) in enumerate(raw):
        a = max(a, t)
        nxt = min(raw[i + 1][0] if i + 1 < len(raw) else dur, dur)
        if not voice_safe:
            if kind == "freeze":
                freezes.append((a, val, text))
                continue
            if b <= a + 0.05:
                continue
            if a > t:
                segs.append((t, a, 1.0))
            segs.append((a, b, val))
            t = b
            continue

        # ---------------- voice-safe ----------------
        if kind == "fast":
            log(f"    ⚠ voice-safe: dropped speed_up @ {a:.1f}s "
                f"(would desync the narration)")
            continue
        if kind == "slow":
            if b <= a + 0.05:
                continue
            avail = max(0.0, nxt - b)
            max_extra = avail * save
            extra = (b - a) * (1.0 / val - 1.0)
            if extra > max_extra:
                if max_extra < 0.15:
                    log(f"    ⚠ voice-safe: dropped slow_motion @ {a:.1f}s "
                        f"(no room for the catch-up ramp)")
                    continue
                val = (b - a) / ((b - a) + max_extra)
                extra = max_extra
            catch = extra / save
            if a > t:
                segs.append((t, a, 1.0))
            segs.append((a, b, val))
            segs.append((b, min(b + catch, dur), CATCH_SPEED))
            t = min(b + catch, dur)
        else:  # freeze
            avail = max(0.0, nxt - a)
            hold = min(val, avail * save)
            if hold < 0.15:
                log(f"    ⚠ voice-safe: dropped freeze_frame @ {a:.1f}s "
                    f"(no room for the catch-up ramp)")
                continue
            freezes.append((a, hold, text))
            catch = hold / save
            if a > t:
                segs.append((t, a, 1.0))
            segs.append((a, min(a + catch, dur), CATCH_SPEED))
            t = min(a + catch, dur)

    if t < dur:
        segs.append((t, dur, 1.0))
    return segs, freezes


def atempo_chain(f):
    parts = []
    while f < 0.5:
        parts.append("atempo=0.5"); f /= 0.5
    while f > 2.0:
        parts.append("atempo=2.0"); f /= 2.0
    parts.append(f"atempo={f:.4f}")
    return ",".join(parts)


def build_audio_ops(segs, freezes):
    """(non-voice-safe path) ordered ops along source time."""
    fz = list(freezes)
    ops = []
    for (a, b, s) in segs:
        cur = a
        while fz and a <= fz[0][0] < b:
            ft, hold, _ = fz.pop(0)
            if ft > cur:
                ops.append(("src", cur, ft, s))
            ops.append(("sil", hold))
            cur = ft
        if b > cur:
            ops.append(("src", cur, b, s))
    for (ft, hold, _) in fz:
        ops.append(("sil", hold))
    return ops


# ----------------------------------------------------------------------------
# Typography — glow text engine (the "wow font", now everywhere)
# ----------------------------------------------------------------------------
ACCENT = (36, 116, 255)          # BGR orange-red
NEON = [(255, 255, 60), (255, 80, 255), (80, 200, 255), (120, 255, 120)]
PANEL = (18, 16, 14)
FONT = cv2.FONT_HERSHEY_DUPLEX


def glow_text(img, text, org, fs, color, alpha, thickness=2,
              glow_color=None, glow_k=21):
    """Neon glow text: blurred halo layer added, crisp core on top."""
    if alpha <= 0.01 or not text:
        return
    x, y = int(org[0]), int(org[1])
    (tw, th), _ = cv2.getTextSize(text, FONT, fs, thickness)
    pad = glow_k * 2
    x1, y1 = max(0, x - pad), max(0, y - th - pad)
    x2, y2 = min(img.shape[1], x + tw + pad), min(img.shape[0], y + pad)
    if x2 <= x1 or y2 <= y1:
        return
    roi = img[y1:y2, x1:x2]
    layer = np.zeros_like(roi)
    gc = glow_color or color
    cv2.putText(layer, text, (x - x1, y - y1), FONT, fs, gc,
                thickness + 4, cv2.LINE_AA)
    layer = cv2.GaussianBlur(layer, (glow_k, glow_k), 0)
    cv2.add(roi, (layer * (0.9 * alpha)).astype(np.uint8), roi)
    core = tuple(int(c * alpha + 255 * (1 - alpha) * 0) for c in color)
    ov = roi.copy()
    cv2.putText(ov, text, (x - x1, y - y1), FONT, fs, color,
                thickness, cv2.LINE_AA)
    cv2.addWeighted(ov, alpha, roi, 1 - alpha, 0, roi)


def rounded_rect(img, x1, y1, x2, y2, r, color, alpha):
    over = img.copy()
    cv2.rectangle(over, (x1 + r, y1), (x2 - r, y2), color, -1)
    cv2.rectangle(over, (x1, y1 + r), (x2, y2 - r), color, -1)
    for cx, cy in ((x1 + r, y1 + r), (x2 - r, y1 + r),
                   (x1 + r, y2 - r), (x2 - r, y2 - r)):
        cv2.circle(over, (cx, cy), r, color, -1)
    cv2.addWeighted(over, alpha, img, 1 - alpha, 0, img)


def draw_label(img, text, alpha, W, H, prog=1.0):
    """Lower-third: plate slides up + glow title text."""
    if not text or alpha <= 0.01:
        return
    scale = H / 1080.0
    fs = 1.2 * scale
    (tw, th), _ = cv2.getTextSize(text, FONT, fs, 2)
    pad = int(26 * scale)
    slide = int(30 * scale * (1.0 - ease_out(min(1.0, prog * 3))))
    x1 = (W - tw) // 2 - pad
    y2 = H - int(84 * scale) + slide
    y1 = y2 - th - 2 * pad
    x2 = x1 + tw + 2 * pad
    rounded_rect(img, x1, y1, x2, y2, int(12 * scale), PANEL, 0.72 * alpha)
    bar = img.copy()
    cv2.rectangle(bar, (x1, y1), (x1 + int(8 * scale), y2), ACCENT, -1)
    cv2.rectangle(bar, (x1, y2 - int(4 * scale)), (x2, y2), ACCENT, -1)
    cv2.addWeighted(bar, alpha, img, 1 - alpha, 0, img)
    glow_text(img, text, (x1 + pad + int(10 * scale), y2 - pad), fs,
              (245, 245, 245), alpha, 2, glow_color=ACCENT,
              glow_k=int(21 * scale) | 1)


def draw_title(img, text, alpha, W, H, prog=1.0):
    """BIG center-screen neon title with slide + double glow."""
    if not text or alpha <= 0.01:
        return
    scale = H / 1080.0
    fs = 2.6 * scale
    (tw, th), _ = cv2.getTextSize(text, FONT, fs, 4)
    while tw > W * 0.85 and fs > 0.8:
        fs *= 0.92
        (tw, th), _ = cv2.getTextSize(text, FONT, fs, 4)
    x = (W - tw) // 2
    slide = int(46 * scale * (1.0 - ease_out(min(1.0, prog * 2.5))))
    y = int(H * 0.72) + th // 2 + slide          # lower third, not mid-screen
    # dark cinematic band behind
    band = img.copy()
    cv2.rectangle(band, (0, y - th - int(40 * scale)),
                  (W, y + int(40 * scale)), (8, 8, 8), -1)
    cv2.addWeighted(band, 0.45 * alpha, img, 1 - 0.45 * alpha, 0, img)
    glow_text(img, text, (x, y), fs, (250, 250, 250), alpha, 4,
              glow_color=NEON[2], glow_k=int(31 * scale) | 1)
    # accent underline
    ln = img.copy()
    cv2.rectangle(ln, (x, y + int(16 * scale)),
                  (x + tw, y + int(16 * scale) + max(2, int(5 * scale))),
                  ACCENT, -1)
    cv2.addWeighted(ln, alpha, img, 1 - alpha, 0, img)


def draw_hand_text(img, text, hx, hy, alpha, W, H, out_t):
    """Professional tracking tag: anchor dot on the hands, thin connector
    line, small glowing plate that floats and FOLLOWS the grip."""
    if not text or alpha <= 0.01:
        return
    scale = H / 1080.0
    fs = 0.85 * scale
    (tw, th), _ = cv2.getTextSize(text, FONT, fs, 2)
    pad = int(16 * scale)
    drift = math.sin(out_t * 2.1) * 4 * scale
    # plate above-right of the hand, clamped on screen
    px = int(min(max(hx + 70 * scale, 16), W - tw - 2 * pad - 16))
    py = int(min(max(hy - 110 * scale + drift, th + 2 * pad + 16),
                 H - int(180 * scale)))
    x1, y1 = px, py - th - 2 * pad
    x2, y2 = px + tw + 2 * pad, py
    ax, ay = int(min(max(hx, 8), W - 8)), int(min(max(hy, 8), H - 8))
    # connector: anchor dot -> elbow -> plate corner
    ov = img.copy()
    ex, ey = x1 + int(6 * scale), y2 + int(14 * scale)
    cv2.line(ov, (ax, ay), (ex, ey), NEON[2], max(1, int(2 * scale)),
             cv2.LINE_AA)
    cv2.line(ov, (ex, ey), (x1 + int(6 * scale), y2), NEON[2],
             max(1, int(2 * scale)), cv2.LINE_AA)
    cv2.circle(ov, (ax, ay), max(2, int(5 * scale)), NEON[2], -1,
               cv2.LINE_AA)
    cv2.circle(ov, (ax, ay), max(3, int(9 * scale)), NEON[2],
               max(1, int(1.5 * scale)), cv2.LINE_AA)
    cv2.addWeighted(ov, 0.9 * alpha, img, 1 - 0.9 * alpha, 0, img)
    rounded_rect(img, x1, y1, x2, y2, int(9 * scale), PANEL, 0.72 * alpha)
    bar = img.copy()
    cv2.rectangle(bar, (x1, y1), (x1 + int(5 * scale), y2), NEON[2], -1)
    cv2.addWeighted(bar, alpha, img, 1 - alpha, 0, img)
    glow_text(img, text, (x1 + pad + int(6 * scale), y2 - pad), fs,
              (245, 245, 245), alpha, 2, glow_color=NEON[2],
              glow_k=int(15 * scale) | 1)


def draw_kinetic_text(img, text, p, voice, alpha, W, H):
    """Kinetic typography: words land one by one with a pop, and the whole
    line breathes with the narration loudness (voice 0..1)."""
    if not text or alpha <= 0.01:
        return
    words = text.split()[:8]
    if not words:
        return
    scale = H / 1080.0
    fs0 = 1.55 * scale
    # layout at base size so words don't jitter while popping
    widths = [cv2.getTextSize(w, FONT, fs0, 3)[0][0] for w in words]
    gap = int(22 * scale)
    total = sum(widths) + gap * (len(words) - 1)
    while total > W * 0.9 and fs0 > 0.6:
        fs0 *= 0.92
        widths = [cv2.getTextSize(w, FONT, fs0, 3)[0][0] for w in words]
        total = sum(widths) + gap * (len(words) - 1)
    x = (W - total) // 2
    y = int(H * 0.70)
    reveal = p * (len(words) + 0.8)          # words arrive across the event
    for wi, word in enumerate(words):
        wp = reveal - wi
        if wp <= 0:
            break
        pop = 1.0 + 0.45 * max(0.0, 1.0 - wp * 2.2)      # arrival overshoot
        breathe = 1.0 + 0.10 * voice
        fs = fs0 * pop * breathe
        (tw, th), _ = cv2.getTextSize(word, FONT, fs, 3)
        cxx = x + widths[wi] // 2
        emph = (wi == len(words) - 1)         # last word carries the payoff
        col = (250, 250, 250)
        gcol = ACCENT if emph else NEON[2]
        a = alpha * min(1.0, wp * 3.0) * (0.85 + 0.15 * voice)
        glow_text(img, word, (cxx - tw // 2, y + th // 2), fs, col, a, 3,
                  glow_color=gcol, glow_k=int(23 * scale) | 1)
        x += widths[wi] + gap


def draw_badge(img, text, W, H, pulse=1.0):
    scale = H / 1080.0
    fs = 0.85 * scale
    (tw, th), _ = cv2.getTextSize(text, FONT, fs, 2)
    pad = int(14 * scale)
    x2 = W - int(40 * scale)
    x1 = x2 - tw - 2 * pad - int(26 * scale)
    y1 = int(40 * scale); y2 = y1 + th + 2 * pad
    rounded_rect(img, x1, y1, x2, y2, int(10 * scale), PANEL, 0.7)
    cy = (y1 + y2) // 2
    cv2.circle(img, (x1 + pad + int(5 * scale), cy),
               max(1, int(6 * scale * pulse)), ACCENT, -1)
    glow_text(img, text, (x1 + pad + int(22 * scale), y2 - pad), fs,
              (240, 240, 240), 1.0, 2, glow_color=ACCENT,
              glow_k=int(13 * scale) | 1)


def draw_hud(img, src_t, dur, W, H, badge=None, badge_pulse=1.0):
    scale = H / 1080.0
    bar_h = max(3, int(5 * scale))
    p = 0.0 if not dur else max(0.0, min(1.0, src_t / dur))
    over = img.copy()
    cv2.rectangle(over, (0, H - bar_h), (W, H), (60, 58, 55), -1)
    cv2.addWeighted(over, 0.55, img, 0.45, 0, img)
    cv2.rectangle(img, (0, H - bar_h), (int(W * p), H), ACCENT, -1)
    mm, ss = int(src_t // 60), int(src_t % 60)
    txt = f"{mm:02d}:{ss:02d}"
    fs = 0.7 * scale
    (tw, th), _ = cv2.getTextSize(txt, FONT, fs, 2)
    pad = int(12 * scale)
    rounded_rect(img, int(40 * scale), int(40 * scale),
                 int(40 * scale) + tw + 2 * pad,
                 int(40 * scale) + th + 2 * pad, int(8 * scale), PANEL, 0.65)
    cv2.putText(img, txt, (int(40 * scale) + pad,
                           int(40 * scale) + th + pad - int(2 * scale)),
                FONT, fs, (235, 235, 235), 2, cv2.LINE_AA)
    if badge:
        draw_badge(img, badge, W, H, badge_pulse)


# ----------------------------------------------------------------------------
# Neon hand skeleton overlay (cyberpunk)
# ----------------------------------------------------------------------------
def draw_hand_glow(img, lms_f, pres_f, alpha, out_t, transform):
    """lms_f: (MAX_HANDS, 21, 2) normalized to the SOURCE frame; transform
    maps source-frame pixels -> current (possibly zoomed/shaken) frame."""
    if alpha <= 0.02 or not pres_f.any():
        return
    H, W = img.shape[:2]
    x0, y0, sx, sy = transform
    pulse = 0.75 + 0.25 * math.sin(out_t * 5.0)
    a = alpha * pulse
    for hi in range(pres_f.shape[0]):
        if not pres_f[hi]:
            continue
        pts = lms_f[hi].astype(np.float32)
        # landmarks are normalized to the source frame == current W,H canvas
        xs = (pts[:, 0] * W - x0) * sx
        ys = (pts[:, 1] * H - y0) * sy
        P = np.stack([xs, ys], axis=1).astype(np.int32)
        x1, y1 = P.min(axis=0); x2, y2 = P.max(axis=0)
        m = max(24, int(0.06 * H))
        rx1, ry1 = max(0, x1 - m), max(0, y1 - m)
        rx2, ry2 = min(W, x2 + m), min(H, y2 + m)
        if rx2 - rx1 < 8 or ry2 - ry1 < 8 or rx2 <= 0 or ry2 <= 0:
            continue
        if x2 < -m or y2 < -m or x1 > W + m or y1 > H + m:
            continue
        roi = img[ry1:ry2, rx1:rx2]
        layer = np.zeros_like(roi)
        color = NEON[hi % len(NEON)]
        Q = P - np.array([rx1, ry1])
        lw = max(1, int(H / 480))
        for (i, j) in HAND_CONNECTIONS:
            cv2.line(layer, tuple(Q[i]), tuple(Q[j]), color, lw + 1,
                     cv2.LINE_AA)
        for q in Q:
            cv2.circle(layer, tuple(q), lw + 1, (255, 255, 255), -1,
                       cv2.LINE_AA)
        # corner brackets (targeting HUD feel)
        bx1, by1 = max(0, x1 - m // 2 - rx1), max(0, y1 - m // 2 - ry1)
        bx2 = min(rx2 - rx1 - 1, x2 + m // 2 - rx1)
        by2 = min(ry2 - ry1 - 1, y2 + m // 2 - ry1)
        L = max(6, (bx2 - bx1) // 6)
        for (cx, cy, dx, dy) in ((bx1, by1, 1, 1), (bx2, by1, -1, 1),
                                 (bx1, by2, 1, -1), (bx2, by2, -1, -1)):
            cv2.line(layer, (cx, cy), (cx + dx * L, cy), color, lw,
                     cv2.LINE_AA)
            cv2.line(layer, (cx, cy), (cx, cy + dy * L), color, lw,
                     cv2.LINE_AA)
        k = max(9, (2 * int(H / 180)) + 1) | 1
        glow = cv2.GaussianBlur(layer, (k, k), 0)
        cv2.add(roi, (glow * (1.1 * a)).clip(0, 255).astype(np.uint8), roi)
        cv2.add(roi, (layer * (0.9 * a)).clip(0, 255).astype(np.uint8), roi)


# ----------------------------------------------------------------------------
# Per-frame effect application (vectorized — no per-pixel Python)
# ----------------------------------------------------------------------------
class FrameFX:
    def __init__(self, W, H, effects, tdata, fps, opts):
        self.W, self.H, self.fps, self.opts = W, H, fps, opts
        self.track = tdata["track"] if tdata else None
        self.lms = tdata["lms"] if tdata else None
        self.pres = tdata["pres"] if tdata else None
        by = lambda name: [e for e in effects if e["effect"] == name]
        self.zooms    = by("zoom_in")
        self.zoomouts = by("zoom_out")
        self.punches  = by("zoom_punch")
        self.flashes  = by("impact_flash")
        self.shakes   = by("camera_shake")
        self.grades   = by("dramatic_grade")
        self.agrades  = by("action_grade")
        self.whips    = by("whip_transition")
        self.zblurs   = by("zoom_blur")
        self.dips     = by("dip_to_black")
        self.labels   = by("label")
        self.titles   = by("title")
        self.htexts   = by("hand_text")
        self.glows    = by("hand_glow")
        self.splits   = by("rgb_split")
        self.glitches = by("glitch")
        self.boxes    = by("letterbox")
        self.dissolves = by("cross_dissolve") + by("match_cut")
        self.jumps     = by("jump_cut")
        self.fblocks   = by("frame_block")
        self.leaks     = by("light_leak")
        self.grains    = by("film_grain")
        self.screens   = by("split_screen")
        self.kinetics  = by("kinetic_text")
        self.aud = tdata.get("aud") if tdata else None
        # light-leak texture: warm blob from the top-right + soft streak
        yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
        d1 = np.sqrt(((xx - W * 1.05) / W) ** 2 + ((yy + H * 0.10) / H) ** 2)
        d2 = np.abs((yy - (0.25 * H + 0.3 * xx * H / W))) / (0.22 * H)
        blob = np.clip(1.15 - d1, 0, 1) ** 2.2 + 0.5 * np.clip(1 - d2, 0, 1) ** 3
        self.leak = np.dstack([blob * 45, blob * 130, blob * 255]
                              ).astype(np.float32)          # warm BGR
        # film grain: pre-rendered monochromatic noise tiles (organic texture)
        g_rng = np.random.default_rng(1234)
        self.grain_tiles = [
            np.repeat(g_rng.integers(0, 256, (H, W, 1), dtype=np.uint8),
                      3, axis=2)
            for _ in range(6)]
        self.global_grain = float(opts.get("global_grain", 0.0))
        # resolve whip directions once ('auto' -> read the pin direction
        # from the tracked grip velocity around the transition)
        for e in self.whips:
            d = e.get("direction", "auto") or "auto"
            if d == "auto":
                d = self._motion_direction(e["start"])
            e["_dir"] = d
        # cinematic LUT (single-channel S-curve)
        x = np.arange(256, dtype=np.float32) / 255.0
        s = 1.0 / (1.0 + np.exp(-9.0 * (x - 0.5)))
        s = (s - s.min()) / (s.max() - s.min())
        self.lut = np.clip(255.0 * (0.35 * s + 0.65 * x) * 1.12,
                           0, 255).astype(np.uint8)
        # Hollywood action LUT (per-channel orange-teal, punchy)
        s2 = 1.0 / (1.0 + np.exp(-11.0 * (x - 0.5)))
        s2 = (s2 - s2.min()) / (s2.max() - s2.min())
        base = 0.55 * s2 + 0.45 * x
        r = np.clip(255.0 * (base * 1.14 + 0.03), 0, 255)          # warm hi
        g = np.clip(255.0 * (base * 1.05), 0, 255)
        b = np.clip(255.0 * (base * 0.98 + 0.05 * (1.0 - x)), 0, 255)  # teal lo
        self.action_lut = np.dstack([b, g, r]).astype(np.uint8)    # BGR
        yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
        d = np.sqrt(((xx - W / 2) / (W / 2)) ** 2 +
                    ((yy - H / 2) / (H / 2)) ** 2)
        self.vig = np.clip(1.0 - 0.45 * np.clip(d - 0.55, 0, 1.2) ** 1.6,
                           0.35, 1.0)[..., None].astype(np.float32)

    def grip(self, frame_idx):
        if self.track is None:
            return self.W * 0.5, self.H * 0.45
        i = min(max(frame_idx, 0), len(self.track) - 1)
        return float(self.track[i, 0]) * self.W, float(self.track[i, 1]) * self.H

    def _motion_direction(self, t):
        """Which way did the pin go? Read grip velocity around time t."""
        if self.track is None or len(self.track) < 4:
            return "left"
        i0 = int(max(0, (t - 0.5) * self.fps))
        i1 = int(min(len(self.track) - 1, (t + 0.35) * self.fps))
        if i1 <= i0 + 1:
            return "left"
        dx = float(self.track[i1, 0] - self.track[i0, 0])
        dy = float(self.track[i1, 1] - self.track[i0, 1])
        if abs(dx) >= abs(dy):
            return "right" if dx > 0 else "left"
        return "down" if dy > 0 else "up"

    def zoom_amount(self, t):
        z = 1.0
        for e in self.zblurs:                    # crash-zoom transition pulse
            a = env(t, e["start"], e["end"], 0.10, 0.14)
            if a > 0:
                z *= 1.0 + (0.12 + 0.03 * e["intensity"]) * a
        for e in self.zooms:
            if e["start"] <= t <= e["end"]:
                p = ease_in_out((t - e["start"]) / (e["end"] - e["start"]))
                z *= 1.0 + (0.06 + 0.028 * e["intensity"]) * p
        for e in self.zoomouts:
            if e["start"] <= t <= e["end"]:
                p = ease_in_out((t - e["start"]) / (e["end"] - e["start"]))
                z *= 1.0 + (0.06 + 0.028 * e["intensity"]) * (1.0 - p)
        for e in self.punches:
            if e["start"] <= t <= e["end"]:
                d = e["end"] - e["start"]
                p = (t - e["start"]) / d
                amp = 0.08 + 0.03 * e["intensity"]
                z *= 1.0 + amp * (ease_out(p * 3) if p < 1 / 3
                                  else (1.0 - ease_in_out((p - 1/3) / (2/3))))
        return z

    def shake_offset(self, t, out_t):
        dx = dy = 0.0
        for e in self.shakes:
            a = env(t, e["start"], e["end"], 0.05, 0.35)
            if a > 0:
                amp = (2.5 + 1.4 * e["intensity"]) * (self.H / 1080.0) * a
                dx += amp * math.sin(out_t * 61.7) * math.cos(out_t * 23.3)
                dy += amp * math.sin(out_t * 47.1 + 1.7)
        return dx, dy

    def stutter_active(self, t):
        return any(e["start"] <= t <= e["end"] for e in self.jumps)

    def pre_transition(self, frame, t, snaps):
        """Cross dissolve / match cut: morph FROM a snapshot taken at the
        event start INTO the live footage. With the narration flowing uncut
        underneath, this reads as a J-cut/L-cut — sound bridging the change."""
        for e in self.dissolves:
            a, b = e["start"], e["end"]
            key = id(e)
            if a <= t <= b:
                if key not in snaps:
                    snaps[key] = frame.copy()
                p = ease_in_out((t - a) / max(b - a, 1e-6))
                snap = snaps[key]
                if e["effect"] == "match_cut":     # outgoing shot zooms in
                    z = 1.0 + 0.20 * p
                    cw, ch = int(self.W / z), int(self.H / z)
                    snap = cv2.resize(
                        cv2.getRectSubPix(snap, (cw, ch),
                                          (self.W / 2, self.H / 2)),
                        (self.W, self.H), interpolation=cv2.INTER_LINEAR)
                frame = cv2.addWeighted(snap, 1.0 - p, frame, p, 0)
            elif t > b and key in snaps:
                snaps.pop(key, None)
        return frame

    def _split_screen(self, frame, frame_idx, a, transform):
        """Multi-cam windowing: WIDE (center crop) + GRIP CAM (auto-tracked
        magnified panel) side by side."""
        W, H = self.W, self.H
        half = W // 2
        left = frame[:, W // 4: W // 4 + half].copy()
        x0, y0, sx, sy = transform
        gx, gy = self.grip(frame_idx)
        gx, gy = (gx - x0) * sx, (gy - y0) * sy
        Z = 2.1
        cw, ch = half / Z, H / Z
        gx = min(max(gx, cw / 2), W - cw / 2)
        gy = min(max(gy, ch / 2), H - ch / 2)
        right = cv2.resize(
            cv2.getRectSubPix(frame, (int(cw), int(ch)), (gx, gy)),
            (half, H), interpolation=cv2.INTER_LINEAR)
        comp = np.hstack([left, right[:, :W - half]])
        scale = H / 1080.0
        cv2.rectangle(comp, (half - max(2, int(3 * scale)), 0),
                      (half + max(2, int(3 * scale)), H), ACCENT, -1)
        glow_text(comp, "WIDE", (int(24 * scale), int(64 * scale)),
                  0.75 * scale, (240, 240, 240), a, 2, glow_color=ACCENT,
                  glow_k=int(13 * scale) | 1)
        glow_text(comp, "GRIP CAM", (half + int(24 * scale),
                                     int(64 * scale)),
                  0.75 * scale, (240, 240, 240), a, 2, glow_color=NEON[2],
                  glow_k=int(13 * scale) | 1)
        return cv2.addWeighted(comp, a, frame, 1.0 - a, 0)

    def glow_alpha(self, t):
        mode = self.opts.get("hand_fx_mode", "AI-decided")
        if mode == "Off" or self.pres is None:
            return 0.0
        if mode == "Always on":
            return 0.65
        a = 0.0
        for e in self.glows:
            a = max(a, env(t, e["start"], e["end"], 0.4, 0.5)
                    * (0.40 + 0.045 * e["intensity"]))
        return a

    def apply(self, frame, t, out_t, frame_idx, snaps=None, badge=None,
              freeze_text=None, freeze_p=0.0):
        W, H = self.W, self.H
        if snaps is not None and self.dissolves:
            frame = self.pre_transition(frame, t, snaps)
        z = self.zoom_amount(t)
        if freeze_text is not None:
            z *= 1.0 + 0.05 * ease_in_out(freeze_p)
        dx, dy = self.shake_offset(t, out_t)
        if abs(dx) + abs(dy) > 0.2:
            z = max(z, 1.035)
        transform = (0.0, 0.0, 1.0, 1.0)          # src px -> canvas px
        if z > 1.001 or abs(dx) + abs(dy) > 0.2:
            cw, ch = W / z, H / z
            cx, cy = self.grip(frame_idx)
            cx = min(max(cx + dx, cw / 2), W - cw / 2)
            cy = min(max(cy + dy, ch / 2), H - ch / 2)
            frame = cv2.getRectSubPix(frame, (int(cw), int(ch)), (cx, cy))
            frame = cv2.resize(frame, (W, H), interpolation=cv2.INTER_LINEAR)
            transform = (cx - cw / 2, cy - ch / 2, W / cw, H / ch)

        # directional whip: real camera push (translate) + axis motion blur —
        # at pins the push follows the direction the losing hand was driven
        for e in self.whips:
            a = env(t, e["start"], e["end"], 0.08, 0.08)
            if a > 0.05:
                p = (t - e["start"]) / max(e["end"] - e["start"], 1e-6)
                sweep = (ease_in_out(p) * 2.0 - 1.0)      # -1 .. +1 through cut
                amp = (0.10 + 0.02 * e["intensity"]) * min(W, H) * a
                dxy = {"left": (-1, 0), "right": (1, 0),
                       "up": (0, -1), "down": (0, 1)}.get(e.get("_dir",
                                                                "left"),
                                                          (-1, 0))
                tx, ty = dxy[0] * sweep * amp, dxy[1] * sweep * amp
                M = np.float32([[1, 0, tx], [0, 1, ty]])
                frame = cv2.warpAffine(frame, M, (W, H),
                                       borderMode=cv2.BORDER_REFLECT)
                k = max(3, int((12 + 7 * e["intensity"]) * a))
                frame = (cv2.blur(frame, (k * 4, 1)) if dxy[0]
                         else cv2.blur(frame, (1, k * 4)))

        # crash-zoom blur burst
        for e in self.zblurs:
            a = env(t, e["start"], e["end"], 0.10, 0.14)
            if a > 0.05:
                k = max(3, int((8 + 5 * e["intensity"]) * a) | 1)
                frame = cv2.blur(frame, (k, k))

        # rgb split / glitch (cyberpunk)
        split_px = 0
        for e in self.splits:
            a = env(t, e["start"], e["end"], 0.06, 0.12)
            split_px = max(split_px, int(a * (2 + 1.4 * e["intensity"])
                                         * (H / 720.0)))
        glitch_a = 0.0
        for e in self.glitches:
            a = env(t, e["start"], e["end"], 0.05, 0.1)
            if a > glitch_a:
                glitch_a = a
                split_px = max(split_px,
                               int(a * (3 + 1.6 * e["intensity"])
                                   * (H / 720.0)))
        if split_px >= 1:
            frame[:, :, 2] = np.roll(frame[:, :, 2], split_px, axis=1)
            frame[:, :, 0] = np.roll(frame[:, :, 0], -split_px, axis=1)
        if glitch_a > 0.08:
            rng = np.random.default_rng(int(out_t * 997) & 0xFFFF)
            n_sl = 3 + int(glitch_a * 5)
            for _ in range(n_sl):
                y1 = int(rng.integers(0, H - 8))
                hh = int(rng.integers(4, max(6, H // 22)))
                off = int(rng.integers(-1, 2)
                          * rng.integers(4, max(6, int(28 * glitch_a
                                                       * H / 720))))
                if off:
                    frame[y1:y1 + hh] = np.roll(frame[y1:y1 + hh], off,
                                                axis=1)

        for e in self.grades:                      # cinematic film look
            a = env(t, e["start"], e["end"], 0.5, 0.8) * \
                (0.35 + 0.065 * e["intensity"])
            if a > 0.02:
                graded = cv2.LUT(frame, self.lut)
                graded = (graded.astype(np.float32) * self.vig)
                frame = cv2.addWeighted(graded.astype(np.uint8), a,
                                        frame, 1 - a, 0)

        for e in self.agrades:                     # Hollywood action look
            a = env(t, e["start"], e["end"], 0.4, 0.6) * \
                (0.40 + 0.06 * e["intensity"])
            if a > 0.02:
                graded = cv2.LUT(frame, self.action_lut)
                soft = cv2.GaussianBlur(graded, (0, 0), 2.2)
                graded = cv2.addWeighted(graded, 1.45, soft, -0.45, 0)
                frame = cv2.addWeighted(graded, a, frame, 1 - a, 0)

        fl = 0.0
        for e in self.flashes:
            fl = max(fl, env(t, e["start"], e["end"], 0.04, 0.12)
                     * (0.5 + 0.05 * e["intensity"]))
        if fl > 0.02:
            frame = cv2.addWeighted(frame, 1 - fl,
                                    np.full_like(frame, 255), fl, 0)

        # frame blocking: a dark blurred band sweeps across like a pass-by
        for e in self.fblocks:
            a = env(t, e["start"], e["end"], 0.05, 0.05)
            if a > 0.05:
                p = (t - e["start"]) / max(e["end"] - e["start"], 1e-6)
                bw = int(W * 0.45)
                cx = int(-bw + p * (W + 2 * bw))
                x1, x2 = max(0, cx - bw // 2), min(W, cx + bw // 2)
                if x2 > x1 + 4:
                    band = frame[:, x1:x2]
                    band = cv2.blur(band, (max(9, bw // 6) | 1, 9))
                    band = cv2.convertScaleAbs(band, alpha=0.35, beta=0)
                    frame[:, x1:x2] = band

        # light leak: warm organic flare washing across the frame
        for e in self.leaks:
            a = env(t, e["start"], e["end"], 0.25, 0.35) \
                * (0.5 + 0.05 * e["intensity"])
            if a > 0.02:
                p = (t - e["start"]) / max(e["end"] - e["start"], 1e-6)
                shift = int((p - 0.5) * 0.7 * W)
                leak = np.roll(self.leak, shift, axis=1)
                frame = cv2.add(frame, (leak * a).astype(np.uint8))

        # dip to black (chapter transitions)
        dk = 0.0
        for e in self.dips:
            dk = max(dk, env(t, e["start"], e["end"], 0.4, 0.4)
                     * (0.6 + 0.04 * e["intensity"]))
        if dk > 0.02:
            frame = cv2.convertScaleAbs(frame, alpha=1.0 - dk, beta=0)

        # film grain: organic monochromatic noise (event spans + global option)
        g = self.global_grain
        for e in self.grains:
            g = max(g, env(t, e["start"], e["end"], 0.8, 0.8)
                    * (0.05 + 0.011 * e["intensity"]))
        if g > 0.01:
            tile = self.grain_tiles[int(out_t * self.fps) % len(
                self.grain_tiles)]
            frame = cv2.addWeighted(frame, 1.0, tile, g, -128.0 * g)

        # split screen / multi-cam windowing (WIDE + tracked GRIP CAM)
        for e in self.screens:
            a = env(t, e["start"], e["end"], 0.4, 0.5)
            if a > 0.03:
                frame = self._split_screen(frame, frame_idx, a, transform)
                break

        # neon hand skeletons (after geometry — landmarks are re-projected)
        ga = self.glow_alpha(t)
        if ga > 0.02 and self.pres is not None:
            i = min(max(frame_idx, 0), len(self.pres) - 1)
            draw_hand_glow(frame, self.lms[i], self.pres[i], ga, out_t,
                           transform)

        # hand-following smart captions (re-projected through the zoom)
        if self.opts.get("draw_labels", True) and self.track is not None:
            x0, y0, sx, sy = transform
            for e in self.htexts:
                a = env(t, e["start"], e["end"], 0.3, 0.4)
                if a > 0.02:
                    gx, gy = self.grip(frame_idx)
                    draw_hand_text(frame, e["text"],
                                   (gx - x0) * sx, (gy - y0) * sy,
                                   a, W, H, out_t)

        # letterbox cinema bars
        for e in self.boxes:
            a = env(t, e["start"], e["end"], 0.6, 0.8)
            if a > 0.02:
                bh = int(0.10 * H * a * (0.6 + 0.04 * e["intensity"]))
                if bh > 0:
                    frame[:bh] = 0
                    frame[H - bh:] = 0

        if self.opts.get("draw_labels", True):
            for e in self.kinetics:
                a = env(t, e["start"], e["end"], 0.15, 0.35)
                if a > 0.02:
                    p = (t - e["start"]) / max(e["end"] - e["start"], 1e-6)
                    v = 0.0
                    if self.aud is not None and len(self.aud):
                        v = float(self.aud[min(max(frame_idx, 0),
                                               len(self.aud) - 1)])
                    draw_kinetic_text(frame, e["text"], p, v, a, W, H)
            for e in self.labels:
                a = env(t, e["start"], e["end"], 0.35, 0.45)
                if a > 0.02:
                    prog = (t - e["start"]) / max(e["end"] - e["start"], 1e-6)
                    draw_label(frame, e["text"] or "ARM WRESTLING", a, W, H,
                               prog)
            for e in self.titles:
                a = env(t, e["start"], e["end"], 0.3, 0.5)
                if a > 0.02:
                    prog = (t - e["start"]) / max(e["end"] - e["start"], 1e-6)
                    draw_title(frame, e["text"] or "ARM WRESTLING", a, W, H,
                               prog)
            if freeze_text:
                draw_title(frame, freeze_text, min(1.0, freeze_p * 4), W, H,
                           freeze_p)
        if self.opts.get("draw_hud", True):
            pulse = 0.7 + 0.3 * math.sin(out_t * 6.0)
            draw_hud(frame, t, self.opts.get("dur", 0), W, H, badge, pulse)
        return frame


# ----------------------------------------------------------------------------
# STEP 3 — single-pass renderer
# ----------------------------------------------------------------------------
def render(cfg, video: Path, plan: dict, tdata, log, progress=None) -> Path:
    ff = find_ffmpeg()
    cap = cv2.VideoCapture(str(video))
    if not cap.isOpened():
        raise RuntimeError("Could not open the video.")
    src_fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    src_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    src_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    n_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 1
    dur = n_frames / src_fps

    effects = list(plan["effects"])
    if cfg.get("auto_suggest", True) and tdata is not None:
        effects = auto_suggest(effects, tdata, src_fps, dur, log)
        effects = enrich_plan(effects, tdata, src_fps, dur, log)
        effects = validate_effects({"effects": effects}, dur, log)["effects"]

    W, H = src_w, src_h
    if cfg.get("cap_1080p", True) and src_h > 1080:
        W = int(round(src_w * 1080 / src_h / 2) * 2); H = 1080
    W -= W % 2; H -= H % 2

    voice_safe = bool(cfg.get("voice_safe", True))
    segs, freezes = build_speed_segments(effects, dur, voice_safe, log)
    if voice_safe:
        log("    🔒 voice-over safe: audio untouched, slow-mo rendered as "
            "speed ramps with ▶▶ catch-up")

    fx = FrameFX(W, H, effects, tdata, src_fps,
                 {"draw_hud": cfg.get("draw_hud", True),
                  "draw_labels": cfg.get("draw_labels", True),
                  "hand_fx_mode": (cfg.get("hand_fx_mode", "AI-decided")
                                   if tdata is not None else "Off"),
                  "global_grain": (0.055 if cfg.get("film_grain", True)
                                   else 0.0),
                  "dur": dur})

    silent = TMP_DIR / "render_silent.mp4"
    enc = pick_encoder(ff, log)
    cmd = [ff, "-y", "-hide_banner", "-loglevel", "error",
           "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{W}x{H}",
           "-r", f"{src_fps:.6f}", "-i", "-",
           "-an", *enc, "-pix_fmt", "yuv420p", str(silent)]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                            stderr=subprocess.PIPE)

    log(f"STEP 3 — rendering {W}x{H} @ {src_fps:.2f}fps, "
        f"{len(effects)} effects, {len(segs)} speed segments ...")

    def speed_at(t):
        for (a, b, s) in segs:
            if a <= t < b:
                return s
        return 1.0

    fz = list(freezes)
    acc, out_t, written, t0 = 0.0, 0.0, 0, time.time()
    resize_needed = (W != src_w or H != src_h)
    snaps, held = {}, None                    # dissolve snapshots, stutter
    try:
        for idx in range(n_frames):
            ok, frame = cap.read()
            if not ok:
                break
            t = idx / src_fps
            if resize_needed:
                frame = cv2.resize(frame, (W, H),
                                   interpolation=cv2.INTER_AREA)

            # jump cut: hold frames in bursts of 3 (stutter, no time change)
            if fx.stutter_active(t):
                if held is None or idx % 3 == 0:
                    held = frame
                else:
                    frame = held
            else:
                held = None

            while fz and fz[0][0] <= t:
                ft, hold, text = fz.pop(0)
                hold_frames = max(1, int(hold * src_fps))
                for k in range(hold_frames):
                    p = k / hold_frames
                    out = fx.apply(frame.copy(), t, out_t, idx, snaps,
                                   badge="FREEZE",
                                   freeze_text=text or "FREEZE", freeze_p=p)
                    proc.stdin.write(out.tobytes())
                    out_t += 1.0 / src_fps
                    written += 1

            s = speed_at(t)
            acc += 1.0 / s
            reps = int(acc)
            acc -= reps
            badge = None
            if s < 0.95:
                badge = "SLO-MO"
            elif s > 1.05 and voice_safe:
                badge = "▶▶"
            for _ in range(reps):
                out = fx.apply(frame.copy(), t, out_t, idx, snaps,
                               badge=badge)
                proc.stdin.write(out.tobytes())
                out_t += 1.0 / src_fps
                written += 1

            if idx % 300 == 0:
                if progress:
                    progress(idx / n_frames)
                if idx % 1500 == 0 and idx:
                    log(f"    {idx}/{n_frames} src frames "
                        f"({idx/max(time.time()-t0, 1e-6):.0f} fps)")
    finally:
        cap.release()
        try:
            proc.stdin.close()
        except Exception:
            pass
        proc.wait()
    if proc.returncode != 0:
        err = (proc.stderr.read() or b"").decode(errors="ignore")[-400:]
        raise RuntimeError(f"ffmpeg encode failed: {err}")
    log(f"    ✔ {written} frames encoded in {time.time()-t0:.1f}s")
    if progress:
        progress(1.0)

    # ---------------- audio ----------------
    stamp = time.strftime("%Y%m%d_%H%M%S")
    final = OUT_DIR / f"{video.stem}_FX_{stamp}.mp4"
    if not has_audio_stream(ff, video):
        log("    source has no audio — muxing video only")
        shutil.move(str(silent), str(final))
        return final

    if voice_safe:
        # net timeline change is zero -> just mux the ORIGINAL audio
        log("    muxing the untouched original audio (voice-over safe) ...")
        r = subprocess.run(
            [ff, "-y", "-hide_banner", "-loglevel", "error",
             "-i", str(silent), "-i", str(video),
             "-map", "0:v", "-map", "1:a",
             "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
             "-shortest", str(final)],
            capture_output=True, text=True)
    else:
        ops = build_audio_ops(segs, freezes)
        parts, labels = [], []
        for i, op in enumerate(ops):
            if op[0] == "src":
                _, a, b, s = op
                chain = f",{atempo_chain(s)}" if abs(s - 1.0) > 1e-3 else ""
                parts.append(f"[1:a]atrim={a:.3f}:{b:.3f},"
                             f"asetpts=PTS-STARTPTS{chain}[a{i}]")
            else:
                parts.append(f"anullsrc=r=48000:cl=stereo,"
                             f"atrim=0:{op[1]:.3f},"
                             f"asetpts=PTS-STARTPTS[a{i}]")
            labels.append(f"[a{i}]")
        graph = ";".join(parts) + ";" + "".join(labels) + \
            f"concat=n={len(labels)}:v=0:a=1[aout]"
        log("    muxing tempo-matched audio ...")
        r = subprocess.run(
            [ff, "-y", "-hide_banner", "-loglevel", "error",
             "-i", str(silent), "-i", str(video),
             "-filter_complex", graph,
             "-map", "0:v", "-map", "[aout]",
             "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
             "-shortest", str(final)],
            capture_output=True, text=True)

    if r.returncode != 0 or not final.exists():
        log(f"    ⚠ audio mux failed ({(r.stderr or '')[-200:]}) — "
            f"delivering video with original audio track")
        subprocess.run([ff, "-y", "-loglevel", "error", "-i", str(silent),
                        "-i", str(video), "-map", "0:v", "-map", "1:a?",
                        "-c", "copy", "-shortest", str(final)],
                       capture_output=True, text=True)
        if not final.exists():
            shutil.move(str(silent), str(final))
    try:
        silent.unlink(missing_ok=True)
    except Exception:
        pass
    return final
