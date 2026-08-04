package com.armpipe.work

import android.app.*
import android.content.Context
import android.content.Intent
import android.os.Build
import android.os.IBinder
import androidx.core.app.NotificationCompat
import com.armpipe.MainActivity
import com.armpipe.R
import com.armpipe.data.Prefs
import com.armpipe.net.Api
import kotlinx.coroutines.*
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.flow.update

/**
 * The single source of truth for a running job.
 *
 * The pipeline itself lives on Modal and never depended on the phone — closing
 * the app was never able to stop the work. What used to break was only the
 * watching: polling ran inside the ViewModel, so it died with the screen. This
 * object holds the state instead, the service below keeps it fed, and the UI
 * simply reads it whenever it happens to be alive.
 */
object JobRepo {

    data class Snapshot(
        val jobId: String? = null,
        /** What was asked for: "all", "cut", "fx"… */
        val requested: String = "",
        /** Which stage the server is on right now, during a full run. */
        val phase: String? = null,
        val status: String = "idle",
        val progress: Float? = null,
        val lines: List<String> = emptyList(),
        val error: String? = null,
    ) {
        val running get() = status == "running" || status == "queued"
    }

    val state = MutableStateFlow(Snapshot())

    /** Called when a stage is started from the UI. */
    suspend fun begin(ctx: Context, jobId: String, requested: String) {
        state.value = Snapshot(jobId = jobId, requested = requested, status = "queued")
        Prefs.setActiveJob(ctx, jobId, requested)
        JobService.start(ctx)
    }

    suspend fun finished(ctx: Context) = Prefs.clearActiveJob(ctx)
}

/**
 * A foreground service that polls the backend for as long as a job is running.
 *
 * Foreground means Android will not freeze it when the app leaves the screen,
 * and START_STICKY plus the job id in storage means that even if the process is
 * killed outright, the service comes back and picks the same job up again.
 */
class JobService : Service() {

