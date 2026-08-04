package com.armpipe.ui

import android.app.Application
import android.content.Context
import android.content.Intent
import android.net.Uri
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.armpipe.data.Prefs
import com.armpipe.net.*
import com.armpipe.work.JobService
import com.armpipe.work.JobRepo
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.coroutineScope
import kotlinx.coroutines.awaitAll
import kotlinx.coroutines.async
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import kotlinx.serialization.json.*

enum class StageState { IDLE, RUNNING, DONE, FAILED }

data class StageUi(
    val id: String,
    val number: String,
    val title: String,
    val blurb: String,
    val state: StageState = StageState.IDLE,
    val progress: Float? = null,
    val log: List<String> = emptyList(),
    val jobId: String? = null,
    val note: String = "",
)

data class UiState(
    val connecting: Boolean = false,
    val connected: Boolean = false,
    val baseUrl: String = "",
    val loading: Boolean = false,
    val config: Config = Config(),
    // Only stats and a preview. Putting a 600 KB file into a text field is
    // what crashed the app, so the full text stays on the server.
    val knowledgeName: String = "",
    val knowledgeChars: Int = 0,
    val knowledgeWords: Int = 0,
    val knowledgePreview: String = "",
    val titleText: String = "",
    val suggestions: List<String> = emptyList(),
    val linksText: String = "",
    val files: List<FileItem> = emptyList(),
    val message: String? = null,
    val stages: List<StageUi> = DEFAULT_STAGES,
    /** Set while a full run is in flight, so Stop works from any card. */
    val activeJobId: String? = null,
    val runningAll: Boolean = false,
    // GitHub details, shared by the Deploy and Connection screens.
    val ghRepo: String = "",
    val ghToken: String = "",
    val ghWorkflow: String = "deploy.yml",
    val fetchingUrl: Boolean = false,
    /** True only when reconnecting to a backend somebody already claimed. */
    val needsPassword: Boolean = false,
    val backendPassword: String = "",
    val resetting: Boolean = false,
    val resetNote: String = "",
    // AI 2 running on the handset instead of on a paid container.
    val localAnalysing: Boolean = false,
    val localDone: Int = 0,
    val localTotal: Int = 0,
    val localLog: List<String> = emptyList(),
) {
    fun stage(id: String) = stages.first { it.id == id }
    val busy get() = stages.any { it.state == StageState.RUNNING }
}

/** The order the chain walks, used to light up the right card as it moves. */
val STAGE_ORDER = listOf("script", "voice", "cut", "fx")

val DEFAULT_STAGES = listOf(
    StageUi("script", "01", "Write the script",
        "Gemini reads your knowledge file and writes the narration."),
    StageUi("voice", "02", "Speak it",
        "Qwen3-TTS clones your voice on a cloud GPU."),
    StageUi("cut", "03", "Cut the video",
        "Three AIs transcribe, watch every source clip and build the edit."),
    StageUi("fx", "04", "Add the effects",
        "Zooms, ramps, hand tracking, grade, then the final render."),
)

class PipelineViewModel(app: Application) : AndroidViewModel(app) {

    private val _ui = MutableStateFlow(UiState())
    val ui: StateFlow<UiState> = _ui.asStateFlow()

    private val ctx: Context get() = getApplication()

    init {
        observeJob()
        rejoinRunningJob()
        viewModelScope.launch {
            Prefs.flow(ctx).collect { p ->
                Api.baseUrl = p.baseUrl
                Api.token = p.token
                val was = _ui.value.connected
                _ui.update {
                    it.copy(
                        connected = p.connected, baseUrl = p.baseUrl,
                        ghRepo = p.ghRepo, ghToken = p.ghToken,
                        ghWorkflow = p.ghWorkflow,
                        backendPassword = p.backendPassword,
                    )
                }
                if (p.connected && !was) refresh()
            }
        }
    }

