package com.armpipe.ui

import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.material3.*
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.sp

// Same palette as the web control panel: table-pad black, chalk, elbow-pad red,
// referee blue for work in progress, brass for work that landed.
val Pad = Color(0xFF171310)
val Pad2 = Color(0xFF211A15)
val Pad3 = Color(0xFF2C231C)
val Chalk = Color(0xFFF3EFE7)
val Tape = Color(0xFFA2968A)
val Vinyl = Color(0xFFDC3B26)
val Ref = Color(0xFF5A8FC7)
val Pin = Color(0xFFC8B273)
val Line = Color(0xFF3A2F26)

private val scheme = darkColorScheme(
    primary = Vinyl,
    onPrimary = Color.White,
    secondary = Ref,
    onSecondary = Color.White,
    tertiary = Pin,
    background = Pad,
    onBackground = Chalk,
    surface = Pad2,
    onSurface = Chalk,
    surfaceVariant = Pad3,
    onSurfaceVariant = Tape,
    outline = Line,
    error = Vinyl,
)

private val typography = Typography(
    displaySmall = TextStyle(fontSize = 30.sp, fontWeight = FontWeight.Black,
        letterSpacing = 0.4.sp),
    headlineSmall = TextStyle(fontSize = 21.sp, fontWeight = FontWeight.Bold),
    titleMedium = TextStyle(fontSize = 16.sp, fontWeight = FontWeight.SemiBold),
    bodyMedium = TextStyle(fontSize = 15.sp, lineHeight = 21.sp),
    bodySmall = TextStyle(fontSize = 13.sp, lineHeight = 18.sp),
    labelSmall = TextStyle(fontSize = 11.sp, fontWeight = FontWeight.Medium,
        letterSpacing = 1.4.sp),
)

@Composable
fun ArmPipelineTheme(
    @Suppress("UNUSED_PARAMETER") dark: Boolean = isSystemInDarkTheme(),
    content: @Composable () -> Unit,
) = MaterialTheme(colorScheme = scheme, typography = typography, content = content)