    private var scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private var poller: Job? = null

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        createChannel()
        startForeground(NOTIFICATION_ID, build("Starting…", null, true))
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (poller?.isActive != true) poller = scope.launch { pollLoop() }
        return Service.START_STICKY
    }

    override fun onDestroy() {
        scope.cancel()
        super.onDestroy()
    }

    private suspend fun pollLoop() {
        // Read straight from storage: after a process restart nothing is in
        // memory, and the job id we need is on disk.
        val prefs = Prefs.flow(applicationContext).first()
        val jobId = prefs.activeJob
        if (jobId.isBlank() || prefs.baseUrl.isBlank() || prefs.token.isBlank()) {
            stopNow(); return
        }
        Api.baseUrl = prefs.baseUrl
        Api.token = prefs.token

        val requested = prefs.activeStage
        JobRepo.state.update {
            if (it.jobId == jobId) it
            else it.copy(jobId = jobId, requested = requested, status = "running")
        }

        // since=0 on a fresh attach replays the whole log the server still holds,
        // so reopening the app shows everything that happened while it was shut.
        var since = if (JobRepo.state.value.lines.isEmpty()) 0
                    else JobRepo.state.value.lines.size
        var misses = 0

        while (currentCoroutineContext().isActive) {
            val j = runCatching { Api.job(jobId, since) }.getOrNull()
            if (j == null) {
                // Tolerate flaky signal; give up only after a sustained outage.
                if (++misses > 40) { stopNow(); return }
                delay(3000)
                continue
            }
            misses = 0
            since = j.next
            JobRepo.state.update { s ->
                s.copy(
                    status = j.status,
                    phase = j.phase ?: s.phase,
                    progress = j.progress ?: s.progress,
                    lines = (s.lines + j.lines).takeLast(600),
                    error = j.error ?: s.error,
                )
            }
            notify(labelFor(j.phase, requested), j.progress, j.status == "running")

            if (j.status == "done" || j.status == "failed" || j.status == "cancelled") {
                JobRepo.finished(applicationContext)
                postFinal(j.status, j.error)
                stopNow()
                return
            }
            delay(2000)
        }
    }

    private fun labelFor(phase: String?, requested: String): String {
        val name = mapOf(
            "script" to "Writing the script",
            "voice" to "Speaking it",
            "analyze" to "Watching the source videos",
            "cut" to "Building the cut",
            "fx" to "Adding the effects",
        )
        return name[phase ?: requested] ?: "Working"
    }

    private fun stopNow() {
        poller?.cancel()
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.N)
            stopForeground(STOP_FOREGROUND_REMOVE) else @Suppress("DEPRECATION") stopForeground(true)
        stopSelf()
    }

    private fun nm() = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager

    private fun notify(text: String, progress: Float?, ongoing: Boolean) =
        nm().notify(NOTIFICATION_ID, build(text, progress, ongoing))

    /** A separate, dismissible notification so you learn it finished. */
    private fun postFinal(status: String, error: String?) {
        val done = status == "done"
        val n = NotificationCompat.Builder(this, CHANNEL_DONE)
            .setContentTitle(if (done) "Video finished" else "Pipeline $status")
            .setContentText(
                if (done) "Open Files to download it"
                else error?.take(80) ?: "Open the app for details"
            )
            .setSmallIcon(R.drawable.ic_stat_pipeline)
            .setAutoCancel(true)
            .setContentIntent(tapIntent())
            .build()
        nm().notify(NOTIFICATION_DONE, n)
    }

    private fun tapIntent(): PendingIntent = PendingIntent.getActivity(
        this, 0, Intent(this, MainActivity::class.java),
        PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT
    )

    private fun build(text: String, progress: Float?, ongoing: Boolean): Notification {
        val b = NotificationCompat.Builder(this, CHANNEL)
            .setContentTitle("Arm Pipeline")
            .setContentText(text)
            .setSmallIcon(R.drawable.ic_stat_pipeline)
            .setOngoing(ongoing)
            .setOnlyAlertOnce(true)
            .setContentIntent(tapIntent())
        if (progress != null) b.setProgress(100, (progress * 100).toInt(), false)
        else b.setProgress(0, 0, true)
        return b.build()
    }

    private fun createChannel() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            nm().createNotificationChannel(
                NotificationChannel(CHANNEL, "Pipeline progress",
                    NotificationManager.IMPORTANCE_LOW))
            nm().createNotificationChannel(
                NotificationChannel(CHANNEL_DONE, "Pipeline finished",
                    NotificationManager.IMPORTANCE_DEFAULT))
        }
    }

    companion object {
        private const val CHANNEL = "armpipe_jobs"
        private const val CHANNEL_DONE = "armpipe_done"
        private const val NOTIFICATION_ID = 42
        private const val NOTIFICATION_DONE = 43

        fun start(ctx: Context) {
            val i = Intent(ctx, JobService::class.java)
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O)
                ctx.startForegroundService(i) else ctx.startService(i)
        }

        fun stop(ctx: Context) = ctx.stopService(Intent(ctx, JobService::class.java))
    }
}

/** Hands the final MP4 to Android's own download manager. */
object Downloader {
    fun enqueue(ctx: Context, url: String, filename: String, bearer: String) {
        val req = DownloadManager.Request(android.net.Uri.parse(url))
            .setTitle(filename)
            .setDescription("Arm Pipeline")
            .setNotificationVisibility(
                DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED)
            .setDestinationInExternalPublicDir(
                android.os.Environment.DIRECTORY_DOWNLOADS, filename)
            .setAllowedOverMetered(true)
        if (bearer.isNotBlank()) req.addRequestHeader("Authorization", "Bearer $bearer")
        val dm = ctx.getSystemService(Context.DOWNLOAD_SERVICE) as DownloadManager
        dm.enqueue(req)
    }
}