    /**
     * If a job was running when the app was last closed, pick it back up.
     * The work never stopped — it runs on Modal — so this only restores the view.
     */
    private fun rejoinRunningJob() = viewModelScope.launch {
        val p = Prefs.flow(ctx).first()
        if (p.activeJob.isBlank()) return@launch
        JobRepo.state.value = JobRepo.Snapshot(
            jobId = p.activeJob, requested = p.activeStage, status = "running")
        JobService.start(ctx)
        say("Rejoined the run already in progress.")
    }

    fun dismissMessage() = _ui.update { it.copy(message = null) }
    fun say(msg: String) = _ui.update { it.copy(message = msg) }

    /* ------------------------------------------------------------ connect */

    /**
     * Everything the connection needs, from one box.
     *
     * Accepts a workspace name ("chris"), a host, or a full URL. Modal web
     * endpoints are always <workspace>--<label>.modal.run, so the workspace
     * name alone is enough to build the address.
     */
    fun resolveUrl(raw: String): String? {
        val t = raw.trim().trimEnd('/')
        if (t.isBlank()) return null
        return when {
            t.startsWith("http://") || t.startsWith("https://") -> t
            t.contains(".modal.run") -> "https://$t"
            t.contains('.') || t.contains(' ') || t.contains('/') -> null
            else -> "https://$t--arm-pipeline.modal.run"
        }
    }

    private fun newPassword(): String {
        val r = java.security.SecureRandom()
        val bytes = ByteArray(12).also { r.nextBytes(it) }
        return bytes.joinToString("") { "%02x".format(it) }
    }

    /** One tap. Finds the backend, claims it if new, signs in. */
    fun connectTo(raw: String) = viewModelScope.launch {
        val url = resolveUrl(raw)
        if (url == null) {
            say("Type your Modal workspace name, or paste the full URL.")
            return@launch
        }
        _ui.update { it.copy(connecting = true, needsPassword = false) }
        try {
            Api.health(url)                     // fail fast if that is not it
            Api.baseUrl = url
            val st = Api.state(url)
            val saved = _ui.value.backendPassword
            when {
                st.needsPassword -> {
                    // Brand new backend: claim it with a generated password so
                    // there is nothing to invent or remember.
                    val pw = newPassword()
                    val token = Api.login(pw)
                    Prefs.setBackendPassword(ctx, pw)
                    Prefs.setConnection(ctx, url, token)
                    say("Connected.")
                }
                saved.isNotBlank() -> {
                    val token = Api.login(saved)
                    Prefs.setConnection(ctx, url, token)
                    say("Connected.")
                }
                else -> {
                    Prefs.setBaseUrl(ctx, url)
                    _ui.update { it.copy(needsPassword = true) }
                    say("This backend already has a password. Enter it once.")
                }
            }
        } catch (e: ApiException) {
            say(if (e.code == 401) "Wrong password." else e.message ?: "Could not connect.")
        } catch (e: Exception) {
            say("Nothing answered at $url — check the workspace name.")
        } finally {
            _ui.update { it.copy(connecting = false) }
        }
    }

    /** Makes a new password, applies it, and remembers it. Nothing to type. */
    fun regeneratePassword() = viewModelScope.launch {
        try {
            val pw = newPassword()
            val token = Api.changePassword(pw)
            Prefs.setBackendPassword(ctx, pw)
            Prefs.setConnection(ctx, _ui.value.baseUrl, token)
            say("New password set and saved.")
        } catch (e: Exception) {
            say("Could not change the password: ${e.message}")
        }
    }

