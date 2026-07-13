package com.example.liveremote.ui.theme

import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Typography
import androidx.compose.material3.darkColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.unit.sp

/** 全局深色主题(始终深色,主播暗光环境)。字体走系统无衬线(中文机型即思源黑体)。 */
private val Scheme = darkColorScheme(
    primary = C.Accent,
    onPrimary = C.OnAccent,
    background = C.Bg,
    onBackground = C.Text,
    surface = C.Card,
    onSurface = C.Text,
    error = C.Danger,
)

private val Typo = Typography().run {
    val f = FontFamily.SansSerif
    copy(
        displaySmall = displaySmall.copy(fontFamily = f),
        headlineMedium = headlineMedium.copy(fontFamily = f),
        titleLarge = titleLarge.copy(fontFamily = f),
        bodyLarge = bodyLarge.copy(fontFamily = f),
        bodyMedium = bodyMedium.copy(fontFamily = f),
        labelLarge = labelLarge.copy(fontFamily = f),
    )
}

@Composable
fun LiveRemoteTheme(content: @Composable () -> Unit) {
    // 始终深色(忽略系统),主播暗光环境。
    MaterialTheme(colorScheme = Scheme, typography = Typo, content = content)
}

/** 便捷字号常量(sp)。 */
object F {
    val h1 = 22.sp
    val songBig = 20.sp
    val lyricBig = 30.sp
    val lyricNext = 17.sp
    val cardTitle = 17.sp
    val rowTitle = 16.sp
    val body = 15.sp
    val sub = 13.sp
    val pill = 12.sp
    val tiny = 11.sp
    val micro = 10.sp
}

val TabTextStyle = TextStyle(fontSize = F.tiny)
