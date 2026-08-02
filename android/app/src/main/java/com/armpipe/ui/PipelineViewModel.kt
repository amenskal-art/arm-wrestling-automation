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
import com.armpipe.work.JobTracker
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
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
    val knowledge: String = "",
    val knowledgeName: String = "",
    val titleText: String = "",
    val suggestions: List<String> = emptyList(),
    val linksText: String = "",
    val files: List<FileItem> = emptyList(),
    val message: String? = null,
    val stages: List<StageUi> = DEFAULT_STAGES,
    /** Set while a full run is in flight, so Stop works from any card. */
    val activeJobId: String? = null,
    val runningAll: Boolean = false,
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

    private val pollers = mutableMapOf<String, Job>()
    private val ctx: Context get() = getApplication()

    init {
        viewModelScope.launch {
            Prefs.flow(ctx).collect { p ->
                Api.baseUrl = p.baseUrl
                Api.token = p.token
                val was = _ui.value.connected
                _ui.update { it.copy(connected = p.connected, baseUrl = p.baseUrl) }
                if (p.connected && !was) refresh()
            }
        }
    }

    fun dismissMessage() = _ui.update { it.copy(message = null) }
    private fun say(msg: String) = _ui.update { it.copy(message = msg) }

    /* ------------------------------------------------------------ connect */

    fun connect(rawUrl: String, password: String) = viewModelScope.launch {
        val url = normalise(rawUrl)
        _ui.update { it.copy(connecting = true) }
        try {
            Api.health(url)                       // fail fast on a wrong URL
            Api.baseUrl = url
            val token = Api.login(password)
            Prefs.setConnection(ctx, url, token)
            say("Connected.")
        } catch (e: ApiException) {
            say(if (e.code == 401) "Wrong password." else e.message ?: "Could not connect.")
        } catch (e: Exception) {
            say("No backend at that address. Check the URL.")
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
        Prefs.setBaseUrl(ctx, normalise(rawUrl))
    }

    fun disconnect() = viewModelScope.launch {
        pollers.values.forEach { it.cancel() }
        pollers.clear()
        Prefs.disconnect(ctx)
        _ui.update { UiState(baseUrl = it.baseUrl) }
    }

    private fun normalise(raw: String): String {
        var u = raw.trim()
        if (u.isEmpty()) return u
        if (!u.startsWith("http")) u = "https://$u"
        return u.trimEnd('/')
    }

    /* ------------------------------------------------------------- config */

    fun refresh() = viewModelScope.launch {
        _ui.update { it.copy(loading = true) }
        try {
            val cfg = Api.config()
            val know = runCatching { Api.readKnowledge() }.getOrNull()
            _ui.update {
                it.copy(
                    config = cfg,
                    linksText = cfg.links.joinToString("\n"),
                    knowledge = know?.text ?: it.knowledge,
                    knowledgeName = cfg.refName,
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

    fun setKnowledge(text: String) = _ui.update { it.copy(knowledge = text) }

    fun saveKnowledge() = viewModelScope.launch {
        try {
            val r = Api.writeKnowledge(_ui.value.knowledge)
            _ui.update { it.copy(knowledgeName = r.name) }
            say("Knowledge saved (${r.chars} characters).")
        } catch (e: Exception) {
            say("Could not save the knowledge file.")
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
            JobService.start(ctx)
            watch(jobId, uiStage, stage)
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
        pollers.values.forEach { it.cancel() }
        pollers.clear()
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
        JobService.stop(ctx)
        say("Stopped.")
    }

    fun cancel(stageId: String) = stopEverything()

    private fun watch(jobId: String, uiStage: String, requested: String) {
        pollers.remove(uiStage)?.cancel()
        pollers[uiStage] = viewModelScope.launch {
            var since = 0
            while (true) {
                delay(1500)
                val j = runCatching { Api.job(jobId, since) }.getOrNull() ?: continue
                since = j.next
                // During a full run the server reports which stage it reached;
                // mark the ones behind it done and move the spotlight forward.
                val target = if (requested == "all")
                    (j.phase?.takeIf { it in STAGE_ORDER } ?: "script") else uiStage
                if (requested == "all") advanceTo(target)
                updateStage(target) { s ->
                    s.copy(
                        progress = j.progress ?: s.progress,
                        log = (s.log + j.lines).takeLast(400),
                    )
                }
                syncTracker()
                when (j.status) {
                    "done" -> {
                        val titles = j.result?.get("titles")?.jsonArray
                            ?.mapNotNull { it.jsonPrimitive.contentOrNull }
                        if (requested == "titles" && titles != null) {
                            _ui.update {
                                it.copy(suggestions = titles,
                                        titleText = titles.firstOrNull() ?: it.titleText)
                            }
                            updateStage(target) { it.copy(state = StageState.IDLE) }
                            _ui.update { it.copy(activeJobId = null) }
                            say("Pick a topic, then run the stage.")
                        } else if (requested == "all") {
                            _ui.update { st ->
                                st.copy(
                                    runningAll = false, activeJobId = null,
                                    stages = st.stages.map {
                                        it.copy(state = StageState.DONE, progress = 1f)
                                    }
                                )
                            }
                            say("All four stages finished. The video is in Files.")
                            loadFiles()
                        } else {
                            updateStage(target) {
                                it.copy(state = StageState.DONE, progress = 1f)
                            }
                            _ui.update { it.copy(activeJobId = null) }
                            say("Finished. The file is in Files.")
                            loadFiles()
                        }
                        finishJob(uiStage); return@launch
                    }
                    "failed", "cancelled" -> {
                        val stopped = j.status == "cancelled"
                        updateStage(target) {
                            it.copy(
                                state = if (stopped) StageState.IDLE else StageState.FAILED,
                                note = if (stopped) "stopped"
                                       else j.error?.take(120).orEmpty()
                            )
                        }
                        _ui.update { it.copy(runningAll = false, activeJobId = null) }
                        if (!stopped) say(j.error?.take(140) ?: "The stage stopped.")
                        finishJob(uiStage); return@launch
                    }
                }
            }
        }
    }

    private fun finishJob(stageId: String) {
        pollers.remove(stageId)
        syncTracker()
        if (pollers.isEmpty()) JobService.stop(ctx)
    }

    private fun syncTracker() {
        val running = _ui.value.stages.filter { it.state == StageState.RUNNING }
        JobTracker.update(
            label = running.joinToString(", ") { it.title }.ifBlank { "Working" },
            progress = running.firstOrNull()?.progress,
            active = running.isNotEmpty(),
        )
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
