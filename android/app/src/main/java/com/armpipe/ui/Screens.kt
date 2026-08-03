@file:OptIn(androidx.compose.material3.ExperimentalMaterial3Api::class)

package com.armpipe.ui

import android.content.Context
import android.net.Uri
import android.provider.OpenableColumns
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.browser.customtabs.CustomTabsIntent
import androidx.compose.animation.AnimatedVisibility
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.rotate
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.armpipe.net.FileItem
import com.armpipe.work.GitHubDeploy
import kotlinx.coroutines.launch
import kotlinx.serialization.json.*

/* =========================================================== shared pieces */

/** Stopping throws away a render that may be twenty minutes deep, so it always
 *  asks first. */
@Composable
fun ConfirmStopDialog(stageName: String?, onDismiss: () -> Unit, onConfirm: () -> Unit) {
    AlertDialog(
        onDismissRequest = onDismiss,
        containerColor = Pad2,
        titleContentColor = Chalk,
        textContentColor = Tape,
        icon = { Icon(Icons.Filled.WarningAmber, null, tint = Vinyl) },
        title = { Text("Stop the pipeline?") },
        text = {
            Text(
                if (stageName != null)
                    "\"$stageName\" is running. Stopping now throws away this " +
                        "stage's work. Anything already cached — scene catalogs, " +
                        "transcripts, the cloned voice — is kept, so starting again " +
                        "picks up from there."
                else
                    "Stopping now throws away the stage that is running. Everything " +
                        "already cached is kept."
            )
        },
        confirmButton = {
            TextButton(onClick = onConfirm) { Text("Stop it", color = Vinyl) }
        },
        dismissButton = {
            TextButton(onClick = onDismiss) { Text("Keep running", color = Tape) }
        }
    )
}

@Composable
fun Section(title: String, hint: String? = null, body: @Composable ColumnScope.() -> Unit) {
    Column(Modifier.fillMaxWidth().padding(horizontal = 16.dp, vertical = 8.dp)) {
        Text(title.uppercase(), style = MaterialTheme.typography.labelSmall, color = Vinyl)
        if (hint != null) {
            Spacer(Modifier.height(3.dp))
            Text(hint, style = MaterialTheme.typography.bodySmall, color = Tape)
        }
        Spacer(Modifier.height(10.dp))
        body()
    }
}

@Composable
fun Field(
    label: String,
    value: String,
    onChange: (String) -> Unit,
    lines: Int = 1,
    password: Boolean = false,
    numeric: Boolean = false,
    placeholder: String = "",
) {
    OutlinedTextField(
        value = value,
        onValueChange = onChange,
        label = { Text(label) },
        placeholder = { if (placeholder.isNotBlank()) Text(placeholder, color = Tape) },
        singleLine = lines == 1,
        minLines = lines,
        visualTransformation = if (password) PasswordVisualTransformation()
        else androidx.compose.ui.text.input.VisualTransformation.None,
        keyboardOptions = androidx.compose.foundation.text.KeyboardOptions(
            keyboardType = if (numeric) KeyboardType.Number else KeyboardType.Text
        ),
        colors = OutlinedTextFieldDefaults.colors(
            focusedBorderColor = Ref, unfocusedBorderColor = Line,
            focusedLabelColor = Ref, unfocusedLabelColor = Tape,
            focusedTextColor = Chalk, unfocusedTextColor = Chalk,
            cursorColor = Vinyl,
        ),
        modifier = Modifier.fillMaxWidth().padding(bottom = 10.dp)
    )
}

@Composable
fun Toggle(label: String, checked: Boolean, onChange: (Boolean) -> Unit) {
    Row(
        Modifier.fillMaxWidth().padding(vertical = 4.dp),
        verticalAlignment = Alignment.CenterVertically
    ) {
        Text(label, Modifier.weight(1f), color = Chalk,
            style = MaterialTheme.typography.bodyMedium)
        Switch(
            checked = checked, onCheckedChange = onChange,
            colors = SwitchDefaults.colors(
                checkedThumbColor = Color.White, checkedTrackColor = Vinyl,
                uncheckedTrackColor = Pad3, uncheckedBorderColor = Line
            )
        )
    }
}

@Composable
private fun LogBox(lines: List<String>) {
    val scroll = rememberScrollState()
    LaunchedEffect(lines.size) { scroll.animateScrollTo(scroll.maxValue) }
    Box(
        Modifier.fillMaxWidth().height(150.dp)
            .background(Color(0xFF0D0A08), RoundedCornerShape(9.dp))
            .border(1.dp, Line, RoundedCornerShape(9.dp))
            .verticalScroll(scroll).padding(10.dp)
    ) {
        Text(
            lines.joinToString("\n"),
            fontFamily = FontFamily.Monospace, fontSize = 11.sp, lineHeight = 16.sp,
            color = Color(0xFFBCD4A8)
        )
    }
}

