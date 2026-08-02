"""AUTO-PORTED from script_maker.py - GUI stripped, paths repointed to the Modal volume.
Everything below is your original logic, unchanged, except: the tkinter imports
were removed and the on-disk directory constants now point at the cloud volume.
"""
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ARM WRESTLING SCRIPT MAKER  —  Gemini-powered YouTube script generator
======================================================================
Feed it a reference .txt file of arm wrestling data / analyses / old scripts.
It asks Gemini to either:
  (A) generate a list of fresh topic titles for you to pick from, then write
      an ~800-word (or your chosen length) script on the chosen title, OR
  (B) write a script directly on a title you type yourself.

The finished script is shown in the app AND saved as a .txt file on disk.

The app remembers your API key, word count, reference file, and last models
between runs (./app_data/config.json).

Requirements:
    pip install google-genai
"""

import os
import re
import json
import time
import queue
import threading
import traceback
import datetime
from pathlib import Path

# ----------------------------------------------------------------------------
# Paths / persistence
# ----------------------------------------------------------------------------
DATA        = Path(os.environ.get("ARM_DATA", "/data"))
APP_DIR     = DATA / "script" / "app_data"
CONFIG_FILE = APP_DIR / "config.json"
OUT_DIR     = DATA / "script" / "scripts_output"

for d in (APP_DIR, OUT_DIR):
    d.mkdir(parents=True, exist_ok=True)

DEFAULT_CONFIG = {
    "api_key": "",
    "ref_path": "",
    "word_count": 800,
    "model": "gemini-2.5-flash",
    "mode": "ai_suggest",   # "ai_suggest" or "own_title"
    "max_ref_chars": 0,     # 0 = send whole file; >0 = send only first N chars
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
# The backend master prompt (your exact instructions)
# ----------------------------------------------------------------------------
MASTER_PROMPT = """Role: You are an expert YouTube scriptwriter and elite-level armwrestling analyst.
Context: I am providing you with a text file containing raw data, match analyses, and previous scripts focused on professional armwrestling. I need you to ingest and analyze every single word of this document to generate fresh content.
Task:

1. Topic Generation: Based strictly on the provided file, suggest 3-5 fresh, highly engaging YouTube video topics that dive deep into advanced techniques.
2. Scriptwriting: Choose the strongest topic from your suggestions and write a comprehensive, {word_count}-word YouTube video script.
Hook & Retention (CRITICAL):

* The Hook: Open with a killer, laugh-out-loud funny hook that instantly grabs the viewer by the shirt. It needs to be bold, unexpected, and set the street-smart tone immediately.
* High Retention: Keep viewers glued to the screen by using "open loops" (teasing a massive technical secret early on but revealing it later). Use bold statements, rapid-fire pacing, and talk directly to the audience (e.g., telling them to imagine the pressure on their own wrist).
Content & Technical Focus:

* The 75/25 Rule: The script must be exactly 75% raw table technique and 25% scientific/biomechanical evidence.
* Break down the gritty mechanics on the pad—explain exactly how a technique is executed (e.g., ripping through someone's pronation, locking in static containment, or surviving in a king's move) before backing it up with just enough sports science to prove why it works.
* Pull all factual data and technical insights directly from the provided text file.
Tone & Voice:

* Vibe: High-energy, highly engaging, and raw.
* Language: Use funny, street-smart, conversational language. Blend elite technical analysis with gritty, relatable humor. Talk to the audience like you're hyping up a crowd at a live supermatch.
Strict Formatting Constraints:

* NO timestamps.
* NO headlines or subheadings.
* NO bullet points or numbered lists.
* The script must flow as one continuous, uninterrupted spoken narrative from start to finish.
"""


# ----------------------------------------------------------------------------
# Gemini helpers (same wiring style as the reference pipeline)
# ----------------------------------------------------------------------------
def make_client(api_key: str):
    try:
        from google import genai  # google-genai SDK
    except ImportError:
        raise RuntimeError("Missing SDK. Run:  pip install google-genai")
    if not api_key.strip():
        raise RuntimeError("Enter your Gemini API key first.")
    return genai.Client(api_key=api_key.strip())


def read_reference(ref_path: str, max_chars: int = 0) -> str:
    p = Path(ref_path)
    if not p.exists():
        raise RuntimeError("Reference .txt file not found. Load it first.")
    txt = p.read_text(encoding="utf-8", errors="ignore").strip()
    if not txt:
        raise RuntimeError("The reference file is empty.")
    if max_chars and len(txt) > max_chars:
        txt = txt[:max_chars]
    return txt


def _parse_retry_delay(msg: str) -> float:
    """Pull the server's suggested retry delay (seconds) out of a 429 message."""
    m = re.search(r"retry\D*?(\d+(?:\.\d+)?)\s*s", msg, re.I)
    if m:
        return float(m.group(1))
    m = re.search(r"retryDelay['\"]?\s*[:=]\s*['\"]?(\d+(?:\.\d+)?)", msg, re.I)
    return float(m.group(1)) if m else 30.0


