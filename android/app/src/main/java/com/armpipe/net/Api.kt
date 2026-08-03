package com.armpipe.net

import android.content.Context
import android.net.Uri
import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.*
import okhttp3.*
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.RequestBody.Companion.toRequestBody
import java.io.IOException
import java.util.concurrent.TimeUnit
import kotlin.coroutines.resume
import kotlin.coroutines.resumeWithException
import kotlin.coroutines.suspendCoroutine

/* ---------------------------------------------------------------- payloads */

@Serializable
data class Health(val ok: Boolean = false, val app: String = "")

@Serializable
data class LoginResp(val ok: Boolean = false, val token: String = "")

@Serializable
data class BackendState(
    @SerialName("needs_password") val needsPassword: Boolean = false,
    val locked: Boolean = false,
)

@Serializable
data class RunResp(@SerialName("job_id") val jobId: String)

@Serializable
data class JobResp(
    val status: String = "queued",
    val stage: String? = null,
    /** Which of the four stages the full run is on right now. */
    val phase: String? = null,
    val progress: Float? = null,
    val error: String? = null,
    val lines: List<String> = emptyList(),
    val next: Int = 0,
    val result: JsonObject? = null,
)

@Serializable
data class FileItem(
    val kind: String,
    val name: String,
    val path: String,
    val mb: Double,
    @SerialName("when") val whenTs: Long = 0,
)

@Serializable
data class PasteResp(val text: String = "", val chars: Int = 0, val name: String = "")

@Serializable
data class UploadResp(
    val ok: Boolean = false,
    val name: String = "",
    @SerialName("size_mb") val sizeMb: Double = 0.0,
)

/** Everything the backend remembers. Unknown keys are ignored so the app keeps
 *  working when the Python side grows new settings. */
@Serializable
data class Config(
    @SerialName("api_key_set") val apiKeySet: Boolean = false,
    val links: List<String> = emptyList(),
    // stage 1
    @SerialName("word_count") val wordCount: Int = 800,
    @SerialName("max_ref_chars") val maxRefChars: Int = 0,
    val model: String = "gemini-2.5-flash",
    // stage 2
    val language: String = "Auto",
    @SerialName("voice_ref_text") val voiceRefText: String = "",
    // stage 3
    @SerialName("model_audio") val modelAudio: String = "gemini-3.1-flash-lite",
    @SerialName("model_vision") val modelVision: String = "gemini-3.1-flash-lite",
    @SerialName("model_match") val modelMatch: String = "gemini-3.5-flash",
    @SerialName("min_height") val minHeight: Int = 720,
    @SerialName("max_scene_len") val maxSceneLen: Float = 6f,
    @SerialName("max_scene_uses") val maxSceneUses: Int = 2,
    // stage 4
    @SerialName("model_fx") val modelFx: String = "gemini-3.5-flash",
    @SerialName("hand_fx_mode") val handFxMode: String = "AI-decided",
    @SerialName("voice_safe") val voiceSafe: Boolean = true,
    @SerialName("auto_suggest") val autoSuggest: Boolean = true,
    @SerialName("film_grain") val filmGrain: Boolean = true,
    @SerialName("draw_hud") val drawHud: Boolean = true,
    @SerialName("draw_labels") val drawLabels: Boolean = true,
    @SerialName("cap_1080p") val cap1080p: Boolean = true,
    // uploaded file names, read-only
    @SerialName("ref_text_path_name") val refName: String = "",
    @SerialName("voice_ref_path_name") val voiceName: String = "",
    @SerialName("cookies_file_name") val cookiesName: String = "",
)

class ApiException(val code: Int, message: String) : IOException(message)

/* ------------------------------------------------------------------ client */

object Api {

    val json = Json { ignoreUnknownKeys = true; encodeDefaults = true; isLenient = true }

    private val client = OkHttpClient.Builder()
        .connectTimeout(20, TimeUnit.SECONDS)
        .readTimeout(60, TimeUnit.SECONDS)
        .writeTimeout(300, TimeUnit.SECONDS)   // voice clips can be slow to push
        .retryOnConnectionFailure(true)
        .build()

    private val JSON_TYPE = "application/json; charset=utf-8".toMediaType()

    var baseUrl: String = ""
    var token: String = ""

    private fun url(path: String) = baseUrl.trimEnd('/') + path