private fun displayName(ctx: Context, uri: Uri): String {
    ctx.contentResolver.query(uri, null, null, null, null)?.use { c ->
        val i = c.getColumnIndex(OpenableColumns.DISPLAY_NAME)
        if (i >= 0 && c.moveToFirst()) return c.getString(i)
    }
    return uri.lastPathSegment ?: "file"
}

private fun openTab(ctx: Context, url: String) {
    runCatching {
        CustomTabsIntent.Builder().setShowTitle(true).build()
            .launchUrl(ctx, Uri.parse(url))
    }
}

/* ================================================================ pipeline */

@Composable
fun PipelineScreen(ui: UiState, vm: PipelineViewModel) {
    if (!ui.connected) {
        Section("Not connected", "Open Connection in the menu and point the app at your Modal URL.") {}
        return
    }
    var confirmStop by remember { mutableStateOf(false) }
    val running = ui.stages.firstOrNull { it.state == StageState.RUNNING }

    if (confirmStop) ConfirmStopDialog(
        stageName = running?.title,
        onDismiss = { confirmStop = false },
        onConfirm = { confirmStop = false; vm.stopEverything() }
    )

    Column(Modifier.padding(bottom = 8.dp)) {
        if (ui.runningAll) {
            Text(
                "Running all four stages. You do not need to do anything else.",
                style = MaterialTheme.typography.bodySmall, color = Pin,
                modifier = Modifier.padding(start = 18.dp, top = 4.dp, end = 18.dp)
            )
        }
        ui.stages.forEach { stage ->
            StageCard(stage, ui, vm) { confirmStop = true }
        }
        Spacer(Modifier.height(8.dp))
    }
}

@Composable
private fun StageCard(
    stage: StageUi, ui: UiState, vm: PipelineViewModel, onStop: () -> Unit,
) {
    var showLog by rememberSaveable(stage.id) { mutableStateOf(false) }
    val accent = when (stage.state) {
        StageState.RUNNING -> Ref
        StageState.DONE -> Pin
        StageState.FAILED -> Vinyl
        else -> Line
    }
    Card(
        colors = CardDefaults.cardColors(
            containerColor = if (stage.state == StageState.RUNNING) Pad3 else Pad2
        ),
        shape = RoundedCornerShape(14.dp),
        modifier = Modifier.fillMaxWidth().padding(horizontal = 14.dp, vertical = 6.dp)
    ) {
        Row(Modifier.height(IntrinsicSize.Min)) {
            Box(Modifier.width(3.dp).fillMaxHeight().background(accent))
            Column(Modifier.padding(15.dp)) {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Column(Modifier.weight(1f)) {
                        Text(stage.number, style = MaterialTheme.typography.labelSmall,
                            color = Tape, fontFamily = FontFamily.Monospace)
                        Text(stage.title, style = MaterialTheme.typography.headlineSmall,
                            color = Chalk)
                    }
                    // The stamp: a pin ends a match, and it ends a stage too.
                    if (stage.state == StageState.DONE) {
                        Text(
                            "PINNED", fontSize = 11.sp, fontWeight = FontWeight.Black,
                            color = Pin, letterSpacing = 1.6.sp,
                            modifier = Modifier
                                .rotate(-9f)
                                .border(2.dp, Pin, RoundedCornerShape(4.dp))
                                .padding(horizontal = 7.dp, vertical = 2.dp)
                        )
                    }
                }
                Spacer(Modifier.height(4.dp))
                Text(stage.blurb, style = MaterialTheme.typography.bodySmall, color = Tape)

                if (stage.id == "script") {
                    Spacer(Modifier.height(12.dp))
                    Field("Video title", ui.titleText, vm::setTitle,
                        placeholder = "Type one, or let Gemini suggest")
                    if (ui.suggestions.isNotEmpty()) {
                        Text("Suggested topics", style = MaterialTheme.typography.labelSmall,
                            color = Tape)
                        Spacer(Modifier.height(4.dp))
                        ui.suggestions.forEach { s ->
                            SuggestionChip(
                                onClick = { vm.setTitle(s) },
                                label = { Text(s, fontSize = 13.sp, maxLines = 2) },
                                colors = SuggestionChipDefaults.suggestionChipColors(
                                    containerColor = if (s == ui.titleText) Pad else Pad3,
                                    labelColor = Chalk
                                ),
                                modifier = Modifier.fillMaxWidth().padding(bottom = 5.dp)
                            )
                        }
                    }
                    OutlinedButton(
                        onClick = { vm.run("titles") },
                        enabled = !ui.busy,
                        colors = ButtonDefaults.outlinedButtonColors(contentColor = Chalk),
                        border = androidx.compose.foundation.BorderStroke(1.dp, Line)
                    ) { Text("Suggest topics") }
                }

                if (stage.state == StageState.RUNNING) {
                    val p = stage.progress
                    Spacer(Modifier.height(12.dp))
                    if (p != null) {
                        LinearProgressIndicator(
                            progress = { p },
                            color = Ref, trackColor = Line,
                            modifier = Modifier.fillMaxWidth().height(4.dp)
                        )
                        Spacer(Modifier.height(4.dp))
                        Text("${(p * 100).toInt()}% rendered",
                            style = MaterialTheme.typography.bodySmall, color = Tape)
                    } else {
                        LinearProgressIndicator(
                            color = Ref, trackColor = Line,
                            modifier = Modifier.fillMaxWidth().height(4.dp)
                        )
                    }
                }
                if (stage.note.isNotBlank()) {
                    Spacer(Modifier.height(6.dp))
                    Text(stage.note, style = MaterialTheme.typography.bodySmall, color = Vinyl)
                }

                Spacer(Modifier.height(12.dp))
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp),
                    verticalAlignment = Alignment.CenterVertically) {
                    if (stage.state == StageState.RUNNING) {
                        Button(
                            onClick = onStop,
                            colors = ButtonDefaults.buttonColors(containerColor = Pad3)
                        ) { Text("Stop") }
                    } else {
                        Button(
                            onClick = { vm.run(stage.id) },
                            enabled = !ui.busy,
                            colors = ButtonDefaults.buttonColors(containerColor = Vinyl)
                        ) { Text("Run this stage") }
                    }
                    TextButton(onClick = { showLog = !showLog }) {
                        Text(if (showLog) "Hide log" else "Log", color = Tape)
                    }
                }
                AnimatedVisibility(showLog || stage.state == StageState.RUNNING) {
                    Column {
                        Spacer(Modifier.height(8.dp))
                        LogBox(stage.log.ifEmpty { listOf("Nothing yet.") })
                    }
                }
            }
        }
    }
}

