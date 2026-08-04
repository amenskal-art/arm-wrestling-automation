#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ARM PIPELINE — the whole 4-stage factory, running on Modal, driven from a phone.

    Stage 1  script     script_maker.py      -> Gemini writes the script
    Stage 2  voice      tts_modal.py         -> Qwen3-TTS clones your voice (GPU)
    Stage 3  cut        arm_video_maker.py   -> 3-AI pipeline builds final.mp4
    Stage 4  fx         arm_video_editor.py  -> AI-directed effects pass

Nothing was rewritten. core/*.py holds your original Python with the tkinter
GUI removed and the folder constants pointed at a Modal Volume, so every cache
your scripts already keep (video analyses, transcripts, hand tracking, voice
prompts, model weights) survives between runs and between phones.

Deploy:   modal deploy modal_app.py
Then open the printed URL on your phone and add it to your home screen.
"""

import os
import time
import json
import uuid
import shutil
from pathlib import Path

import modal

APP_NAME = "arm-pipeline"
app = modal.App(APP_NAME)

# ---------------------------------------------------------------------------
# Persistent storage — created automatically on your Modal account, first run
# ---------------------------------------------------------------------------
# Everything that must survive: config, all caches, all outputs.
data_vol = modal.Volume.from_name("arm-pipeline-data", create_if_missing=True)
# Qwen3-TTS weights (several GB, downloaded once, then free forever).
tts_models = modal.Volume.from_name("arm-tts-model-cache", create_if_missing=True)
# Memorised voice-clone prompts, keyed by voice fingerprint.
voice_cache = modal.Volume.from_name("arm-tts-voice-cache", create_if_missing=True)
# Live job status + streaming logs, so the phone can watch a running job.
jobs = modal.Dict.from_name("arm-pipeline-jobs", create_if_missing=True)

DATA = Path("/data")
VOL = {"/data": data_vol}

# ---------------------------------------------------------------------------
# Images — one per stage so no container installs more than it needs
# ---------------------------------------------------------------------------
GEMINI = ["google-genai>=1.0.0", "pydantic>=2.0"]

base_image = modal.Image.debian_slim(python_version="3.12")

script_image = (
    base_image.pip_install(*GEMINI)
    .env({"ARM_DATA": "/data"})
    .add_local_python_source("core")
)

maker_image = (
    base_image.apt_install("ffmpeg")
    .pip_install(*GEMINI, "yt-dlp")
    .env({"ARM_DATA": "/data", "ARM_SCRATCH": "/tmp/arm_scratch"})
    .add_local_python_source("core")
)

editor_image = (
    base_image.apt_install("ffmpeg", "libgl1", "libglib2.0-0")
    .pip_install(*GEMINI, "numpy<2.0", "opencv-python-headless", "mediapipe")
    .env({"ARM_DATA": "/data", "ARM_SCRATCH": "/tmp/arm_scratch"})
    .add_local_python_source("core")
)

# Straight from your tts_modal.py — same torch build, same model.
tts_image = (
    modal.Image.debian_slim(python_version="3.12")
    .apt_install("git", "ffmpeg", "libsndfile1")
    .pip_install(
        "torch==2.6.0", "torchaudio==2.6.0",
        index_url="https://download.pytorch.org/whl/cu124",
    )
    .pip_install("qwen-tts", "soundfile", "numpy")
    .env({"CACHE_BUST": "qwen3tts_v1", "ARM_DATA": "/data"})
    .add_local_python_source("core")
)

# The web container serves the analysis plan, which means it reads the real
# prompt, schema and cache paths out of maker_core. Without the source and
# pydantic it cannot import them, and every request to those endpoints fails.
web_image = (
    base_image.pip_install("fastapi[standard]", "pydantic>=2.0")
    .env({"ARM_DATA": "/data"})
    .add_local_dir("web", remote_path="/assets")
    .add_local_python_source("core")
)

GPU_TYPE = "A10G"          # bump to "L40S" or "A100" for faster long scripts

CONFIG_PATH = DATA / "config.json"

DEFAULT_CONFIG = {
    # typed once, remembered forever
    "api_key": "",
    "links": [],
    "ref_text_path": "",        # the arm-wrestling knowledge .txt
    "voice_ref_path": "",       # 3-second reference voice clip
    "voice_ref_text": "",       # exact transcript of that clip
    "cookies_file": "",         # optional youtube cookies.txt
    # stage 1
    "word_count": 800,
    "model": "gemini-2.5-flash",
    "max_ref_chars": 0,
    # stage 2
    "language": "Auto",
    # stage 3
    "model_audio": "gemini-3.1-flash-lite",
    "model_vision": "gemini-3.1-flash-lite",
    "model_match": "gemini-3.5-flash",
    "min_height": 720,
    "max_scene_len": 6.0,
    "max_extension": 2.0,
    "max_scene_uses": 2,
    "low_res_over_minutes": 20,
    "delete_originals": True,
    "analyze_workers": 8,
    "phone_workers": 2,
    # stage 4
    "model_fx": "gemini-3.5-flash",
    "hand_fx_mode": "AI-decided",
    "voice_safe": True,
    "auto_suggest": True,
    "film_grain": True,
    "draw_hud": True,
    "draw_labels": True,
    "cap_1080p": True,
    # ui
    "password_hash": "",
    "session_secret": "",
}


# ---------------------------------------------------------------------------
# Config + job-log helpers (shared by every container)
# ---------------------------------------------------------------------------
def load_cfg() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    try:
        data_vol.reload()
    except Exception:
        pass
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
        except Exception:
            pass
    return cfg


def save_cfg(cfg: dict):
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    try:
        data_vol.commit()
    except Exception:
        pass


def job_logger(job_id: str, stage: str):
    """Returns a log(msg) callable that streams into the shared job Dict.

    Guarded by a lock because the analysis stage logs from several worker
    threads at once, and an unsynchronised list would drop or duplicate lines.
    """
    import threading
    buf, last, lock = [], [0.0], threading.Lock()

    def flush(status=None, **extra):
      with lock:
        rec = jobs.get(job_id) or {"stage": stage, "log": [], "started": time.time()}
        rec.setdefault("log", [])
        if buf:
            rec["log"].extend(buf)
            buf.clear()
        rec["log"] = rec["log"][-600:]
        if status:
            rec["status"] = status
        rec.update(extra)
        rec["updated"] = time.time()
        jobs[job_id] = rec
        last[0] = time.time()

    def log(msg):
        print(msg)
        with lock:
            buf.append(str(msg))
            due = len(buf) >= 15 or time.time() - last[0] > 1.5
        if due:
            flush()

    log.flush = flush
    return log


def rel(p) -> str:
    """Path on the volume -> browser-safe relative path."""
    return str(Path(p)).replace("/data/", "", 1).lstrip("/")


def set_phase(job_id: str, phase: str):
    rec = jobs.get(job_id) or {}
    rec["phase"] = phase
    jobs[job_id] = rec


def is_cancelled(job_id: str) -> bool:
    return (jobs.get(job_id) or {}).get("status") == "cancelled"


def track_child(job_id: str, call_id: str):
    """Remember spawned sub-calls so Stop can reach them too."""
    rec = jobs.get(job_id) or {}
    rec.setdefault("children", []).append(call_id)
    jobs[job_id] = rec


def finish(log, job_id, final: bool = True, **result):
    """final=False when this stage is one link in the full-run chain: record the
    result, but leave the job 'running' so the phone keeps watching."""
    if final:
        log.flush(status="done", result=result, finished=time.time())
    else:
        log.flush(result=result)
    try:
        data_vol.commit()
    except Exception:
        pass
    return result


def fail(log, job_id, exc):
    import traceback
    log(f"\nERROR: {exc}")
    log(traceback.format_exc(limit=4))
    log.flush(status="failed", error=str(exc), finished=time.time())
    raise


# ---------------------------------------------------------------------------
# STAGE 1 — script maker
# ---------------------------------------------------------------------------
@app.function(image=script_image, volumes=VOL, timeout=1800)
def stage_titles(job_id: str, final: bool = True):
    from core import script_core as S
    log = job_logger(job_id, "titles")
    log.flush(status="running")
    try:
        cfg = load_cfg()
        client = S.make_client(cfg["api_key"])
        ref = S.read_reference(cfg["ref_text_path"], cfg.get("max_ref_chars", 0))
        log(f"Reference loaded ({len(ref):,} characters).")
        titles = S.gen_titles(client, cfg["model"], ref, cfg["word_count"], log)
        for t in titles:
            log("  - " + t)
        return finish(log, job_id, final, titles=titles)
    except Exception as e:
        fail(log, job_id, e)


@app.function(image=script_image, volumes=VOL, timeout=1800)
def stage_script(job_id: str, title: str, final: bool = True):
    from core import script_core as S
    log = job_logger(job_id, "script")
    log.flush(status="running")
    try:
        cfg = load_cfg()
        client = S.make_client(cfg["api_key"])
        ref = S.read_reference(cfg["ref_text_path"], cfg.get("max_ref_chars", 0))
        script = S.gen_script(client, cfg["model"], ref, title, cfg["word_count"], log)
        out = S.save_script(title, script)
        log(f"Saved: {out}")
        cfg["last_script_path"] = str(out)
        cfg["last_title"] = title
        save_cfg(cfg)
        return finish(log, job_id, final, path=rel(out), title=title, text=script)
    except Exception as e:
        fail(log, job_id, e)


# ---------------------------------------------------------------------------
# STAGE 2 — Qwen3-TTS voice clone (GPU)
# ---------------------------------------------------------------------------
@app.function(
    image=tts_image,
    gpu=GPU_TYPE,
    timeout=3600,
    volumes={"/data": data_vol, "/cache": tts_models, "/voices": voice_cache},
)
def stage_voice(job_id: str, script_text: str = "", final: bool = True):
    from core import tts_core as T
    log = job_logger(job_id, "voice")
    log.flush(status="running")
    try:
        os.environ["HF_HOME"] = "/cache/huggingface"
        import torch
        import numpy as np
        import soundfile as sf
        from qwen_tts import Qwen3TTSModel

        cfg = load_cfg()
        text = (script_text or "").strip()
        if not text:
            sp = cfg.get("last_script_path", "")
            if not sp or not Path(sp).exists():
                raise RuntimeError("No script to speak. Run stage 1 first.")
            raw = Path(sp).read_text(encoding="utf-8")
            text = raw.split("=" * 60, 1)[-1].strip() or raw   # drop the TITLE header

        ref_path = cfg.get("voice_ref_path", "")
        ref_text = (cfg.get("voice_ref_text") or "").strip()
        if not ref_path or not Path(ref_path).exists():
            raise RuntimeError("Upload a reference voice clip in Settings first.")
        audio_bytes = Path(ref_path).read_bytes()
        voice_id = T.compute_voice_id(audio_bytes, ref_text)
        log(f"Voice fingerprint: {voice_id}")

        model = Qwen3TTSModel.from_pretrained(
            T.MODEL_ID, device_map="cuda:0", dtype=torch.bfloat16,
            attn_implementation="sdpa",
        )
        try:
            tts_models.commit()
        except Exception:
            pass

        voice_cache.reload()
        prompt_path = f"/voices/{voice_id}.pt"
        cache_hit = os.path.exists(prompt_path)
        if cache_hit:
            log("Voice cache HIT — skipping the expensive cloning step.")
            voice_clone_prompt = torch.load(prompt_path, weights_only=False,
                                            map_location="cpu")
        else:
            if not ref_text:
                raise RuntimeError(
                    "New voice: also type the exact transcript of the reference clip.")
            log("Voice cache MISS — cloning once, then memorising it forever.")
            voice_clone_prompt = model.create_voice_clone_prompt(
                ref_audio=ref_path, ref_text=ref_text, x_vector_only_mode=False)
            torch.save(voice_clone_prompt, prompt_path)
            voice_cache.commit()

        chunks = T._split_script(text)
        log(f"{len(text)} chars -> {len(chunks)} chunks")
        all_wavs, sr = [], None
        for i in range(0, len(chunks), T.BATCH_SIZE):
            batch = chunks[i:i + T.BATCH_SIZE]
            log(f"speaking chunks {i + 1}-{i + len(batch)} / {len(chunks)}")
            wavs, sr = model.generate_voice_clone(
                text=batch if len(batch) > 1 else batch[0],
                language=[cfg["language"]] * len(batch) if len(batch) > 1 else cfg["language"],
                voice_clone_prompt=voice_clone_prompt,
                max_new_tokens=2048,
            )
            for w in wavs:
                all_wavs.append(np.asarray(w).reshape(-1))

        gap = np.zeros(int(sr * T.GAP_SECONDS), dtype=all_wavs[0].dtype)
        pieces = []
        for j, w in enumerate(all_wavs):
            pieces.append(w)
            if j != len(all_wavs) - 1:
                pieces.append(gap)
        # NOTE: do not name this `final` - that is the parameter that tells
        # finish() whether this stage ends the job. Shadowing it with a numpy
        # array made `if final:` raise on an ambiguous truth value.
        audio = np.concatenate(pieces)

        out_dir = DATA / "voice"
        out_dir.mkdir(parents=True, exist_ok=True)
        out = out_dir / f"voiceover_{time.strftime('%Y%m%d_%H%M%S')}.wav"
        sf.write(str(out), audio, sr)
        seconds = round(len(audio) / sr, 1)
        log(f"{seconds}s of audio -> {out}")

        cfg["last_voice_path"] = str(out)
        save_cfg(cfg)
        return finish(log, job_id, final, path=rel(out), seconds=seconds,
                      chunks=len(chunks), cache_hit=cache_hit)
    except Exception as e:
        fail(log, job_id, e)


# ---------------------------------------------------------------------------
# STAGE 3a — analysis only. Deliberately CHEAP.
# ---------------------------------------------------------------------------
# Watching videos is almost entirely waiting on Gemini, and Modal bills the
# resources you reserve for as long as you hold them — a blocked socket costs
# exactly as much as a busy core. Running this phase on 2 cores instead of the
# render's 8 cuts the price of the longest part of a run by about four times.
# A container that only waits on HTTP needs almost no machine. A quarter of a
# core is plenty, and Modal bills what you reserve, so this is where the money
# actually goes.
@app.function(image=maker_image, volumes=VOL, timeout=21600, cpu=0.25, memory=2048)
def stage_analyze(job_id: str, final: bool = True):
    """Watches every source video, concurrently.

    Gemini's per-minute limit caps how often a request may START, not how many
    may be running. Forty analyses that each take minutes can therefore overlap:
    starts stay paced by _throttle, and total time collapses from the sum of all
    of them to roughly one of them.
    """
    from core import maker_core as M
    from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED

    log = job_logger(job_id, "analyze")
    log.flush(status="running")
    try:
        cfg = load_cfg()
        links = [u.strip() for u in cfg.get("links", []) if u.strip()]
        if not links:
            raise RuntimeError("Add at least one video link in Settings.")

        run_cfg = dict(M.DEFAULT_CONFIG)
        run_cfg.update({
            "api_key": cfg["api_key"],
            "model_vision": cfg["model_vision"],
            "cookies_file": cfg.get("cookies_file", ""),
            "min_height": cfg["min_height"],
            "low_res_over_minutes": cfg["low_res_over_minutes"],
        })

        todo = [u for u in links if not (M.CACHE_DIR / f"{M.url_key(u)}.json").exists()]
        cached = len(links) - len(todo)
        log(f"{len(links)} link(s): {cached} already analysed, {len(todo)} to do.")
        if not todo:
            log("Everything is cached. Nothing to pay for.")
            return finish(log, job_id, final, analysed=0, cached=cached)

        pace = M._MODEL_INTERVAL.get(cfg["model_vision"], 13.0)
        workers = max(1, min(int(cfg.get("analyze_workers", 8)), len(todo)))
        log(f"{cfg['model_vision']}: about {60 / pace:.0f} starts a minute. "
            f"Running {workers} at a time, so the last one begins after roughly "
            f"{len(todo) * pace / 60:.0f} min rather than after all the others "
            f"have finished.")

        client = M.make_client(run_cfg["api_key"])
        done, failed = 0, []
        t0 = time.time()

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(M.analyze_video, client, u, run_cfg, log): u
                       for u in todo}
            pending = set(futures)
            while pending:
                finished, pending = wait(pending, timeout=30,
                                         return_when=FIRST_COMPLETED)
                for f in finished:
                    url = futures[f]
                    try:
                        f.result()
                        done += 1
                    except Exception as e:
                        failed.append(url)
                        log(f"  x {url}\n      -> {str(e)[:200]}")
                if finished:
                    # Commit as results land: a crash never loses analysed videos.
                    try:
                        data_vol.commit()
                    except Exception:
                        pass
                    log(f"  [{done + len(failed)}/{len(todo)}] complete, "
                        f"{len(pending)} still running "
                        f"({time.time() - t0:.0f}s elapsed)")
                else:
                    # Heartbeat, so a long silent stretch never looks like a hang.
                    log(f"    ...{len(pending)} video(s) in flight "
                        f"({time.time() - t0:.0f}s elapsed)")
                if is_cancelled(job_id):
                    log("Stop requested — finishing the ones already started.")
                    for f in pending:
                        f.cancel()
                    break

        try:
            data_vol.commit()
        except Exception:
            pass
        log(f"Analysed {done}, cached {cached}, failed {len(failed)}, "
            f"in {time.time() - t0:.0f}s.")
        return finish(log, job_id, final, analysed=done, cached=cached,
                      failed=len(failed))
    except Exception as e:
        fail(log, job_id, e)


# ---------------------------------------------------------------------------
# STAGE 3 — video maker (3-AI pipeline + ffmpeg render)
# ---------------------------------------------------------------------------
@app.function(image=maker_image, volumes=VOL, timeout=7200, cpu=8.0, memory=16384)
def stage_cut(job_id: str, final: bool = True):
    from core import maker_core as M
    log = job_logger(job_id, "cut")
    log.flush(status="running")
    try:
        cfg = load_cfg()
        voice = cfg.get("last_voice_path", "")
        if not voice or not Path(voice).exists():
            raise RuntimeError("No voice-over found. Run stage 2 first.")
        links = [u.strip() for u in cfg.get("links", []) if u.strip()]
        if not links:
            raise RuntimeError("Add at least one video link in Settings.")

        run_cfg = dict(M.DEFAULT_CONFIG)
        run_cfg.update({
            "api_key": cfg["api_key"],
            "mp3_path": voice,
            "links": links,
            "cookies_file": cfg.get("cookies_file", ""),
            "model_audio": cfg["model_audio"],
            "model_vision": cfg["model_vision"],
            "model_match": cfg["model_match"],
            "min_height": cfg["min_height"],
            "max_scene_len": cfg["max_scene_len"],
            "max_extension": cfg["max_extension"],
            "max_scene_uses": cfg["max_scene_uses"],
            "low_res_over_minutes": cfg["low_res_over_minutes"],
            "delete_originals": cfg["delete_originals"],
        })
        # Pre-warm the analysis cache on the 2-core container rather than
        # holding these 8 cores idle while Gemini watches 40 videos.
        missing = [u for u in links
                   if not (M.CACHE_DIR / f"{M.url_key(u)}.json").exists()]
        if missing:
            log(f"{len(missing)} link(s) not analysed yet — doing that on a "
                f"small machine first.")
            stage_analyze.remote(job_id, final=False)
            data_vol.reload()

        log(f"{len(links)} source link(s). Cached analyses are reused, never redone.")
        out = M.run_pipeline(run_cfg, log)
        log(f"Cut video ready: {out}")
        cfg["last_cut_path"] = str(out)
        save_cfg(cfg)
        return finish(log, job_id, final, path=rel(out))
    except Exception as e:
        fail(log, job_id, e)


# ---------------------------------------------------------------------------
# STAGE 4 — FX editor
# ---------------------------------------------------------------------------
# Stage 4 is the heaviest: OpenCV touches every frame. Modal bills physical
# cores, and the soft ceiling is your request + 16, so 16 requested cores means
# the render can burst to ~32 without throttling. Nothing here needs CUDA.
@app.function(image=editor_image, volumes=VOL, timeout=7200, cpu=16.0, memory=32768)
def stage_fx(job_id: str, video_rel: str = "", final: bool = True):
    from core import editor_core as E
    log = job_logger(job_id, "fx")
    log.flush(status="running")
    try:
        cfg = load_cfg()
        video = Path("/data") / video_rel if video_rel else Path(cfg.get("last_cut_path", ""))
        if not video.exists():
            raise RuntimeError("No video to edit. Run stage 3 first, or pick a file.")

        run_cfg = dict(E.DEFAULT_CONFIG)
        run_cfg.update({
            "api_key": cfg["api_key"],
            "model_vision": cfg["model_fx"],
            "hand_fx_mode": cfg["hand_fx_mode"],
            "voice_safe": cfg["voice_safe"],
            "auto_suggest": cfg["auto_suggest"],
            "film_grain": cfg["film_grain"],
            "draw_hud": cfg["draw_hud"],
            "draw_labels": cfg["draw_labels"],
            "cap_1080p": cfg["cap_1080p"],
            "low_res_over_minutes": cfg["low_res_over_minutes"],
        })
        log(f"Editing {video.name}")
        plan = E.analyze_video(run_cfg, video, log)
        log(f"{len(plan.get('effects', []))} effects planned.")
        need_track = run_cfg["hand_fx_mode"] != "Off" or run_cfg["auto_suggest"]
        tdata = E.track_hands(video, log) if need_track else None

        def progress(p):
            rec = jobs.get(job_id) or {}
            rec["progress"] = round(float(p), 3)
            jobs[job_id] = rec

        out = E.render(run_cfg, video, plan, tdata, log, progress=progress)
        log(f"Final video: {out}")
        cfg["last_fx_path"] = str(out)
        save_cfg(cfg)
        return finish(log, job_id, final, path=rel(out))
    except Exception as e:
        fail(log, job_id, e)


# ---------------------------------------------------------------------------
# RUN EVERYTHING — one tap, script -> voice -> cut -> fx, no further input
# ---------------------------------------------------------------------------
@app.function(image=base_image.env({"ARM_DATA": "/data"}), volumes=VOL, timeout=21600)
def stage_all(job_id: str, title: str = ""):
    """Runs the whole factory unattended. Each phase is spawned so Stop can
    reach it, and the chain checks for a stop request between phases."""
    log = job_logger(job_id, "all")
    log.flush(status="running", phase="script")

    def phase(name, fn, *args):
        set_phase(job_id, name)
        call = fn.spawn(*args, final=False)
        track_child(job_id, call.object_id)
        return call.get()

    class Stopped(Exception):
        pass

    def check():
        if is_cancelled(job_id):
            raise Stopped()

    try:
        chosen = (title or "").strip()
        if not chosen:
            # Nothing to instruct: pick the topic itself and keep going.
            log("=== 0/4  no title given, choosing a topic ===")
            r0 = phase("script", stage_titles, job_id)
            chosen = (r0.get("titles") or [""])[0]
            if not chosen:
                raise RuntimeError("Gemini returned no usable topics.")
            log(f"Topic chosen automatically: {chosen}")
        check()

        log("=== 1/4  writing the script ===")
        r1 = phase("script", stage_script, job_id, chosen)
        check()

        log("=== 2/4  cloning your voice ===")
        phase("voice", stage_voice, job_id, r1["text"])
        check()

        log("=== 3/4  watching the source videos ===")
        phase("cut", stage_analyze, job_id)
        check()

        log("=== 3/4  building the cut ===")
        phase("cut", stage_cut, job_id)
        check()

        log("=== 4/4  adding the effects ===")
        r4 = phase("fx", stage_fx, job_id, "")

        set_phase(job_id, "fx")
        log(f"Whole pipeline finished: {r4['path']}")
        return finish(log, job_id, True, path=r4["path"], title=chosen)
    except Stopped:
        log("\nStopped on request.")
        log.flush(status="cancelled", finished=time.time())
    except Exception as e:
        fail(log, job_id, e)


# ---------------------------------------------------------------------------
# Small utility functions the web app calls directly
# ---------------------------------------------------------------------------
@app.function(image=base_image, volumes={"/voices": voice_cache}, timeout=120)
def list_voices() -> list:
    import glob
    voice_cache.reload()
    return [
        {"voice_id": Path(p).stem, "size_mb": round(Path(p).stat().st_size / 1e6, 2)}
        for p in sorted(glob.glob("/voices/*.pt"))
    ]


@app.function(image=base_image, volumes=VOL, timeout=120)
def reset_password() -> str:
    """Forgets the backend password so the next connect claims it fresh.

    Everything else survives: your Gemini key, links, every cache, every
    rendered file. Only the lock is removed. Any device still signed in is
    signed out, because the session secret is rotated too.
    """
    cfg = load_cfg()
    had = bool(cfg.get("password_hash"))
    cfg["password_hash"] = ""
    cfg["session_secret"] = ""
    save_cfg(cfg)
    return ("Password cleared. Open the app and tap Connect — it will set a "
            "new one automatically." if had else "There was no password set.")


@app.function(image=base_image, volumes=VOL, timeout=600)
def clear_cache(what: str) -> str:
    """what: 'video' (AI-2 scene catalogs), 'audio', 'fx', 'outputs'."""
    targets = {
        "video": [DATA / "maker/app_data/cache"],
        "audio": [DATA / "maker/app_data/audio_cache"],
        "fx": [DATA / "editor/fx_app_data/analysis_cache",
               DATA / "editor/fx_app_data/track_cache"],
        "outputs": [DATA / "maker/output", DATA / "editor/fx_output",
                    DATA / "voice", DATA / "script/scripts_output"],
    }
    n = 0
    for d in targets.get(what, []):
        if d.exists():
            for f in d.iterdir():
                if f.is_file():
                    f.unlink()
                    n += 1
    data_vol.commit()
    return f"Cleared {n} file(s) from {what}."


# ---------------------------------------------------------------------------
# THE PHONE APP — a FastAPI web app served straight off Modal
# ---------------------------------------------------------------------------
@app.function(image=web_image, volumes=VOL, timeout=900, max_containers=1)
@modal.concurrent(max_inputs=20)
@modal.asgi_app(label="arm-pipeline")
def web():
    import hmac
    import hashlib
    import secrets
    from fastapi import FastAPI, Request, UploadFile, File, Form, HTTPException
    from fastapi.responses import (HTMLResponse, JSONResponse, FileResponse,
                                   PlainTextResponse)

    api = FastAPI(title="Arm Pipeline")
    ASSETS = Path("/assets")

    def sha(s: str) -> str:
        return hashlib.sha256(s.encode()).hexdigest()

    def token_for(cfg) -> str:
        return hmac.new(cfg["session_secret"].encode(), b"arm-v1",
                        hashlib.sha256).hexdigest()

    def guard(request: Request) -> dict:
        cfg = load_cfg()
        if not cfg.get("password_hash"):
            return cfg          # first run: no password set yet
        want = token_for(cfg)
        # Browser sends a cookie; the Android app sends a bearer token.
        if request.cookies.get("arm_token") == want:
            return cfg
        auth = request.headers.get("authorization", "")
        if auth.startswith("Bearer ") and auth[7:].strip() == want:
            return cfg
        raise HTTPException(status_code=401, detail="Locked")

    # ---------- pages ----------
    @api.get("/", response_class=HTMLResponse)
    async def index():
        return (ASSETS / "index.html").read_text(encoding="utf-8")

    @api.get("/api/state")
    async def state(request: Request):
        cfg = load_cfg()
        locked = bool(cfg.get("password_hash")) and \
            request.cookies.get("arm_token") != token_for(cfg)
        return {"needs_password": not cfg.get("password_hash"), "locked": locked}

    @api.get("/api/health")
    async def health():
        """Unauthenticated — lets the Android app verify a URL before signing in."""
        return {"ok": True, "app": APP_NAME}

    @api.post("/api/login")
    async def login(request: Request):
        body = await request.json()
        pw = (body.get("password") or "").strip()
        if len(pw) < 4:
            raise HTTPException(400, "Use at least 4 characters.")
        cfg = load_cfg()
        if not cfg.get("password_hash"):
            cfg["password_hash"] = sha(pw)
            cfg["session_secret"] = secrets.token_hex(16)
            save_cfg(cfg)
        elif sha(pw) != cfg["password_hash"]:
            raise HTTPException(401, "Wrong password.")
        tok = token_for(cfg)
        r = JSONResponse({"ok": True, "token": tok})
        r.set_cookie("arm_token", tok, max_age=60 * 60 * 24 * 365,
                     httponly=True, samesite="lax", secure=True)
        return r

    @api.post("/api/password")
    async def change_password(request: Request):
        """Sets a new password while signed in. Rotating the session secret
        signs out every other device, which is the point of changing it."""
        cfg = guard(request)
        body = await request.json()
        pw = (body.get("password") or "").strip()
        if len(pw) < 4:
            raise HTTPException(400, "Use at least 4 characters.")
        cfg["password_hash"] = sha(pw)
        cfg["session_secret"] = secrets.token_hex(16)
        save_cfg(cfg)
        tok = token_for(cfg)
        r = JSONResponse({"ok": True, "token": tok})
        r.set_cookie("arm_token", tok, max_age=60 * 60 * 24 * 365,
                     httponly=True, samesite="lax", secure=True)
        return r

    # ---------- analysis performed by the phone ----------
    # Waiting on Gemini costs nothing on a phone and costs container-seconds on
    # Modal. These two endpoints let the app do the waiting: it asks for a plan,
    # makes the calls itself, and posts the timestamps back into the same cache
    # the pipeline already reads. Modal is then left with only the work it is
    # actually good at — downloading, cutting and rendering.
    #
    # The prompt, the model and the schema all come from the Python, so the app
    # never carries its own copy that could drift out of step.
    @api.get("/api/analysis/plan")
    async def analysis_plan(request: Request):
        cfg = guard(request)
        try:
            from core import maker_core as M
        except Exception as e:
            raise HTTPException(
                500, f"Backend cannot load the pipeline code: {e}")

        links = [u.strip() for u in cfg.get("links", []) if u.strip()]
        todo = []
        for u in links:
            key = M.url_key(u)
            if (M.CACHE_DIR / f"{key}.json").exists():
                continue
            todo.append({"url": u, "key": key, "uri": M.normalize_youtube(u) or ""})

        def gemini_schema(model_cls):
            """Pydantic JSON schema -> the OpenAPI subset Gemini accepts."""
            raw = model_cls.model_json_schema()
            defs = raw.get("$defs", {})

            def clean(node):
                if not isinstance(node, dict):
                    return node
                if "$ref" in node:
                    return clean(defs[node["$ref"].split("/")[-1]])
                out = {}
                for k, v in node.items():
                    if k in ("title", "default", "$defs", "additionalProperties"):
                        continue
                    if k == "properties":
                        out[k] = {pk: clean(pv) for pk, pv in v.items()}
                    elif k == "items":
                        out[k] = clean(v)
                    elif k == "anyOf":
                        return clean(next(x for x in v if x.get("type") != "null"))
                    else:
                        out[k] = v
                return out

            return clean(raw)

        schema = {}
        try:
            schema = gemini_schema(M.SCHEMAS["vision"])
        except Exception:
            pass

        model = cfg["model_vision"]
        return {
            "model": model,
            "api_key": cfg.get("api_key", ""),
            "system": M.VISION_SYSTEM,
            "prompt": "Catalog every arm wrestling scene per the schema.",
            "schema": schema,
            "min_interval_ms": int(M._MODEL_INTERVAL.get(model, 13.0) * 1000),
            # Videos are enormous in tokens and the free tier caps input tokens
            # per minute, so only a couple may overlap however fast requests
            # are allowed to start.
            "max_concurrent": int(cfg.get("phone_workers", 2)),
            "todo": todo,
            "cached": len(links) - len(todo),
            "total": len(links),
        }

    @api.post("/api/analysis/result")
    async def analysis_result(request: Request):
        """Stores one analysed video, in the exact shape analyze_video writes."""
        guard(request)
        try:
            from core import maker_core as M
        except Exception as e:
            raise HTTPException(
                500, f"Backend cannot load the pipeline code: {e}")

        body = await request.json()
        url = (body.get("url") or "").strip()
        scenes = body.get("scenes")
        if not url or not isinstance(scenes, list):
            raise HTTPException(400, "Need a url and a list of scenes.")
        key = body.get("key") or M.url_key(url)
        record = {"url": url, "key": key,
                  "duration": float(body.get("duration") or 0.0),
                  "scenes": scenes}
        M.CACHE_DIR.mkdir(parents=True, exist_ok=True)
        (M.CACHE_DIR / f"{key}.json").write_text(
            json.dumps(record, indent=2), encoding="utf-8")
        try:
            data_vol.commit()
        except Exception:
            pass
        return {"ok": True, "key": key, "scenes": len(scenes)}

    @api.get("/auth", response_class=HTMLResponse)
    async def auth_page(redirect: str = ""):
        """Sign-in page the Android app opens in a Custom Tab. On success it
        bounces back into the app with the session token on the deep link."""
        cfg = load_cfg()
        first = not cfg.get("password_hash")
        return f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Arm Pipeline</title><style>
body{{background:#171310;color:#f3efe7;font-family:system-ui,sans-serif;
display:flex;flex-direction:column;justify-content:center;min-height:88vh;padding:26px;margin:0}}
h1{{font-size:26px;margin:0 0 6px;text-transform:uppercase;letter-spacing:.02em}}
p{{color:#a2968a;font-size:14px;margin:0 0 18px}}
input{{width:100%;background:#0d0a08;color:#f3efe7;border:1px solid #3a2f26;
border-radius:10px;padding:13px;font-size:16px;box-sizing:border-box}}
button{{width:100%;margin-top:12px;background:#dc3b26;color:#fff;border:0;
border-radius:10px;padding:14px;font-size:16px;font-weight:600}}
#err{{color:#dc3b26;font-size:14px;min-height:20px;margin-top:10px}}</style></head>
<body><h1>Arm Pipeline</h1>
<p>{'Choose a password to lock this backend.' if first else 'Sign in to connect the app.'}</p>
<input id=pw type=password placeholder="Password" autofocus>
<button onclick=go()>{'Set password' if first else 'Sign in'}</button>
<div id=err></div><script>
const redirect={json.dumps(redirect)};
async function go(){{
  const r=await fetch('/api/login',{{method:'POST',
    headers:{{'Content-Type':'application/json'}},
    body:JSON.stringify({{password:document.getElementById('pw').value}})}});
  if(!r.ok){{document.getElementById('err').textContent=
    (await r.json()).detail||'Sign-in failed';return;}}
  const d=await r.json();
  if(redirect) location.href=redirect+'?token='+encodeURIComponent(d.token);
  else location.href='/';
}}
document.getElementById('pw').addEventListener('keydown',e=>{{if(e.key==='Enter')go()}});
</script></body></html>"""

    # ---------- settings ----------
    @api.get("/api/config")
    async def get_config(request: Request):
        cfg = guard(request)
        safe = {k: v for k, v in cfg.items()
                if k not in ("password_hash", "session_secret")}
        safe["api_key_set"] = bool(cfg.get("api_key"))
        safe["api_key"] = ""
        for k in ("ref_text_path", "voice_ref_path", "cookies_file"):
            safe[k + "_name"] = Path(cfg[k]).name if cfg.get(k) else ""
        return safe

    @api.post("/api/config")
    async def set_config(request: Request):
        cfg = guard(request)
        body = await request.json()
        for k, v in body.items():
            if k in ("password_hash", "session_secret"):
                continue
            if k == "api_key" and not str(v).strip():
                continue        # never blank out a saved key by accident
            if k in DEFAULT_CONFIG or k.startswith("last_"):
                cfg[k] = v
        save_cfg(cfg)
        return {"ok": True}

    @api.post("/api/paste")
    async def paste(request: Request):
        """Save pasted text as the knowledge file — no file picker needed."""
        cfg = guard(request)
        body = await request.json()
        kind = body.get("kind", "reference")
        text = body.get("text", "") or ""
        folder = DATA / "uploads"
        folder.mkdir(parents=True, exist_ok=True)
        dest = folder / f"{kind}_pasted.txt"
        dest.write_text(text, encoding="utf-8")
        key = {"reference": "ref_text_path", "cookies": "cookies_file"}.get(kind)
        if key:
            cfg[key] = str(dest)
        save_cfg(cfg)
        return {"ok": True, "chars": len(text), "name": dest.name}

    @api.get("/api/knowledge")
    async def knowledge_info(request: Request):
        """Stats and a short preview only. The knowledge file can be hundreds
        of KB, which no phone text field can render, so the full text is never
        sent to the app."""
        cfg = guard(request)
        p = Path(cfg.get("ref_text_path", "") or "")
        if not p.is_file():
            return {"name": "", "chars": 0, "words": 0, "preview": ""}
        text = p.read_text(encoding="utf-8", errors="ignore")
        return {
            "name": p.name,
            "chars": len(text),
            "words": len(text.split()),
            "preview": text[:1200],
        }

    @api.post("/api/knowledge/append")
    async def knowledge_append(request: Request):
        """Adds to the existing knowledge file instead of replacing it, so
        pasting a few paragraphs never costs you the big import."""
        cfg = guard(request)
        body = await request.json()
        extra = (body.get("text") or "").strip()
        if not extra:
            raise HTTPException(400, "Nothing to add.")
        folder = DATA / "uploads"
        folder.mkdir(parents=True, exist_ok=True)
        dest = Path(cfg.get("ref_text_path", "") or "")
        if not dest.is_file():
            dest = folder / "reference_pasted.txt"
            dest.write_text("", encoding="utf-8")
        with open(dest, "a", encoding="utf-8") as f:
            f.write("\n\n" + extra)
        cfg["ref_text_path"] = str(dest)
        save_cfg(cfg)
        total = dest.stat().st_size
        return {"ok": True, "name": dest.name, "added": len(extra), "bytes": total}

    @api.get("/api/paste")
    async def paste_read(request: Request, kind: str = "reference"):
        cfg = guard(request)
        key = {"reference": "ref_text_path", "cookies": "cookies_file"}.get(kind)
        p = Path(cfg.get(key or "", "") or "")
        text = p.read_text(encoding="utf-8", errors="ignore") if p.is_file() else ""
        return {"text": text, "chars": len(text), "name": p.name if p.is_file() else ""}

    @api.post("/api/upload")
    async def upload(request: Request, kind: str = Form(...),
                     file: UploadFile = File(...)):
        cfg = guard(request)
        folder = DATA / "uploads"
        folder.mkdir(parents=True, exist_ok=True)
        dest = folder / f"{kind}_{Path(file.filename).name}"
        with open(dest, "wb") as f:
            shutil.copyfileobj(file.file, f)
        key = {"reference": "ref_text_path", "voice": "voice_ref_path",
               "cookies": "cookies_file"}.get(kind)
        if key:
            cfg[key] = str(dest)
        save_cfg(cfg)
        return {"ok": True, "name": dest.name,
                "size_mb": round(dest.stat().st_size / 1e6, 2)}

    # ---------- jobs ----------
    STAGES = {"titles": stage_titles, "script": stage_script, "voice": stage_voice,
              "analyze": stage_analyze, "cut": stage_cut, "fx": stage_fx,
              "all": stage_all}

    @api.post("/api/run/{stage}")
    async def run(stage: str, request: Request):
        guard(request)
        if stage not in STAGES:
            raise HTTPException(404, "No such stage.")
        body = {}
        try:
            body = await request.json()
        except Exception:
            pass
        job_id = f"{stage}-{uuid.uuid4().hex[:8]}"
        jobs[job_id] = {"stage": stage, "status": "queued", "log": [],
                        "started": time.time()}
        fn = STAGES[stage]
        if stage in ("script", "all"):
            # "all" happily takes an empty title and picks a topic itself.
            call = await fn.spawn.aio(job_id, body.get("title", ""))
        elif stage == "voice":
            call = await fn.spawn.aio(job_id, body.get("text", ""))
        elif stage == "fx":
            call = await fn.spawn.aio(job_id, body.get("video", ""))
        else:
            call = await fn.spawn.aio(job_id)
        rec = jobs.get(job_id) or {}
        rec["call_id"] = call.object_id
        jobs[job_id] = rec
        return {"job_id": job_id}

    @api.get("/api/job/{job_id}")
    async def job_status(job_id: str, request: Request, since: int = 0):
        guard(request)
        rec = jobs.get(job_id)
        if not rec:
            raise HTTPException(404, "Unknown job.")
        lines = rec.get("log", [])
        return {
            "status": rec.get("status", "queued"),
            "stage": rec.get("stage"),
            "phase": rec.get("phase"),
            "progress": rec.get("progress"),
            "result": rec.get("result"),
            "error": rec.get("error"),
            "lines": lines[since:],
            "next": len(lines),
        }

    @api.post("/api/job/{job_id}/cancel")
    async def job_cancel(job_id: str, request: Request):
        guard(request)
        rec = jobs.get(job_id) or {}
        # Mark first: the full-run chain checks this between phases.
        rec["status"] = "cancelled"
        jobs[job_id] = rec
        # Then kill the chain itself and whatever phase it had spawned.
        for cid in [rec.get("call_id")] + list(rec.get("children", [])):
            if not cid:
                continue
            try:
                await modal.FunctionCall.from_id(cid).cancel.aio(
                    terminate_containers=True)
            except Exception:
                pass
        return {"ok": True}

    # ---------- files ----------
    @api.get("/api/files")
    async def files(request: Request):
        guard(request)
        try:
            data_vol.reload()
        except Exception:
            pass
        out = []
        buckets = [
            ("fx", DATA / "editor/fx_output"),
            ("cut", DATA / "maker/output"),
            ("voice", DATA / "voice"),
            ("script", DATA / "script/scripts_output"),
        ]
        for kind, d in buckets:
            if not d.exists():
                continue
            for f in sorted(d.iterdir(), key=lambda p: -p.stat().st_mtime)[:25]:
                if f.is_file():
                    out.append({"kind": kind, "name": f.name, "path": rel(f),
                                "mb": round(f.stat().st_size / 1e6, 2),
                                "when": int(f.stat().st_mtime)})
        return out

    @api.get("/api/file/{path:path}")
    async def get_file(path: str, request: Request):
        guard(request)
        try:
            data_vol.reload()
        except Exception:
            pass
        target = (DATA / path).resolve()
        if not str(target).startswith("/data/") or not target.is_file():
            raise HTTPException(404, "Not found.")
        return FileResponse(target, filename=target.name)

    @api.get("/api/text/{path:path}", response_class=PlainTextResponse)
    async def get_text(path: str, request: Request):
        guard(request)
        target = (DATA / path).resolve()
        if not str(target).startswith("/data/") or not target.is_file():
            raise HTTPException(404, "Not found.")
        return target.read_text(encoding="utf-8", errors="ignore")

    @api.post("/api/clear/{what}")
    async def clear(what: str, request: Request):
        guard(request)
        return {"message": await clear_cache.remote.aio(what)}

    @api.get("/api/voices")
    async def voices(request: Request):
        guard(request)
        return await list_voices.remote.aio()

    return api


@app.local_entrypoint()
def main():
    print("Deploy with:  modal deploy modal_app.py")