    /**
     * The locked-out path. The app cannot ask the backend to unlock itself, so
     * it asks GitHub to run the reset workflow, waits for it, then reconnects.
     */
    fun resetPasswordViaGitHub(workspace: String) = viewModelScope.launch {
        val st = _ui.value
        if (st.ghRepo.isBlank() || st.ghToken.isBlank()) {
            say("Enter your repository and GitHub token first.")
            return@launch
        }
        _ui.update { it.copy(resetting = true, resetNote = "Asking GitHub to reset…") }
        try {
            com.armpipe.work.GitHubDeploy.dispatch(
                st.ghRepo, st.ghToken, com.armpipe.work.GitHubDeploy.RESET_WORKFLOW)
            var done = false
            for (attempt in 0 until 60) {
                delay(5000)
                val run = runCatching {
                    com.armpipe.work.GitHubDeploy.latestRun(
                        st.ghRepo, st.ghToken,
                        com.armpipe.work.GitHubDeploy.RESET_WORKFLOW)
                }.getOrNull()
                _ui.update {
                    it.copy(resetNote = when {
                        run == null -> "Waiting for the run to start…"
                        run.status != "completed" -> "Resetting — ${run.status}"
                        run.conclusion == "success" -> "Reset done. Reconnecting…"
                        else -> "Reset failed: ${run.conclusion}"
                    })
                }
                if (run?.status == "completed") { done = run.conclusion == "success"; break }
            }
            _ui.update { it.copy(resetting = false) }
            if (done) {
                Prefs.setBackendPassword(ctx, "")
                connectTo(workspace.ifBlank { st.baseUrl })
            }
        } catch (e: Exception) {
            _ui.update {
                it.copy(resetting = false,
                        resetNote = e.message ?: "GitHub refused the request.")
            }
        }
    }

    /**
     * Runs AI 2 here on the phone.
     *
     * Gemini fetches each YouTube video itself, so this sends a few kilobytes
     * per video and then waits. Waiting is free on a handset and billed by the
     * second on a container, so the whole analysis phase costs nothing. Results
     * go straight into the same cache the cut stage reads, which then has no
     * Gemini work left to do.
     */
    fun analyseOnThisPhone() = viewModelScope.launch {
        if (_ui.value.localAnalysing) return@launch
        _ui.update { it.copy(localAnalysing = true, localLog = emptyList(),
                             localDone = 0, localTotal = 0) }

        fun note(m: String) = _ui.update {
            it.copy(localLog = (it.localLog + m).takeLast(300))
        }

        try {
            val plan = Api.analysisPlan()
            if (plan.api_key.isBlank()) {
                say("Save your Gemini key in Sources first."); return@launch
            }
            _ui.update { it.copy(localTotal = plan.todo.size) }
            note("${plan.total} link(s): ${plan.cached} cached, " +
                 "${plan.todo.size} to analyse here.")
            if (plan.todo.isEmpty()) {
                note("Nothing to do — every video is already analysed.")
                say("All videos already analysed."); return@launch
            }
            note("Model ${plan.model}, one start every " +
                 "${plan.min_interval_ms / 1000}s.")

            var ok = 0
            var failed = 0
            val started = System.currentTimeMillis()

            // Several analyses run at once; only their STARTS are paced, which
            // is what the per-minute limit actually counts.
            val gate = kotlinx.coroutines.sync.Mutex()
            var lastStart = 0L

            coroutineScope {
                val jobs = plan.todo.map { task ->
                    async(kotlinx.coroutines.Dispatchers.IO) {
                        gate.withLock {
                            val since = System.currentTimeMillis() - lastStart
                            val wait = plan.min_interval_ms - since
                            if (lastStart != 0L && wait > 0) delay(wait)
                            lastStart = System.currentTimeMillis()
                        }
                        try {
                            if (task.uri.isBlank())
                                throw IllegalStateException("no video id in the link")
                            val scenes = Gemini.analyseVideo(plan, task)
                            Api.postAnalysis(task.url, task.key, scenes)
                            synchronized(this@PipelineViewModel) { ok++ }
                            note("  ok  ${scenes.size} scenes — ${task.url.takeLast(40)}")
                        } catch (e: Gemini.QuotaExhausted) {
                            synchronized(this@PipelineViewModel) { failed++ }
                            note("  quota reached — stopping. ${e.message?.take(90)}")
                            throw e
                        } catch (e: Exception) {
                            synchronized(this@PipelineViewModel) { failed++ }
                            note("  x  ${task.url.takeLast(40)} -> ${e.message?.take(90)}")
                        } finally {
                            _ui.update { it.copy(localDone = it.localDone + 1) }
                        }
                    }
                }
                runCatching { jobs.awaitAll() }
            }

            val secs = (System.currentTimeMillis() - started) / 1000
            note("Analysed $ok, failed $failed, in ${secs}s. " +
                 "Modal now has every timestamp it needs.")
            say("Analysis done on this phone. Run the cut next.")
        } catch (e: Exception) {
            note("Stopped: ${e.message}")
            say("Analysis stopped: ${e.message}")
        } finally {
            _ui.update { it.copy(localAnalysing = false) }
        }
    }