@Composable
fun RunEverythingBar(ui: UiState, vm: PipelineViewModel) {
    var confirmStop by remember { mutableStateOf(false) }
    val running = ui.stages.firstOrNull { it.state == StageState.RUNNING }

    if (confirmStop) ConfirmStopDialog(
        stageName = running?.title,
        onDismiss = { confirmStop = false },
        onConfirm = { confirmStop = false; vm.stopEverything() }
    )

    Surface(color = Pad, tonalElevation = 0.dp) {
        Column(
            Modifier.fillMaxWidth().navigationBarsPadding()
                .padding(horizontal = 14.dp, vertical = 10.dp)
        ) {
            if (ui.busy) {
                Text(
                    running?.let { "Working on ${it.title.lowercase()}" }
                        ?: "Working",
                    style = MaterialTheme.typography.bodySmall, color = Tape,
                    modifier = Modifier.padding(bottom = 6.dp)
                )
                Button(
                    onClick = { confirmStop = true },
                    colors = ButtonDefaults.buttonColors(containerColor = Pad3),
                    modifier = Modifier.fillMaxWidth().height(50.dp)
                ) {
                    Icon(Icons.Filled.StopCircle, null, tint = Vinyl)
                    Spacer(Modifier.width(8.dp))
                    Text("Stop", fontSize = 16.sp, fontWeight = FontWeight.Bold,
                        color = Chalk)
                }
            } else {
                Button(
                    onClick = { vm.runEverything() },
                    colors = ButtonDefaults.buttonColors(containerColor = Vinyl),
                    modifier = Modifier.fillMaxWidth().height(50.dp)
                ) {
                    Icon(Icons.Filled.PlayArrow, null)
                    Spacer(Modifier.width(8.dp))
                    Text("Run everything", fontSize = 16.sp, fontWeight = FontWeight.Bold)
                }
                Text(
                    "Script, voice, cut and effects, start to finish. Leave the " +
                        "title empty and Gemini picks the topic too.",
                    style = MaterialTheme.typography.bodySmall, color = Tape,
                    modifier = Modifier.padding(top = 6.dp)
                )
            }
        }
    }
}

/* =============================================================== knowledge */

