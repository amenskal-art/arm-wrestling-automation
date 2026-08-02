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