    /** Only used when rejoining a backend that already has a password. */
    fun connectWithPassword(raw: String, password: String) = viewModelScope.launch {
        val url = resolveUrl(raw) ?: return@launch
        if (password.length < 4) { say("Enter the password you used before."); return@launch }
        _ui.update { it.copy(connecting = true) }
        try {
            Api.baseUrl = url
            val token = Api.login(password)
            Prefs.setBackendPassword(ctx, password)
            Prefs.setConnection(ctx, url, token)
            _ui.update { it.copy(needsPassword = false) }
            say("Connected.")
        } catch (e: ApiException) {
            say(if (e.code == 401) "Wrong password." else e.message ?: "Could not connect.")
        } catch (e: Exception) {
            say("Could not reach the backend.")
        } finally {
            _ui.update { it.copy(connecting = false) }
        }
    }

    /** Called when the browser sign-in bounces back on armpipe://auth?token=… */
    fun acceptBrowserToken(token: String) = viewModelScope.launch {
        Prefs.setToken(ctx, token)
        say("Signed in.")
    }

    fun rememberUrl(rawUrl: String) = viewModelScope.launch {
        resolveUrl(rawUrl)?.let { Prefs.setBaseUrl(ctx, it) }
    }

    fun saveGitHub(repo: String, token: String, workflow: String = "deploy.yml") =
        viewModelScope.launch {
            Prefs.setGitHub(ctx, repo, token, workflow)
        }

    /** Reads the address the deploy workflow recorded in your repo. */
    fun fetchBackendUrl(loud: Boolean = true) = viewModelScope.launch {
        val st = _ui.value
        if (st.ghRepo.isBlank() || st.ghToken.isBlank()) {
            if (loud) say("Enter your GitHub repository and token first.")
            return@launch
        }
        _ui.update { it.copy(fetchingUrl = true) }
        val found = runCatching {
            com.armpipe.work.GitHubDeploy.backendUrl(st.ghRepo, st.ghToken)
        }.getOrNull()
        _ui.update { it.copy(fetchingUrl = false) }
        when {
            found != null -> {
                Prefs.setBaseUrl(ctx, found)
                say("Address found. Now choose a password.")
            }
            loud -> say("No address recorded yet — deploy the backend first.")
        }
    }


    fun disconnect() = viewModelScope.launch {
        Prefs.disconnect(ctx)
        _ui.update { UiState(baseUrl = it.baseUrl) }
    }

    /* ------------------------------------------------------------- config */

    fun refresh() = viewModelScope.launch {
        // Nothing to reload before a connection exists - don't shout about it.
        if (!_ui.value.connected) return@launch
        _ui.update { it.copy(loading = true) }
        try {
            val cfg = Api.config()
            val know = runCatching { Api.knowledgeInfo() }.getOrNull()
            _ui.update {
                it.copy(
                    config = cfg,
                    linksText = cfg.links.joinToString("\n"),
                    knowledgeName = know?.name ?: cfg.refName,
                    knowledgeChars = know?.chars ?: 0,
                    knowledgeWords = know?.words ?: 0,
                    knowledgePreview = know?.preview.orEmpty(),
                )
            }
            loadFiles()
        } catch (e: ApiException) {
            if (e.code == 401) say("Session expired. Sign in again.")
            else say(e.message ?: "Could not load settings.")
        } catch (e: Exception) {
            say("Could not reach the backend.")
        } finally {
            _ui.update { it.copy(loading = false) }
        }
    }

    fun patch(build: JsonObjectBuilder.() -> Unit) = viewModelScope.launch {
        try {
            Api.saveConfig(buildJsonObject(build))
            val cfg = Api.config()
            _ui.update { it.copy(config = cfg, linksText = cfg.links.joinToString("\n")) }
        } catch (e: Exception) {
            say("Could not save: ${e.message}")
        }
    }