@Composable
fun KnowledgeScreen(ui: UiState, vm: PipelineViewModel) {
    val ctx = LocalContext.current
    // Anything above this is refused by the paste box. A phone text field
    // measures every character it holds, and a big file will kill the app.
    val PASTE_LIMIT = 20_000
    var draft by rememberSaveable { mutableStateOf("") }

    val picker = rememberLauncherForActivityResult(
        ActivityResultContracts.OpenDocument()
    ) { uri -> uri?.let { vm.importKnowledge(it, displayName(ctx, it)) } }

    Section(
        "Knowledge file",
        "Every fact in the script comes from this text. The writing instructions " +
            "live in the Python on the server, so you only supply the material."
    ) {
        if (ui.knowledgeChars > 0) {
            Card(
                colors = CardDefaults.cardColors(containerColor = Pad3),
                shape = RoundedCornerShape(12.dp),
                modifier = Modifier.fillMaxWidth()
            ) {
                Column(Modifier.padding(14.dp)) {
                    Text(ui.knowledgeName, style = MaterialTheme.typography.titleMedium,
                        color = Chalk, maxLines = 1)
                    Spacer(Modifier.height(6.dp))
                    Text(
                        "%,d words · %,d characters".format(
                            ui.knowledgeWords, ui.knowledgeChars),
                        style = MaterialTheme.typography.bodySmall, color = Pin,
                        fontFamily = FontFamily.Monospace
                    )
                    if (ui.knowledgePreview.isNotBlank()) {
                        Spacer(Modifier.height(10.dp))
                        Text("Starts with", style = MaterialTheme.typography.labelSmall,
                            color = Tape)
                        Spacer(Modifier.height(4.dp))
                        Text(
                            ui.knowledgePreview.take(400) + "…",
                            style = MaterialTheme.typography.bodySmall, color = Tape,
                            maxLines = 6
                        )
                    }
                }
            }
            Spacer(Modifier.height(6.dp))
            Text(
                "Stored on the server. It is not loaded into the app, which is why " +
                    "a file this size opens instantly.",
                style = MaterialTheme.typography.bodySmall, color = Tape
            )
        } else {
            Text("No knowledge file yet.", style = MaterialTheme.typography.bodyMedium,
                color = Tape)
        }

        Spacer(Modifier.height(16.dp))
        Button(
            onClick = { picker.launch(arrayOf("text/plain", "*/*")) },
            colors = ButtonDefaults.buttonColors(containerColor = Vinyl),
            modifier = Modifier.fillMaxWidth().height(48.dp)
        ) {
            Icon(Icons.Filled.UploadFile, null)
            Spacer(Modifier.width(8.dp))
            Text(if (ui.knowledgeChars > 0) "Replace with a .txt file"
                 else "Import a .txt file")
        }
        Text(
            "The right way to load a big document — it goes straight to the server " +
                "without passing through the app.",
            style = MaterialTheme.typography.bodySmall, color = Tape,
            modifier = Modifier.padding(top = 6.dp)
        )
    }

    HorizontalDivider(Modifier.padding(16.dp), color = Line)

    Section("Add a passage", "For a few paragraphs. Appends, never replaces.") {
        OutlinedTextField(
            value = draft,
            onValueChange = {
                // Refuse an oversized paste rather than trying to render it.
                if (it.length <= PASTE_LIMIT) draft = it
                else vm.say("Too long to paste here — use Import a .txt file instead.")
            },
            label = { Text("Paste text") },
            minLines = 6,
            colors = OutlinedTextFieldDefaults.colors(
                focusedBorderColor = Ref, unfocusedBorderColor = Line,
                focusedLabelColor = Ref, unfocusedLabelColor = Tape,
                focusedTextColor = Chalk, unfocusedTextColor = Chalk,
                cursorColor = Vinyl,
            ),
            modifier = Modifier.fillMaxWidth()
        )
        Spacer(Modifier.height(4.dp))
        Text(
            "${draft.length} / $PASTE_LIMIT characters",
            style = MaterialTheme.typography.bodySmall,
            color = if (draft.length > PASTE_LIMIT * 9 / 10) Vinyl else Tape
        )
        Spacer(Modifier.height(10.dp))
        Button(
            onClick = { vm.appendKnowledge(draft) { draft = "" } },
            colors = ButtonDefaults.buttonColors(containerColor = Vinyl),
            modifier = Modifier.fillMaxWidth()
        ) { Text("Add to the knowledge file") }
    }
    Spacer(Modifier.height(30.dp))
}

/* ================================================================= sources */

