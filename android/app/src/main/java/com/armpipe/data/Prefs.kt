package com.armpipe.data

import android.content.Context
import androidx.datastore.preferences.core.*
import androidx.datastore.preferences.preferencesDataStore
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.map

private val Context.store by preferencesDataStore("armpipe")

/** Only local device state lives here. Your Gemini key never touches the phone —
 *  it is stored on the Modal volume and only ever used inside Modal containers. */
object Prefs {

    private val BASE_URL = stringPreferencesKey("base_url")
    private val TOKEN = stringPreferencesKey("token")
    private val GH_REPO = stringPreferencesKey("gh_repo")
    private val GH_TOKEN = stringPreferencesKey("gh_token")
    private val GH_WORKFLOW = stringPreferencesKey("gh_workflow")
    private val LAST_TITLE = stringPreferencesKey("last_title")
    private val BACKEND_PW = stringPreferencesKey("backend_pw")
    // Which job is in flight, so a reopened app can rejoin it.
    private val ACTIVE_JOB = stringPreferencesKey("active_job")
    private val ACTIVE_STAGE = stringPreferencesKey("active_stage")

    data class State(
        val baseUrl: String = "",
        val token: String = "",
        val ghRepo: String = "",
        val ghToken: String = "",
        val ghWorkflow: String = "deploy.yml",
        val lastTitle: String = "",
        /** Generated on first connect. You never have to type or invent one. */
        val backendPassword: String = "",
        val activeJob: String = "",
        val activeStage: String = "",
    ) {
        val connected get() = baseUrl.isNotBlank() && token.isNotBlank()
    }

    fun flow(ctx: Context): Flow<State> = ctx.store.data.map { p ->
        State(
            baseUrl = p[BASE_URL].orEmpty(),
            token = p[TOKEN].orEmpty(),
            ghRepo = p[GH_REPO].orEmpty(),
            ghToken = p[GH_TOKEN].orEmpty(),
            ghWorkflow = p[GH_WORKFLOW] ?: "deploy.yml",
            lastTitle = p[LAST_TITLE].orEmpty(),
            backendPassword = p[BACKEND_PW].orEmpty(),
            activeJob = p[ACTIVE_JOB].orEmpty(),
            activeStage = p[ACTIVE_STAGE].orEmpty(),
        )
    }

    suspend fun setConnection(ctx: Context, url: String, token: String) {
        ctx.store.edit { it[BASE_URL] = url.trimEnd('/'); it[TOKEN] = token }
    }

    suspend fun setBackendPassword(ctx: Context, pw: String) {
        ctx.store.edit { it[BACKEND_PW] = pw }
    }

    suspend fun setActiveJob(ctx: Context, jobId: String, stage: String) {
        ctx.store.edit { it[ACTIVE_JOB] = jobId; it[ACTIVE_STAGE] = stage }
    }

    suspend fun clearActiveJob(ctx: Context) {
        ctx.store.edit { it.remove(ACTIVE_JOB); it.remove(ACTIVE_STAGE) }
    }

    suspend fun setToken(ctx: Context, token: String) {
        ctx.store.edit { it[TOKEN] = token }
    }

    suspend fun setBaseUrl(ctx: Context, url: String) {
        ctx.store.edit { it[BASE_URL] = url.trimEnd('/') }
    }

    suspend fun setGitHub(ctx: Context, repo: String, token: String, workflow: String) {
        ctx.store.edit {
            it[GH_REPO] = repo.trim()
            it[GH_TOKEN] = token.trim()
            it[GH_WORKFLOW] = workflow.trim().ifBlank { "deploy.yml" }
        }
    }

    suspend fun setLastTitle(ctx: Context, title: String) {
        ctx.store.edit { it[LAST_TITLE] = title }
    }

    suspend fun disconnect(ctx: Context) {
        ctx.store.edit { it.remove(TOKEN) }
    }
}
