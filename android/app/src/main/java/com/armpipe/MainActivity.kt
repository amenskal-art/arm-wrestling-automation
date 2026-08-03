@file:OptIn(androidx.compose.material3.ExperimentalMaterial3Api::class)

package com.armpipe

import android.Manifest
import android.content.Intent
import android.os.Build
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.activity.viewModels
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.core.view.WindowCompat
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.armpipe.ui.*
import kotlinx.coroutines.launch

enum class Dest(val label: String, val icon: ImageVector, val caption: String) {
    PIPELINE("Pipeline", Icons.Filled.PlayCircleOutline, "Run the four stages"),
    KNOWLEDGE("Knowledge", Icons.Filled.Notes, "What the script is written from"),
    SOURCES("Sources", Icons.Filled.Link, "Video links and the voice clip"),
    MODELS("Models", Icons.Filled.Tune, "Which AI does which job"),
    FILES("Files", Icons.Filled.Download, "Download the finished video"),
    DEPLOY("Deploy", Icons.Filled.CloudUpload, "Push the backend to Modal"),
    CONNECTION("Connection", Icons.Filled.Key, "Backend address and sign-in"),
}

class MainActivity : ComponentActivity() {

    private val vm: PipelineViewModel by viewModels()

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        WindowCompat.setDecorFitsSystemWindows(window, false)
        handleAuthLink(intent)

        setContent {
            ArmPipelineTheme {
                val ui by vm.ui.collectAsStateWithLifecycle()
                val drawer = rememberDrawerState(DrawerValue.Closed)
                val scope = rememberCoroutineScope()
                val snackbar = remember { SnackbarHostState() }
                var dest by rememberSaveable {
                    mutableStateOf(if (ui.connected) Dest.PIPELINE else Dest.CONNECTION)
                }

                // Ask for the notification permission once, so long renders can
                // report progress while the app is in the background.
                val perm = rememberLauncherForActivityResult(
                    ActivityResultContracts.RequestPermission()
                ) { }
                LaunchedEffect(Unit) {
                    if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU)
                        perm.launch(Manifest.permission.POST_NOTIFICATIONS)
                }

                LaunchedEffect(ui.message) {
                    ui.message?.let { snackbar.showSnackbar(it); vm.dismissMessage() }
                }
                LaunchedEffect(ui.connected) {
                    if (ui.connected && dest == Dest.CONNECTION) dest = Dest.PIPELINE
                }

                ModalNavigationDrawer(
                    drawerState = drawer,
                    drawerContent = {
                        Sidebar(current = dest, connected = ui.connected) {
                            dest = it
                            scope.launch { drawer.close() }
                        }
                    }
                ) {
                    Scaffold(
                        containerColor = Pad,
                        snackbarHost = { SnackbarHost(snackbar) },
                        topBar = {
                            TopAppBar(
                                title = {
                                    Column {
                                        Text(dest.label, fontWeight = FontWeight.Black)
                                        Text(
                                            dest.caption,
                                            style = MaterialTheme.typography.bodySmall,
                                            color = Tape
                                        )
                                    }
                                },
                                navigationIcon = {
                                    IconButton(onClick = { scope.launch { drawer.open() } }) {
                                        Icon(Icons.Filled.Menu, "Open the menu")
                                    }
                                },
                                actions = {
                                    if (ui.loading) CircularProgressIndicator(
                                        Modifier.padding(end = 16.dp).size(20.dp),
                                        strokeWidth = 2.dp, color = Ref
                                    ) else IconButton(onClick = { vm.refresh() }) {
                                        Icon(Icons.Filled.Refresh, "Reload from the server")
                                    }
                                },
                                colors = TopAppBarDefaults.topAppBarColors(
                                    containerColor = Pad, titleContentColor = Chalk
                                )
                            )
                        },
                        bottomBar = {
                            if (dest == Dest.PIPELINE && ui.connected)
                                RunEverythingBar(ui, vm)
                        }
                    ) { pad ->
                        Column(
                            Modifier.padding(pad).fillMaxSize()
                                .verticalScroll(rememberScrollState())
                        ) {
                            when (dest) {
                                Dest.PIPELINE -> PipelineScreen(ui, vm)
                                Dest.KNOWLEDGE -> KnowledgeScreen(ui, vm)
                                Dest.SOURCES -> SourcesScreen(ui, vm)
                                Dest.MODELS -> ModelsScreen(ui, vm)
                                Dest.FILES -> FilesScreen(ui, vm)
                                Dest.DEPLOY -> DeployScreen(ui, vm)
                                Dest.CONNECTION -> ConnectionScreen(ui, vm)
                            }
                        }
                    }
                }
            }
        }
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        handleAuthLink(intent)
    }

    /** armpipe://auth?token=… — the browser handing the session back to the app. */
    private fun handleAuthLink(intent: Intent?) {
        val data = intent?.data ?: return
        if (data.scheme == "armpipe" && data.host == "auth") {
            data.getQueryParameter("token")?.takeIf { it.isNotBlank() }
                ?.let { vm.acceptBrowserToken(it) }
        }
    }
}

@Composable
private fun Sidebar(current: Dest, connected: Boolean, onPick: (Dest) -> Unit) {
    ModalDrawerSheet(drawerContainerColor = Pad2) {
        Column(Modifier.padding(start = 22.dp, top = 34.dp, bottom = 14.dp)) {
            Text("ARM", style = MaterialTheme.typography.displaySmall, color = Chalk)
            Text("PIPELINE", style = MaterialTheme.typography.displaySmall, color = Vinyl)
            Spacer(Modifier.height(6.dp))
            Row(verticalAlignment = Alignment.CenterVertically) {
                Icon(
                    if (connected) Icons.Filled.CloudDone else Icons.Filled.CloudOff,
                    null, tint = if (connected) Pin else Tape,
                    modifier = Modifier.size(15.dp)
                )
                Spacer(Modifier.width(7.dp))
                Text(
                    if (connected) "Backend connected" else "Not connected",
                    style = MaterialTheme.typography.bodySmall, color = Tape
                )
            }
        }
        HorizontalDivider(color = Line)
        Spacer(Modifier.height(8.dp))
        Dest.entries.forEach { d ->
            NavigationDrawerItem(
                icon = { Icon(d.icon, null) },
                label = { Text(d.label, fontSize = 15.sp) },
                selected = d == current,
                onClick = { onPick(d) },
                colors = NavigationDrawerItemDefaults.colors(
                    selectedContainerColor = Pad3,
                    selectedTextColor = Chalk,
                    selectedIconColor = Vinyl,
                    unselectedTextColor = Tape,
                    unselectedIconColor = Tape,
                ),
                modifier = Modifier.padding(horizontal = 12.dp, vertical = 2.dp)
            )
        }
        Spacer(Modifier.weight(1f))
        Text(
            "Every AI call runs on Modal. Nothing is processed on this phone.",
            style = MaterialTheme.typography.bodySmall, color = Tape,
            modifier = Modifier.padding(22.dp)
        )
    }
}