@Composable
fun SourcesScreen(ui: UiState, vm: PipelineViewModel) {
    val ctx = LocalContext.current
    var apiKey by rememberSaveable { mutableStateOf("") }

    val voicePick = rememberLauncherForActivityResult(
        ActivityResultContracts.OpenDocument()
    ) { uri -> uri?.let { vm.upload("voice", it, displayName(ctx, it)) } }

    val cookiePick = rememberLauncherForActivityResult(
        ActivityResultContracts.OpenDocument()
    ) { uri -> uri?.let { vm.upload("cookies", it, displayName(ctx, it)) } }

    Section("Gemini key", "Stored on the Modal volume. It never sits on this phone.") {
        Field("Gemini API key", apiKey, { apiKey = it }, password = true,
            placeholder = if (ui.config.apiKeySet) "A key is already saved" else "Paste it once")
        Button(
            onClick = { vm.setApiKey(apiKey); apiKey = "" },
            enabled = apiKey.isNotBlank(),
            colors = ButtonDefaults.buttonColors(containerColor = Vinyl)
        ) { Text("Save key") }
    }

    HorizontalDivider(Modifier.padding(16.dp), color = Line)

    Section("Video links", "One per line. Each is analysed once, then cached forever.") {
        Field("Links", ui.linksText, vm::setLinksText, lines = 6,
            placeholder = "https://www.youtube.com/watch?v=…")
        Button(
            onClick = { vm.saveLinks() },
            colors = ButtonDefaults.buttonColors(containerColor = Vinyl)
        ) { Text("Save links") }
    }

    HorizontalDivider(Modifier.padding(16.dp), color = Line)

    Section("Reference voice", "Three seconds is enough. Cloned once, then memorised.") {
        Text(
            ui.config.voiceName.ifBlank { "No clip uploaded yet." },
            style = MaterialTheme.typography.bodySmall, color = Tape
        )
        Spacer(Modifier.height(8.dp))
        OutlinedButton(
            onClick = { voicePick.launch(arrayOf("audio/*")) },
            colors = ButtonDefaults.outlinedButtonColors(contentColor = Chalk)
        ) { Text("Choose an audio clip") }
        Spacer(Modifier.height(12.dp))
        var transcript by remember(ui.config.voiceRefText) {
            mutableStateOf(ui.config.voiceRefText)
        }
        Field("Exact words spoken in that clip", transcript, { transcript = it }, lines = 3)
        Button(
            onClick = { vm.patch { put("voice_ref_text", transcript) } },
            colors = ButtonDefaults.buttonColors(containerColor = Vinyl)
        ) { Text("Save transcript") }
    }

    HorizontalDivider(Modifier.padding(16.dp), color = Line)

    Section(
        "YouTube cookies",
        "Only needed if downloads start failing a bot check. Export cookies.txt " +
            "with a browser extension while signed in to YouTube."
    ) {
        Text(
            ui.config.cookiesName.ifBlank { "None uploaded." },
            style = MaterialTheme.typography.bodySmall, color = Tape
        )
        Spacer(Modifier.height(8.dp))
        OutlinedButton(
            onClick = { cookiePick.launch(arrayOf("text/plain", "*/*")) },
            colors = ButtonDefaults.outlinedButtonColors(contentColor = Chalk)
        ) { Text("Choose cookies.txt") }
    }
    Spacer(Modifier.height(30.dp))
}

/* ================================================================== models */

@Composable
fun ModelsScreen(ui: UiState, vm: PipelineViewModel) {
    val c = ui.config
    var model by remember(c.model) { mutableStateOf(c.model) }
    var audio by remember(c.modelAudio) { mutableStateOf(c.modelAudio) }
    var vision by remember(c.modelVision) { mutableStateOf(c.modelVision) }
    var match by remember(c.modelMatch) { mutableStateOf(c.modelMatch) }
    var fx by remember(c.modelFx) { mutableStateOf(c.modelFx) }
    var words by remember(c.wordCount) { mutableStateOf(c.wordCount.toString()) }
    var maxRef by remember(c.maxRefChars) { mutableStateOf(c.maxRefChars.toString()) }
    var height by remember(c.minHeight) { mutableStateOf(c.minHeight.toString()) }
    var sceneLen by remember(c.maxSceneLen) { mutableStateOf(c.maxSceneLen.toString()) }
    var sceneUses by remember(c.maxSceneUses) { mutableStateOf(c.maxSceneUses.toString()) }
    var hands by remember(c.handFxMode) { mutableStateOf(c.handFxMode) }

    Section("01 Script", "Model names are free text — type whatever your key has access to.") {
        Field("Writer", model, { model = it })
        Field("Target word count", words, { words = it }, numeric = true)
        Field("Max reference characters (0 sends the whole file)", maxRef, { maxRef = it },
            numeric = true)
    }
    Section("03 Cut") {
        Field("Transcriber (AI 1)", audio, { audio = it })
        Field("Scene watcher (AI 2)", vision, { vision = it })
        Field("Matcher (AI 3)", match, { match = it })
        Field("Minimum source height", height, { height = it }, numeric = true)
        Field("Longest single scene, seconds", sceneLen, { sceneLen = it }, numeric = true)
        Field("Times one scene may be reused", sceneUses, { sceneUses = it }, numeric = true)
    }
    Section("04 Effects") {
        Field("Effects director", fx, { fx = it })
        Text("Neon hand skeleton", style = MaterialTheme.typography.bodySmall, color = Tape)
        Spacer(Modifier.height(6.dp))
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            listOf("AI-decided", "Always on", "Off").forEach { mode ->
                FilterChip(
                    selected = hands == mode,
                    onClick = { hands = mode },
                    label = { Text(mode, fontSize = 12.sp) },
                    colors = FilterChipDefaults.filterChipColors(
                        selectedContainerColor = Vinyl, selectedLabelColor = Color.White,
                        containerColor = Pad3, labelColor = Tape
                    )
                )
            }
        }
        Spacer(Modifier.height(10.dp))
        Toggle("Never warp the voice-over", c.voiceSafe) { v ->
            vm.patch { put("voice_safe", v) }
        }
        Toggle("Add effects from motion spikes", c.autoSuggest) { v ->
            vm.patch { put("auto_suggest", v) }
        }
        Toggle("Film grain", c.filmGrain) { v -> vm.patch { put("film_grain", v) } }
        Toggle("HUD overlay", c.drawHud) { v -> vm.patch { put("draw_hud", v) } }
        Toggle("On-screen labels", c.drawLabels) { v -> vm.patch { put("draw_labels", v) } }
        Toggle("Cap output at 1080p", c.cap1080p) { v -> vm.patch { put("cap_1080p", v) } }
    }
    Section("Save") {
        Button(
            onClick = {
                vm.patch {
                    put("model", model); put("model_audio", audio)
                    put("model_vision", vision); put("model_match", match)
                    put("model_fx", fx); put("hand_fx_mode", hands)
                    put("word_count", words.toIntOrNull() ?: 800)
                    put("max_ref_chars", maxRef.toIntOrNull() ?: 0)
                    put("min_height", height.toIntOrNull() ?: 720)
                    put("max_scene_len", sceneLen.toFloatOrNull() ?: 6f)
                    put("max_scene_uses", sceneUses.toIntOrNull() ?: 2)
                }
            },
            colors = ButtonDefaults.buttonColors(containerColor = Vinyl),
            modifier = Modifier.fillMaxWidth()
        ) { Text("Save models and limits") }
    }
    Spacer(Modifier.height(30.dp))
}

