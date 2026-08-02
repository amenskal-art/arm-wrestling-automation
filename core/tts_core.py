"""Pure logic lifted verbatim from your tts_modal.py - no GUI, no Modal glue.
Only the constants and the two helpers the cloud function actually needs.
"""

import re
import hashlib

# ==========================================
# SHARED CONFIG
# ==========================================
APP_NAME = "qwen3tts-cloud"
MODEL_ID = "Qwen/Qwen3-TTS-12Hz-1.7B-Base"
GPU_TYPE = "A10G"          # 1.7B model runs great on A10G. If your Modal account
                           # has a payment method, "A100" or "L40S" = faster long scripts.
CHUNK_CHAR_BUDGET = 350    # ~50-60 words per synthesis chunk (keeps each generation stable)
BATCH_SIZE = 8             # chunks synthesized per generate_voice_clone() call
GAP_SECONDS = 0.25         # silence stitched between chunks
UPLOAD_CHUNK = 1024 * 1024        # 1 MB   (client -> cloud)
DOWNLOAD_CHUNK = 2 * 1024 * 1024  # 2 MB   (cloud -> client)
VOICES_DIR = "/voices"

LANGUAGES = ["Auto", "English", "Chinese", "Japanese", "Korean", "German",
             "French", "Russian", "Portuguese", "Spanish", "Italian"]


def compute_voice_id(audio_bytes: bytes, ref_text: str) -> str:
    """Fingerprint of a reference voice. Used by BOTH client and cloud."""
    h = hashlib.sha256()
    h.update(audio_bytes)
    h.update(ref_text.strip().encode("utf-8"))
    return h.hexdigest()[:16]


def _split_script(text: str, budget: int = CHUNK_CHAR_BUDGET) -> list:
    """Split on sentence boundaries, pack sentences into ~budget-char chunks.
    A 1500-word script becomes ~25-30 small utterances the model handles
    reliably, instead of one giant generation that would truncate/degrade."""
    text = re.sub(r"\s+", " ", text).strip()
    sentences = re.split(r"(?<=[.!?\u3002\uff01\uff1f\u2026])\s+", text)
    chunks, cur = [], ""
    for s in sentences:
        s = s.strip()
        if not s:
            continue
        # a single monster sentence longer than budget: hard-split on commas/spaces
        while len(s) > budget:
            cut = s.rfind(",", 0, budget)
            if cut < budget // 2:
                cut = s.rfind(" ", 0, budget)
            if cut <= 0:
                cut = budget
            piece, s = s[:cut + 1].strip(), s[cut + 1:].strip()
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(piece)
        if not s:
            continue
        if len(cur) + len(s) + 1 <= budget:
            cur = (cur + " " + s).strip()
        else:
            if cur:
                chunks.append(cur)
            cur = s
    if cur:
        chunks.append(cur)
    return chunks
