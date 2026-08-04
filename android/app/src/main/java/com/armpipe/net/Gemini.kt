package com.armpipe.net

import kotlinx.coroutines.suspendCancellableCoroutine
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.*
import okhttp3.*
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.RequestBody.Companion.toRequestBody
import java.io.IOException
import java.util.concurrent.TimeUnit
import kotlin.coroutines.resume
import kotlin.coroutines.resumeWithException

/** One video the phone still has to analyse. */
@Serializable
data class AnalysisTask(val url: String, val key: String, val uri: String = "")

/**
 * Everything needed to run AI 2 from the phone, handed over by the backend.
 *
 * The prompt, model and schema are served by the Python rather than copied into
 * Kotlin, so the two can never fall out of step.
 */
@Serializable
data class AnalysisPlan(
    val model: String = "",
    val api_key: String = "",
    val system: String = "",
    val prompt: String = "",
    val schema: JsonObject = JsonObject(emptyMap()),
    val min_interval_ms: Long = 13_000,
    val todo: List<AnalysisTask> = emptyList(),
    val cached: Int = 0,
    val total: Int = 0,
)

/**
 * Talks to Gemini directly from the handset.
 *
 * Gemini fetches YouTube videos server-side, so the phone only ever sends a URL
 * and a prompt and waits — a few kilobytes for a call that may run for minutes.
 * That waiting is free here and billed by the second on a cloud container,
 * which is the whole point of doing it this way.
 */
object Gemini {

    private val client = OkHttpClient.Builder()
        .connectTimeout(30, TimeUnit.SECONDS)
        // Analysing a long video legitimately takes minutes; don't cut it off.
        .readTimeout(10, TimeUnit.MINUTES)
        .callTimeout(12, TimeUnit.MINUTES)
        .retryOnConnectionFailure(true)
        .build()

    private val JSON_TYPE = "application/json; charset=utf-8".toMediaType()
    private val json = Json { ignoreUnknownKeys = true; isLenient = true }

    class QuotaExhausted(message: String) : IOException(message)

    private suspend fun call(req: Request): String =
        suspendCancellableCoroutine { cont ->
            val c = client.newCall(req)
            cont.invokeOnCancellation { runCatching { c.cancel() } }
            c.enqueue(object : Callback {
                override fun onFailure(call: Call, e: IOException) =
                    cont.resumeWithException(e)

                override fun onResponse(call: Call, response: Response) {
                    response.use {
                        val body = it.body?.string().orEmpty()
                        if (it.isSuccessful) { cont.resume(body); return }
                        val msg = runCatching {
                            json.parseToJsonElement(body).jsonObject["error"]
                                ?.jsonObject?.get("message")?.jsonPrimitive?.content
                        }.getOrNull() ?: "HTTP ${it.code}"
                        cont.resumeWithException(
                            if (it.code == 429) QuotaExhausted(msg) else IOException(msg)
                        )
                    }
                }
            })
        }

    /**
     * Analyses one video and returns the scene list.
     *
     * `uri` is a plain YouTube watch URL: Gemini streams it itself, so nothing
     * is downloaded to the phone.
     */
    suspend fun analyseVideo(plan: AnalysisPlan, task: AnalysisTask): JsonArray {
        require(task.uri.isNotBlank()) { "no playable URL" }

        val payload = buildJsonObject {
            putJsonArray("contents") {
                addJsonObject {
                    putJsonArray("parts") {
                        addJsonObject {
                            putJsonObject("file_data") {
                                put("file_uri", task.uri)
                            }
                        }
                        addJsonObject { put("text", plan.prompt) }
                    }
                }
            }
            putJsonObject("system_instruction") {
                putJsonArray("parts") {
                    addJsonObject { put("text", plan.system) }
                }
            }
            putJsonObject("generationConfig") {
                put("response_mime_type", "application/json")
                if (plan.schema.isNotEmpty()) put("response_schema", plan.schema)
            }
        }

        val url = "https://generativelanguage.googleapis.com/v1beta/models/" +
            "${plan.model}:generateContent"
        val req = Request.Builder()
            .url(url)
            .header("x-goog-api-key", plan.api_key)
            .post(payload.toString().toRequestBody(JSON_TYPE))
            .build()

        val text = extractText(call(req))
        val parsed = json.parseToJsonElement(text.trim().removeSurrounding("```json", "```").trim())
        return parsed.jsonObject["scenes"]?.jsonArray
            ?: throw IOException("Gemini returned no scenes")
    }

    /** Pulls the model's text out of the candidate structure. */
    private fun extractText(body: String): String {
        val root = json.parseToJsonElement(body).jsonObject
        val parts = root["candidates"]?.jsonArray?.firstOrNull()
            ?.jsonObject?.get("content")?.jsonObject?.get("parts")?.jsonArray
            ?: throw IOException("Gemini returned an empty response")
        return parts.mapNotNull { it.jsonObject["text"]?.jsonPrimitive?.contentOrNull }
            .joinToString("")
    }
}