/* =================================================================== files */

@Composable
fun FilesScreen(ui: UiState, vm: PipelineViewModel) {
    LaunchedEffect(Unit) { vm.loadFiles() }
    Section("Finished work", "Tap the arrow to save a file into your phone's Downloads.") {
        if (ui.files.isEmpty()) {
            Text("Nothing rendered yet.", style = MaterialTheme.typography.bodySmall,
                color = Tape)
        }
        ui.files.forEach { f -> FileRow(f) { vm.download(f) } }
    }
    HorizontalDivider(Modifier.padding(16.dp), color = Line)
    Section("Caches", "Clearing one means the next run pays for that analysis again.") {
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            listOf("video" to "Scenes", "audio" to "Transcripts").forEach { (k, label) ->
                OutlinedButton(
                    onClick = { vm.clearCache(k) },
                    colors = ButtonDefaults.outlinedButtonColors(contentColor = Tape)
                ) { Text(label) }
            }
        }
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            listOf("fx" to "Effect plans", "outputs" to "All outputs").forEach { (k, label) ->
                OutlinedButton(
                    onClick = { vm.clearCache(k) },
                    colors = ButtonDefaults.outlinedButtonColors(contentColor = Tape)
                ) { Text(label) }
            }
        }
    }
    Spacer(Modifier.height(30.dp))
}

@Composable
private fun FileRow(f: FileItem, onDownload: () -> Unit) {
    Row(
        Modifier.fillMaxWidth().padding(vertical = 9.dp),
        verticalAlignment = Alignment.CenterVertically
    ) {
        Text(
            f.kind.uppercase(), fontSize = 10.sp, fontWeight = FontWeight.Bold,
            color = Pad, fontFamily = FontFamily.Monospace,
            modifier = Modifier
                .background(if (f.kind == "fx") Pin else Tape, RoundedCornerShape(4.dp))
                .padding(horizontal = 6.dp, vertical = 2.dp)
        )
        Spacer(Modifier.width(10.dp))
        Column(Modifier.weight(1f)) {
            Text(f.name, style = MaterialTheme.typography.bodySmall, color = Chalk, maxLines = 1)
            Text("${f.mb} MB", style = MaterialTheme.typography.bodySmall, color = Tape)
        }
        IconButton(onClick = onDownload) {
            Icon(Icons.Filled.Download, "Download ${f.name}", tint = Ref)
        }
    }
    HorizontalDivider(color = Line)
}

/* ================================================================== deploy */

