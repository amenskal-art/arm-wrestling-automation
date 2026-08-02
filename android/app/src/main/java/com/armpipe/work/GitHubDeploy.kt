package com.armpipe.work

import kotlinx.coroutines.suspendCancellableCoroutine
import kotlinx.serialization.json.*
import okhttp3.*
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.RequestBody.Companion.toRequestBody
import java.io.IOException
import kotlin.coroutines.resume
import kotlin.coroutines.resumeWithException

/**
 * The Deploy button. Modal has no third-party OAuth, so the app never holds a
 * Modal key: GitHub holds it as an Actions secret, and this asks GitHub to run
 * the deploy workflow. That also keeps your Modal credentials off the phone.
 */
object GitHubDeploy {

    data class Run(val status: String, val conclusion: String?, val url: String, val name: String)

    private val client = OkHttpClient()
    private val json = Json { ignoreUnknownKeys = true }
    private val JSON_TYPE = "application/json".toMediaType()

    /** Where to send someone to mint a token with exactly the right scopes. */
    const val TOKEN_PAGE =
        "https://github.com/settings/tokens/new?scopes=repo,workflow&description=Arm%20Pipeline%20deploy"

    /** Modal has no OAuth for third-party apps, so the token is created here. */
    const val MODAL_TOKENS_PAGE = "https://modal.com/settings/tokens"

    fun addSecretPage(repo: String) =
        "https://github.com/$repo/settings/secrets/actions/new"

    fun secretsPage(repo: String) =
        "https://github.com/$repo/settings/secrets/actions"

    val REQUIRED_SECRETS = listOf("MODAL_TOKEN_ID", "MODAL_TOKEN_SECRET")

    private suspend fun call(req: Request): String =
        suspendCancellableCoroutine { cont ->
            client.newCall(req).enqueue(object : Callback {
                override fun onFailure(call: Call, e: IOException) =
                    cont.resumeWithException(e)

                override fun onResponse(call: Call, response: Response) {
                    response.use {
                        val body = it.body?.string().orEmpty()
                        if (!it.isSuccessful) {
                            val msg = runCatching {
                                json.parseToJsonElement(body).jsonObject["message"]
                                    ?.jsonPrimitive?.content
                            }.getOrNull() ?: "GitHub returned ${it.code}"
                            cont.resumeWithException(IOException(msg))
                        } else cont.resume(body)
                    }
                }
            })
        }

    private fun builder(url: String, token: String) = Request.Builder()
        .url(url)
        .header("Authorization", "Bearer $token")
        .header("Accept", "application/vnd.github+json")
        .header("X-GitHub-Api-Version", "2022-11-28")

    /** repo is "owner/name". Returns once GitHub has accepted the request. */
    suspend fun dispatch(repo: String, token: String, workflow: String, branch: String = "main") {
        val body = buildJsonObject { put("ref", branch) }.toString().toRequestBody(JSON_TYPE)
        call(
            builder(
                "https://api.github.com/repos/$repo/actions/workflows/$workflow/dispatches",
                token
            ).post(body).build()
        )
    }

    /**
     * Which Actions secrets the repo already has. Only names come back, never
     * values, so this is safe to show in the app.
     */
    suspend fun secretNames(repo: String, token: String): List<String> {
        val body = call(
            builder("https://api.github.com/repos/$repo/actions/secrets?per_page=100", token)
                .get().build()
        )
        return json.parseToJsonElement(body).jsonObject["secrets"]?.jsonArray
            ?.mapNotNull { it.jsonObject["name"]?.jsonPrimitive?.contentOrNull }
            ?: emptyList()
    }

    /**
     * The deploy workflow commits the live address to backend_url.txt, so the
     * app can read it instead of asking anyone to type a URL.
     */
    suspend fun backendUrl(repo: String, token: String): String? {
        val body = call(
            builder("https://api.github.com/repos/$repo/contents/backend_url.txt", token)
                .header("Accept", "application/vnd.github.raw+json")
                .get().build()
        )
        val text = body.trim()
        // The raw media type returns the file itself; fall back to the JSON form.
        val url = if (text.startsWith("http")) text else runCatching {
            val b64 = json.parseToJsonElement(text).jsonObject["content"]
                ?.jsonPrimitive?.content.orEmpty().replace("\n", "")
            String(android.util.Base64.decode(b64, android.util.Base64.DEFAULT)).trim()
        }.getOrNull().orEmpty()
        return url.takeIf { it.startsWith("http") }
    }

    /** Latest run of that workflow, for the progress readout. */
    suspend fun latestRun(repo: String, token: String, workflow: String): Run? {
        val body = call(
            builder(
                "https://api.github.com/repos/$repo/actions/workflows/$workflow/runs?per_page=1",
                token
            ).get().build()
        )
        val run = json.parseToJsonElement(body).jsonObject["workflow_runs"]
            ?.jsonArray?.firstOrNull()?.jsonObject ?: return null
        return Run(
            status = run["status"]?.jsonPrimitive?.contentOrNull ?: "unknown",
            conclusion = run["conclusion"]?.jsonPrimitive?.contentOrNull,
            url = run["html_url"]?.jsonPrimitive?.contentOrNull.orEmpty(),
            name = run["display_title"]?.jsonPrimitive?.contentOrNull.orEmpty(),
        )
    }
}
