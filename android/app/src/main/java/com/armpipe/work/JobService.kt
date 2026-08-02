package com.armpipe.work

import android.app.*
import android.content.Context
import android.content.Intent
import android.os.Build
import android.os.IBinder
import androidx.core.app.NotificationCompat
import com.armpipe.MainActivity
import com.armpipe.R
import kotlinx.coroutines.*
import kotlinx.coroutines.flow.MutableStateFlow

/** Shared snapshot of what the pipeline is doing, so the notification and the
 *  UI never disagree. */
object JobTracker {
    data class Snapshot(
        val active: Boolean = false,
        val label: String = "Working",
        val progress: Float? = null,
    )

    val state = MutableStateFlow(Snapshot())

    fun update(label: String, progress: Float?, active: Boolean) {
        state.value = Snapshot(active, label, progress)
    }
}

/**
 * A stage can render for half an hour. Android will happily freeze a background
 * app and kill its coroutines, so while anything is running we hold a
 * foreground notification with a live progress bar.
 */
class JobService : Service() {

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main)

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        createChannel()
        startForeground(NOTIFICATION_ID, build(JobTracker.state.value))
        scope.launch {
            JobTracker.state.collect { snap ->
                notificationManager().notify(NOTIFICATION_ID, build(snap))
            }
        }
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int) = START_STICKY

    override fun onDestroy() {
        scope.cancel()
        super.onDestroy()
    }

    private fun notificationManager() =
        getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager

    private fun build(snap: JobTracker.Snapshot): Notification {
        val tap = PendingIntent.getActivity(
            this, 0, Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT
        )
        val b = NotificationCompat.Builder(this, CHANNEL)
            .setContentTitle("Arm Pipeline")
            .setContentText(snap.label)
            .setSmallIcon(R.drawable.ic_stat_pipeline)
            .setOngoing(true)
            .setOnlyAlertOnce(true)
            .setContentIntent(tap)
        val p = snap.progress
        if (p != null) b.setProgress(100, (p * 100).toInt(), false)
        else b.setProgress(0, 0, true)
        return b.build()
    }

    private fun createChannel() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            notificationManager().createNotificationChannel(
                NotificationChannel(CHANNEL, "Pipeline jobs",
                    NotificationManager.IMPORTANCE_LOW)
            )
        }
    }

    companion object {
        private const val CHANNEL = "armpipe_jobs"
        private const val NOTIFICATION_ID = 42

        fun start(ctx: Context) {
            val i = Intent(ctx, JobService::class.java)
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O)
                ctx.startForegroundService(i) else ctx.startService(i)
        }

        fun stop(ctx: Context) {
            JobTracker.update("Idle", null, false)
            ctx.stopService(Intent(ctx, JobService::class.java))
        }
    }
}

/** Hands the final MP4 to Android's own download manager, so it lands in the
 *  phone's Downloads folder and appears in the notification shade. */
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