@Composable
fun DeployScreen(ui: UiState, vm: PipelineViewModel) {
    val ctx = LocalContext.current
    val scope = rememberCoroutineScope()
    var status by remember { mutableStateOf("") }
    var busy by remember { mutableStateOf(false) }
    var secrets by remember { mutableStateOf<List<String>?>(null) }
    var checking by remember { mutableStateOf(false) }

    // Declared here, not inside the Section, because checkSecrets() below and
    // the deploy button both need them.
    var repo by rememberSaveable(ui.ghRepo) { mutableStateOf(ui.ghRepo) }
    var token by rememberSaveable(ui.ghToken) { mutableStateOf(ui.ghToken) }
    var workflow by rememberSaveable(ui.ghWorkflow) { mutableStateOf(ui.ghWorkflow) }

    fun checkSecrets() {
        if (repo.isBlank() || token.isBlank()) return
        scope.launch {
            checking = true
            secrets = runCatching { GitHubDeploy.secretNames(repo, token) }.getOrNull()
            checking = false
        }
    }

    Section(
        "Deploy the backend",
        "Modal does not offer a sign-in for third-party apps — no app can create " +
            "a Modal token for you. You make one yourself in the browser, store it " +
            "in GitHub once, and it never touches this phone again."
    ) {
        Field("Repository", repo, {
            repo = it; vm.saveGitHub(it, token, workflow)
        }, placeholder = "your-name/arm-pipeline")
        Field("Workflow file", workflow, {
            workflow = it; vm.saveGitHub(repo, token, it)
        })
        Field("GitHub token", token, {
            token = it; vm.saveGitHub(repo, it, workflow)
        }, password = true, placeholder = "ghp_…")
        OutlinedButton(
            onClick = { openTab(ctx, GitHubDeploy.TOKEN_PAGE) },
            colors = ButtonDefaults.outlinedButtonColors(contentColor = Chalk),
            modifier = Modifier.fillMaxWidth()
        ) {
            Icon(Icons.Filled.OpenInBrowser, null)
            Spacer(Modifier.width(8.dp))
            Text("Create a GitHub token")
        }
        Spacer(Modifier.height(10.dp))
        OutlinedButton(
            onClick = {
                if (repo.isBlank() || token.isBlank())
                    vm.say("Enter your repository and GitHub token first.")
                else checkSecrets()
            },
            enabled = !checking,
            colors = ButtonDefaults.outlinedButtonColors(contentColor = Chalk),
            modifier = Modifier.fillMaxWidth()
        ) { Text(if (checking) "Checking…" else "Check the Modal credentials") }

        secrets?.let { have ->
            Spacer(Modifier.height(12.dp))
            GitHubDeploy.REQUIRED_SECRETS.forEach { name ->
                val ok = name in have
                Row(
                    Modifier.fillMaxWidth().padding(vertical = 4.dp),
                    verticalAlignment = Alignment.CenterVertically
                ) {
                    Icon(
                        if (ok) Icons.Filled.CheckCircle else Icons.Filled.ErrorOutline,
                        null, tint = if (ok) Pin else Vinyl,
                        modifier = Modifier.size(18.dp)
                    )
                    Spacer(Modifier.width(9.dp))
                    Text(name, fontFamily = FontFamily.Monospace, fontSize = 12.sp,
                        color = Chalk, modifier = Modifier.weight(1f))
                    Text(if (ok) "set" else "missing",
                        style = MaterialTheme.typography.bodySmall,
                        color = if (ok) Pin else Vinyl)
                }
            }
            if (GitHubDeploy.REQUIRED_SECRETS.any { it !in have }) {
                Spacer(Modifier.height(8.dp))
                Text(
                    "Deploying will fail until both are set. Copy the two values " +
                        "from Modal, then add them to GitHub under exactly these names.",
                    style = MaterialTheme.typography.bodySmall, color = Tape
                )
                Spacer(Modifier.height(8.dp))
                OutlinedButton(
                    onClick = { openTab(ctx, GitHubDeploy.MODAL_TOKENS_PAGE) },
                    colors = ButtonDefaults.outlinedButtonColors(contentColor = Chalk),
                    modifier = Modifier.fillMaxWidth()
                ) {
                    Icon(Icons.Filled.OpenInBrowser, null)
                    Spacer(Modifier.width(8.dp))
                    Text("1 — Get the token from Modal")
                }
                Spacer(Modifier.height(6.dp))
                OutlinedButton(
                    onClick = { openTab(ctx, GitHubDeploy.addSecretPage(repo)) },
                    colors = ButtonDefaults.outlinedButtonColors(contentColor = Chalk),
                    modifier = Modifier.fillMaxWidth()
                ) {
                    Icon(Icons.Filled.OpenInBrowser, null)
                    Spacer(Modifier.width(8.dp))
                    Text("2 — Add it to GitHub")
                }
            }
        }
        Spacer(Modifier.height(14.dp))
        Button(
            onClick = {
                if (repo.isBlank() || token.isBlank()) {
                    vm.say("Enter your repository and GitHub token first.")
                    return@Button
                }
                scope.launch {
                    busy = true
                    status = "Asking GitHub to deploy…"
                    runCatching { GitHubDeploy.dispatch(repo, token, workflow) }
                        .onFailure { status = it.message ?: "GitHub refused the request." }
                        .onSuccess {
                            for (attempt in 0 until 90) {
                                kotlinx.coroutines.delay(5000)
                                val run = runCatching {
                                    GitHubDeploy.latestRun(repo, token, workflow)
                                }.getOrNull()
                                status = when {
                                    run == null -> "Waiting for the run to appear…"
                                    run.status != "completed" -> "Building — ${run.status}"
                                    run.conclusion == "success" -> "Deployed. Your backend is live."
                                    else -> "Failed: ${run.conclusion}. Open the run on GitHub."
                                }
                                if (run?.status == "completed") break
                            }
                        }
                    busy = false
                }
            },
            enabled = !busy,
            colors = ButtonDefaults.buttonColors(containerColor = Vinyl),
            modifier = Modifier.fillMaxWidth().height(50.dp)
        ) {
            Icon(Icons.Filled.CloudUpload, null)
            Spacer(Modifier.width(8.dp))
            Text("Deploy to Modal")
        }
        if (busy) {
            Spacer(Modifier.height(12.dp))
            LinearProgressIndicator(
                color = Ref, trackColor = Line,
                modifier = Modifier.fillMaxWidth().height(4.dp)
            )
        }
        if (status.isNotBlank()) {
            Spacer(Modifier.height(10.dp))
            Text(status, style = MaterialTheme.typography.bodyMedium,
                color = if (status.startsWith("Deployed")) Pin else Tape)
        }
        if (repo.isNotBlank()) {
            Spacer(Modifier.height(8.dp))
            TextButton(onClick = {
                openTab(ctx, "https://github.com/$repo/actions")
            }) { Text("Open Actions on GitHub", color = Ref) }
        }
    }
    Spacer(Modifier.height(30.dp))
}

