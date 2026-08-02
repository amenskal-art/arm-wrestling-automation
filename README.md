# Arm Pipeline — Kotlin app on the phone, Python pipeline on Modal

Your four Python scripts run unchanged on Modal. A native Android app is the
control surface: sidebar, stage cards, live progress, downloads.

```
Android app  ──HTTPS JSON──▶  Modal web endpoint  ──▶  Modal containers
   sidebar                       auth + jobs            Gemini · yt-dlp
   stage cards                                          ffmpeg · OpenCV
   progress bars                                        MediaPipe · Qwen3-TTS
   downloads    ◀──── final .mp4 ──────────────────────────────┘
```

## Your questions, answered

**Where does the Gemini agent run?** Entirely inside Modal. The phone never
calls Gemini and never holds your key — you paste it once, it is written to the
Modal volume, and only Modal containers read it. The app just says "run stage 3"
and reads back log lines.

**What travels between phone and cloud?** Small JSON over HTTPS: settings you
save, a `POST /api/run/{stage}` to start work, and a poll every 1.5 s returning
new log lines and a progress fraction. Uploads go up once (voice clip, knowledge
text). The only large download is the finished MP4, when you tap it — handed to
Android's DownloadManager so it lands in your Downloads folder.

**Where are the AI instructions?** Baked into the Python, in
`core/script_core.py` (`MASTER_PROMPT`) and the system prompts in
`core/maker_core.py` and `core/editor_core.py`. The app only supplies material
and a title.

**CPU or GPU?** Three stages are pure CPU, as you said. Stage 3 requests 8
cores, stage 4 requests 16 — Modal bills physical cores and lets a container
burst to request + 16, so the OpenCV render can reach roughly 32 before
throttling. Raise `cpu=` in `modal_app.py` if you want more.

One correction: **stage 2 does need a GPU.** Your own `tts_modal.py` loads
Qwen3-TTS with `device_map="cuda:0"` and bfloat16 — that is a CUDA model, and it
already ran on an A10G in your version. It stays on the A10G, alive only while
speaking, and it is the cheapest part of a run. On CPU it would take hours per
script, which is why the original was written for GPU in the first place.

## Getting it on your phone, without a PC

**1. Push this repo to GitHub.** Add two Actions secrets under
*Settings → Secrets and variables → Actions*: `MODAL_TOKEN_ID` and
`MODAL_TOKEN_SECRET`, from your Modal dashboard.

**2. Two workflows run automatically.**
- `deploy.yml` deploys the Python backend to Modal and prints your URL.
- `android.yml` builds the APK on GitHub's machines and attaches it to a
  release tagged `apk-latest`.

**3. Install the app.** On your phone open
`github.com/<you>/<repo>/releases/tag/apk-latest`, download the APK, and allow
"install from this source" when Android asks.

**4. Connect.** Open the app → **Connection** → paste the Modal URL → set a
password. Or tap *Sign in with the browser*: a Custom Tab opens the backend's
sign-in page and hands the session straight back to the app over
`armpipe://auth`.

The APK is debug-signed so it installs without a keystore. Android will warn
about an unknown developer; that is expected for a self-built app.

## Using it

**Sidebar** — Pipeline, Knowledge, Sources, Models, Files, Deploy, Connection.

**Knowledge** — paste your arm wrestling material, or import a `.txt`. Saved to
the Modal volume once.

**Sources** — Gemini key, video links (one per line), the 3-second reference
voice clip and its transcript, and an optional `cookies.txt` for when YouTube
starts asking datacenter IPs to prove they are not bots.

**Models** — every model name is a free-text field, so you can point each of the
five AI roles at whatever your key actually has.

**Pipeline** — tap **Run everything** and walk away. One tap does all four
stages back to back with no further input: if the title box is empty it even
asks Gemini for a topic and takes the strongest one. The card for whichever
stage is live lights up, earlier ones get stamped PINNED, and the log follows
along. A foreground notification keeps the poll alive with a progress bar while
you are in other apps, and the FX render reports a real percentage because your
`render()` already emits progress every 300 frames.

While anything is running, the bottom button turns into **Stop**. It asks for
confirmation first — the server then halts the chain and terminates the
container of whatever phase was working. Caches survive a stop, so restarting
skips everything already analysed.

You can still run stages one at a time from their own cards when you want to
redo just the effects pass.

**Files** — every output, newest first, with a download button.

**Deploy** — repository, workflow file, and a GitHub token. *Create a token in
the browser* opens GitHub's token page with the right scopes preselected; paste
it back, then **Deploy to Modal** fires the workflow and polls the run until it
finishes. Your Modal token stays in GitHub and never reaches the phone.

## Caching, unchanged

Three Modal volumes, created on first deploy:

- `arm-pipeline-data` — config, scene catalogs, transcripts, effect plans, hand
  tracking, outputs
- `arm-tts-model-cache` — Qwen3-TTS weights, downloaded once
- `arm-tts-voice-cache` — memorised voice clones

Nothing is analysed twice. Add a fifth link to four and only the new one costs
quota. *Files → Caches* clears one deliberately if you want a re-analysis.

## Layout

```
modal_app.py            Modal wiring: images, volumes, 4 stages, web API
core/script_core.py     your script_maker.py, GUI removed
core/tts_core.py        chunking + voice fingerprint from tts_modal.py
core/maker_core.py      your arm_video_maker.py, GUI removed
core/editor_core.py     your arm_video_editor.py, GUI removed
web/index.html          browser fallback UI, same endpoints
android/                the Kotlin app (Compose, Material 3)
.github/workflows/      deploy.yml → Modal,  android.yml → APK
```

## If something breaks

- **App says "No backend at that address"** — the URL is wrong or the deploy has
  not finished. Open the URL in a browser; you should see the sign-in page.
- **A stage fails with a 404 on the model** — that model name is not on your
  key. Change it in Models.
- **Downloads fail a bot check** — upload `cookies.txt` in Sources.
- **Stage 4 is slow** — it is single-pass OpenCV over every frame. Raise `cpu=`
  in `modal_app.py` and redeploy from the app's Deploy screen.

The first APK build is the one most likely to need a nudge: if `android.yml`
fails, open the run log, and the Gradle error line will name the dependency
version to bump in `android/app/build.gradle.kts`.