    fun setApiKey(key: String) = viewModelScope.launch {
        if (key.isBlank()) return@launch
        runCatching { Api.saveApiKey(key) }
            .onSuccess { say("Key saved on the server."); refresh() }
            .onFailure { say("Could not save the key.") }
    }

    fun setLinksText(text: String) = _ui.update { it.copy(linksText = text) }

    fun saveLinks() = patch {
        putJsonArray("links") {
            _ui.value.linksText.lines().map { it.trim() }.filter { it.isNotEmpty() }
                .forEach { add(it) }
        }
    }

    /** Adds a short passage. Big files go through importKnowledge instead. */
    fun appendKnowledge(text: String, onDone: () -> Unit) = viewModelScope.launch {
        if (text.isBlank()) { say("Nothing to add."); return@launch }
        try {
            Api.appendKnowledge(text)
            onDone()
            refreshKnowledge()
            say("Added ${text.length} characters.")
        } catch (e: Exception) {
            say("Could not add that text.")
        }
    }

    /**
     * Streams a picked .txt straight to the server. The contents are never
     * held in UI state, so size does not matter.
     */
    fun importKnowledge(uri: Uri, name: String) = viewModelScope.launch {
        say("Uploading…")
        try {
            Api.upload(ctx, "reference", uri, name)
            refreshKnowledge()
            say("Knowledge file imported.")
        } catch (e: OutOfMemoryError) {
            say("That file is too large to send from this phone.")
        } catch (e: Exception) {
            say("Import failed: ${e.message}")
        }
    }

    private fun refreshKnowledge() = viewModelScope.launch {
        runCatching { Api.knowledgeInfo() }.onSuccess { k ->
            _ui.update {
                it.copy(knowledgeName = k.name, knowledgeChars = k.chars,
                        knowledgeWords = k.words, knowledgePreview = k.preview)
            }
        }
    }

    fun upload(kind: String, uri: Uri, name: String) = viewModelScope.launch {
        say("Uploading…")
        try {
            val r = Api.upload(ctx, kind, uri, name)
            say("Uploaded ${r.name} (${r.sizeMb} MB).")
            refresh()
        } catch (e: Exception) {
            say("Upload failed: ${e.message}")
        }
    }

    fun setTitle(t: String) = _ui.update { it.copy(titleText = t) }

    /* --------------------------------------------------------------- jobs */

    fun run(stage: String) = viewModelScope.launch {
        val uiStage = when (stage) { "titles", "all" -> "script"; else -> stage }
        // Only a single-stage script run needs a title up front. The full run
        // will ask Gemini for a topic itself if the box is empty.
        if (stage == "script" && _ui.value.titleText.isBlank()) {
            say("Type a title, or tap Suggest topics."); return@launch
        }
        if (_ui.value.titleText.isNotBlank()) Prefs.setLastTitle(ctx, _ui.value.titleText)
        try {
            val jobId = Api.run(stage, title = _ui.value.titleText)
            if (stage == "all") {
                _ui.update { st ->
                    st.copy(
                        activeJobId = jobId, runningAll = true,
                        stages = st.stages.map {
                            it.copy(
                                state = if (it.id == "script") StageState.RUNNING
                                        else StageState.IDLE,
                                progress = null, log = emptyList(),
                                jobId = jobId, note = "",
                            )
                        }
                    )
                }
            } else {
                _ui.update { it.copy(activeJobId = jobId) }
                updateStage(uiStage) {
                    it.copy(state = StageState.RUNNING, progress = null,
                            log = emptyList(), jobId = jobId, note = "")
                }
            }
            // The service owns the watching from here. It keeps polling with
            // the app closed, and survives the process being killed.
            JobRepo.begin(ctx, jobId, stage)
        } catch (e: Exception) {
            say("Could not start: ${e.message}")
        }
    }

    fun runEverything() = run("all")