/* ============================================================== connection */

@Composable
fun ConnectionScreen(ui: UiState, vm: PipelineViewModel) {
    val ctx = LocalContext.current
    var input by rememberSaveable(ui.baseUrl) { mutableStateOf(ui.baseUrl) }
    var password by rememberSaveable { mutableStateOf("") }
    var showPw by remember { mutableStateOf(false) }

    if (!ui.connected) {
        Section(
            "Connect to Modal",
            "Type your Modal workspace name — the one in your Modal dashboard URL. " +
                "That is all this needs."
        ) {
            Field("Workspace name", input, { input = it }, placeholder = "e.g. yourname")
            vm.resolveUrl(input)?.let {
                Text(it, style = MaterialTheme.typography.bodySmall, color = Tape,
                    fontFamily = FontFamily.Monospace,
                    modifier = Modifier.padding(bottom = 10.dp))
            }
            Button(
                onClick = { vm.connectTo(input) },
                colors = ButtonDefaults.buttonColors(containerColor = Vinyl),
                modifier = Modifier.fillMaxWidth().height(50.dp)
            ) {
                Text(if (ui.connecting) "Connecting…" else "Connect", fontSize = 16.sp,
                    fontWeight = FontWeight.Bold)
            }
            Text(
                "Already pasted a full https://…modal.run address? That works too.",
                style = MaterialTheme.typography.bodySmall, color = Tape,
                modifier = Modifier.padding(top = 8.dp)
            )
        }

        // Only ever shown when rejoining a backend someone already claimed.
        if (ui.needsPassword) {
            Section("One more thing", "This backend was set up before. Enter its password once.") {
                Field("Password", password, { password = it }, password = true)
                Button(
                    onClick = { vm.connectWithPassword(input, password) },
                    colors = ButtonDefaults.buttonColors(containerColor = Vinyl),
                    modifier = Modifier.fillMaxWidth().height(48.dp)
                ) { Text("Sign in") }
            }
        }
    } else {
        Section("Connected", ui.baseUrl) {
            Text(
                "The backend password was generated for you. You only need it to " +
                    "open the same pipeline in a browser.",
                style = MaterialTheme.typography.bodySmall, color = Tape
            )
            Spacer(Modifier.height(10.dp))
            if (showPw) {
                Text(
                    ui.backendPassword.ifBlank { "(set on another device)" },
                    fontFamily = FontFamily.Monospace, fontSize = 14.sp, color = Pin
                )
                Spacer(Modifier.height(10.dp))
            }
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                OutlinedButton(
                    onClick = { showPw = !showPw },
                    colors = ButtonDefaults.outlinedButtonColors(contentColor = Chalk)
                ) { Text(if (showPw) "Hide password" else "Show password") }
                OutlinedButton(
                    onClick = { openTab(ctx, ui.baseUrl) },
                    colors = ButtonDefaults.outlinedButtonColors(contentColor = Chalk)
                ) { Text("Open in browser") }
            }
            Spacer(Modifier.height(14.dp))
            OutlinedButton(
                onClick = { vm.disconnect() },
                colors = ButtonDefaults.outlinedButtonColors(contentColor = Vinyl)
            ) { Text("Disconnect") }
        }
    }

    HorizontalDivider(Modifier.padding(16.dp), color = Line)
    Section("Where the work happens") {
        listOf(
            "Gemini is called from inside Modal, never from this phone.",
            "Your API key is stored on the Modal volume and only read there.",
            "This app sends short JSON over HTTPS and receives log lines back.",
            "Only the finished video travels to your phone, when you tap download.",
        ).forEach {
            Row(Modifier.padding(vertical = 4.dp)) {
                Text("—  ", color = Vinyl)
                Text(it, style = MaterialTheme.typography.bodySmall, color = Tape)
            }
        }
    }
    Spacer(Modifier.height(30.dp))
}