    private fun req(path: String) = Request.Builder()
        .url(url(path))
        .apply { if (token.isNotBlank()) header("Authorization", "Bearer $token") }

    private suspend fun call(request: Request): String = suspendCoroutine { cont ->
        client.newCall(request).enqueue(object : Callback {
            override fun onFailure(call: Call, e: IOException) = cont.resumeWithException(e)
            override fun onResponse(call: Call, response: Response) {
                response.use {
                    val body = it.body?.string().orEmpty()
                    if (!it.isSuccessful) {
                        val detail = runCatching {
                            json.parseToJsonElement(body).jsonObject["detail"]
                                ?.jsonPrimitive?.content
                        }.getOrNull() ?: it.message
                        cont.resumeWithException(ApiException(it.code, detail))
                    } else cont.resume(body)
                }
            }
        })
    }

    private suspend fun get(path: String) = call(req(path).get().build())

    private suspend fun post(path: String, body: JsonElement? = null): String {
        val payload = (body ?: buildJsonObject { }).toString().toRequestBody(JSON_TYPE)
        return call(req(path).post(payload).build())
    }

    /* ---------------- connection ---------------- */

    /** Verifies a URL points at a real backend before we try to sign in. */
    suspend fun health(candidate: String): Health {
        val r = Request.Builder().url(candidate.trimEnd('/') + "/api/health").get().build()
        return json.decodeFromString(call(r))
    }

    suspend fun login(password: String): String {
        val body = buildJsonObject { put("password", password) }
            .toString().toRequestBody(JSON_TYPE)
        val r = Request.Builder().url(url("/api/login")).post(body).build()
        val resp: LoginResp = json.decodeFromString(call(r))
        token = resp.token
        return resp.token
    }

    /** Has this backend been claimed yet? Answered without any credentials. */
    suspend fun state(candidate: String): BackendState {
        val r = Request.Builder().url(candidate.trimEnd('/') + "/api/state").get().build()
        return json.decodeFromString(call(r))
    }

    fun authPageUrl(): String =
        url("/auth?redirect=") + Uri.encode("armpipe://auth")

    /* ---------------- settings ---------------- */

    suspend fun config(): Config = json.decodeFromString(get("/api/config"))

    suspend fun saveConfig(patch: JsonObject) { post("/api/config", patch) }

    suspend fun saveApiKey(key: String) =
        saveConfig(buildJsonObject { put("api_key", key) })

    suspend fun readKnowledge(): PasteResp = json.decodeFromString(get("/api/paste"))

    suspend fun writeKnowledge(text: String): PasteResp = json.decodeFromString(
        post("/api/paste", buildJsonObject {
            put("kind", "reference"); put("text", text)
        })
    )

    /** Streams a picked file (voice clip, cookies.txt) straight to the volume. */
    suspend fun upload(ctx: Context, kind: String, uri: Uri, filename: String): UploadResp {
        val bytes = ctx.contentResolver.openInputStream(uri)?.use { it.readBytes() }
            ?: throw IOException("Could not read that file.")
        val body = MultipartBody.Builder().setType(MultipartBody.FORM)
            .addFormDataPart("kind", kind)
            .addFormDataPart(
                "file", filename,
                bytes.toRequestBody("application/octet-stream".toMediaType())
            )
            .build()
        return json.decodeFromString(call(req("/api/upload").post(body).build()))
    }

    /* ---------------- jobs ---------------- */

    suspend fun run(stage: String, title: String = "", video: String = ""): String {
        val body = buildJsonObject {
            if (title.isNotBlank()) put("title", title)
            if (video.isNotBlank()) put("video", video)
        }
        val resp: RunResp = json.decodeFromString(post("/api/run/$stage", body))
        return resp.jobId
    }

    suspend fun job(id: String, since: Int): JobResp =
        json.decodeFromString(get("/api/job/$id?since=$since"))

    suspend fun cancel(id: String) { post("/api/job/$id/cancel") }

    /* ---------------- files ---------------- */

    suspend fun files(): List<FileItem> = json.decodeFromString(get("/api/files"))

    fun fileUrl(path: String) = url("/api/file/") + path.split("/").joinToString("/") {
        Uri.encode(it)
    }

    suspend fun clearCache(what: String) { post("/api/clear/$what") }
}