def _is_daily_quota(msg: str) -> bool:
    m = msg.lower().replace("_", "").replace(" ", "")
    return "perday" in m or "requestsperday" in m or "freetierrequests" in m


def gen_text(client, model: str, prompt: str, system_instruction: str = "",
             log=None, max_retries: int = 5) -> str:
    """Plain text generation call with automatic wait-and-retry on 429 rate limits."""
    from google.genai import types
    cfg_kwargs = dict(temperature=0.9)
    if system_instruction:
        cfg_kwargs["system_instruction"] = system_instruction
    config = types.GenerateContentConfig(**cfg_kwargs)

    for attempt in range(1, max_retries + 1):
        try:
            resp = client.models.generate_content(
                model=model, contents=prompt, config=config)
            return (resp.text or "").strip()
        except Exception as e:
            msg = str(e)
            is_429 = "429" in msg or "RESOURCE_EXHAUSTED" in msg or "quota" in msg.lower()
            if not is_429:
                raise
            if _is_daily_quota(msg):
                raise RuntimeError(
                    "Your DAILY free-tier quota for this model is used up. "
                    "Try again tomorrow, switch models, or enable billing on your "
                    "Google AI Studio key.")
            if attempt == max_retries:
                raise RuntimeError(
                    "Rate limit did not clear after several retries. The free tier "
                    "allows ~250k input tokens per minute; your reference file is large. "
                    "Wait a minute and try again, use a smaller reference file, or "
                    "enable billing.")
            wait = _parse_retry_delay(msg) + 2.0
            if log:
                log(f"    ⏳ Rate limit hit — waiting {wait:.0f}s then retrying "
                    f"(attempt {attempt}/{max_retries}) ...")
            time.sleep(wait)
    raise RuntimeError("Generation failed after retries.")


def gen_titles(client, model: str, reference: str, word_count: int, log) -> list[str]:
    """Ask Gemini for a list of fresh topic titles derived from the reference file."""
    log("Asking the AI to suggest fresh topics ...")
    prompt = (
        MASTER_PROMPT.format(word_count=word_count)
        + "\n\n=== DO ONLY THIS STEP RIGHT NOW ===\n"
        "Do NOT write the script yet. First, based STRICTLY on the reference file below, "
        "return 3-5 fresh, highly engaging YouTube video titles that dive deep into advanced "
        "armwrestling techniques. Return ONLY the titles, one per line, with no numbering, "
        "no quotes, no extra commentary.\n\n"
        "=== REFERENCE FILE ===\n" + reference
    )
    raw = gen_text(client, model, prompt, log=log)
    titles = []
    for line in raw.splitlines():
        line = line.strip()
        # strip leading list markers / numbering / bullets / quotes
        line = re.sub(r'^\s*(?:\d+[\.\)]|[-*•])\s*', '', line).strip().strip('"').strip("'")
        if line:
            titles.append(line)
    if not titles:
        raise RuntimeError("The AI did not return any usable titles. Try again.")
    return titles[:5]


def gen_script(client, model: str, reference: str, title: str, word_count: int, log) -> str:
    """Write the full script for a chosen title, following the master prompt."""
    log(f"Writing the script for: {title}")
    prompt = (
        MASTER_PROMPT.format(word_count=word_count)
        + "\n\n=== DO ONLY THIS STEP RIGHT NOW ===\n"
        f"Write the full ~{word_count}-word YouTube script for this exact chosen title:\n"
        f'"{title}"\n\n'
        "Follow every rule above: the funny street-smart hook, open loops, the 75/25 rule, "
        "and the strict formatting constraints (no timestamps, no headings, no bullets, one "
        "continuous spoken narrative). Return ONLY the script text itself — nothing else.\n\n"
        "=== REFERENCE FILE ===\n" + reference
    )
    script = gen_text(client, model, prompt, log=log)
    if not script:
        raise RuntimeError("The AI returned an empty script. Try again.")
    return script


def slugify(text: str, maxlen: int = 50) -> str:
    text = re.sub(r'[^\w\s-]', '', text).strip().lower()
    text = re.sub(r'[\s_-]+', '_', text)
    return (text[:maxlen] or "script").strip("_")


def save_script(title: str, script: str) -> Path:
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    fname = f"{slugify(title)}_{stamp}.txt"
    out = OUT_DIR / fname
    header = f"TITLE: {title}\n{'=' * 60}\n\n"
    out.write_text(header + script, encoding="utf-8")
    return out