    /** Stops whatever is running. The server cancels the chain and the phase
     *  container it had spawned. */
    fun stopEverything() = viewModelScope.launch {
        val job = _ui.value.activeJobId ?: return@launch
        runCatching { Api.cancel(job) }
            .onFailure { say("Could not reach the server to stop it.") }
        _ui.update { st ->
            st.copy(
                runningAll = false, activeJobId = null,
                stages = st.stages.map {
                    if (it.state == StageState.RUNNING)
                        it.copy(state = StageState.IDLE, note = "stopped")
                    else it
                }
            )
        }
        Prefs.clearActiveJob(ctx)
        JobService.stop(ctx)
        JobRepo.state.value = JobRepo.Snapshot()
        say("Stopped.")
    }

    fun cancel(stageId: String) = stopEverything()

    /**
     * Mirrors the service's snapshot onto the stage cards.
     *
     * Collected for the lifetime of the ViewModel, so reopening the app after
     * it was swiped away rebuilds the whole picture — including the log, which
     * the service replays from the server on attach.
     */
    private fun observeJob() = viewModelScope.launch {
        JobRepo.state.collect { snap ->
            val jobId = snap.jobId ?: return@collect
            val requested = snap.requested
            val target = if (requested == "all")
                (snap.phase?.takeIf { it in STAGE_ORDER } ?: "script")
            else if (requested in STAGE_ORDER) requested else "script"

            if (requested == "all") advanceTo(target)

            updateStage(target) { st ->
                st.copy(
                    state = if (snap.running) StageState.RUNNING else st.state,
                    progress = snap.progress ?: st.progress,
                    log = snap.lines,
                    jobId = jobId,
                )
            }
            _ui.update {
                it.copy(
                    activeJobId = if (snap.running) jobId else null,
                    runningAll = snap.running && requested == "all",
                )
            }

            when (snap.status) {
                "done" -> {
                    if (requested == "all") {
                        _ui.update { st ->
                            st.copy(stages = st.stages.map {
                                it.copy(state = StageState.DONE, progress = 1f)
                            })
                        }
                    } else {
                        updateStage(target) {
                            it.copy(state = StageState.DONE, progress = 1f)
                        }
                    }
                    loadFiles()
                }
                "failed" -> updateStage(target) {
                    it.copy(state = StageState.FAILED,
                            note = snap.error?.take(120).orEmpty())
                }
                "cancelled" -> updateStage(target) {
                    it.copy(state = StageState.IDLE, note = "stopped")
                }
            }
        }
    }

    /** Everything before the live phase is finished; the live one is working. */
    private fun advanceTo(phase: String) {
        val idx = STAGE_ORDER.indexOf(phase)
        if (idx < 0) return
        _ui.update { st ->
            st.copy(stages = st.stages.map {
                val i = STAGE_ORDER.indexOf(it.id)
                when {
                    i in 0 until idx && it.state != StageState.DONE ->
                        it.copy(state = StageState.DONE, progress = 1f)
                    i == idx && it.state == StageState.IDLE ->
                        it.copy(state = StageState.RUNNING)
                    else -> it
                }
            })
        }
    }

    private fun updateStage(id: String, block: (StageUi) -> StageUi) =
        _ui.update { st -> st.copy(stages = st.stages.map { if (it.id == id) block(it) else it }) }

    /* -------------------------------------------------------------- files */

    fun loadFiles() = viewModelScope.launch {
        runCatching { Api.files() }.onSuccess { list ->
            _ui.update { it.copy(files = list) }
        }
    }

    fun download(item: FileItem) {
        com.armpipe.work.Downloader.enqueue(ctx, Api.fileUrl(item.path), item.name, Api.token)
        say("Saving ${item.name} to Downloads…")
    }

    fun clearCache(what: String) = viewModelScope.launch {
        runCatching { Api.clearCache(what) }
            .onSuccess { say("Cleared the $what cache."); loadFiles() }
            .onFailure { say("Could not clear that cache.") }
    }

    fun openAuthPage(): Intent =
        Intent(Intent.ACTION_VIEW, Uri.parse(Api.authPageUrl()))
}
