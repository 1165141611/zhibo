package com.example.liveremote.ui.components

import androidx.compose.foundation.Canvas
import androidx.compose.foundation.layout.size
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.StrokeCap
import androidx.compose.ui.graphics.StrokeJoin
import androidx.compose.ui.graphics.drawscope.DrawScope
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.graphics.drawscope.withTransform
import androidx.compose.ui.graphics.vector.PathParser
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp

/**
 * 24×24 viewBox 的 SVG 图标(路径 d 串直接取自原型,像素忠实)。
 * [fills] 填充路径、[strokes] 描边路径;strokeWidth 走 viewBox 单位(随图标缩放)。
 */
@Composable
fun Glyph(
    tint: Color,
    modifier: Modifier = Modifier,
    size: Dp = 24.dp,
    fills: List<String> = emptyList(),
    strokes: List<String> = emptyList(),
    strokeWidth: Float = 2f,
) {
    Canvas(modifier.size(size)) {
        val s = this.size.minDimension / 24f
        withTransform({ scale(s, s, pivot = Offset.Zero) }) {
            fills.forEach { drawPath(PathParser().parsePathString(it).toPath(), tint) }
            val st = Stroke(width = strokeWidth, cap = StrokeCap.Round, join = StrokeJoin.Round)
            strokes.forEach { drawPath(PathParser().parsePathString(it).toPath(), tint, style = st) }
        }
    }
}

// ——— 填充型控制图标 ———
@Composable fun PlayIcon(tint: Color, size: Dp) = Glyph(tint, size = size, fills = listOf("M8 5v14l11-7z"))
@Composable fun PauseIcon(tint: Color, size: Dp) = Glyph(tint, size = size, fills = listOf("M7 5h4v14H7zM13 5h4v14h-4z"))
@Composable fun SeekBackIcon(tint: Color, size: Dp) = Glyph(tint, size = size, fills = listOf("M11 5v14l-8-7z", "M20 5v14l-8-7z"))
@Composable fun SeekFwdIcon(tint: Color, size: Dp) = Glyph(tint, size = size, fills = listOf("M13 5v14l8-7z", "M4 5v14l8-7z"))
@Composable fun SkipNextIcon(tint: Color, size: Dp) = Glyph(tint, size = size, fills = listOf("M6 5v14l9-7z", "M15.5 5h2.4v14h-2.4z"))
@Composable fun BgmPrevIcon(tint: Color, size: Dp) = Glyph(tint, size = size, fills = listOf("M18 5v14l-9-7z", "M6.1 5H8.5v14H6.1z"))

// ——— 描边型图标 ———
@Composable fun PlusIcon(tint: Color, size: Dp, sw: Float = 2.4f) = Glyph(tint, size = size, strokes = listOf("M12 6v12M6 12h12"), strokeWidth = sw)
@Composable fun CloseIcon(tint: Color, size: Dp, sw: Float = 2.2f) = Glyph(tint, size = size, strokes = listOf("M6 6l12 12M18 6L6 18"), strokeWidth = sw)
@Composable fun CheckIcon(tint: Color, size: Dp, sw: Float = 2.6f) = Glyph(tint, size = size, strokes = listOf("M5 12l4.5 4.5L19 7"), strokeWidth = sw)
@Composable fun ArrowUpIcon(tint: Color, size: Dp, sw: Float = 2f) = Glyph(tint, size = size, strokes = listOf("M12 19V6M6 12l6-6 6 6"), strokeWidth = sw)
@Composable fun BackIcon(tint: Color, size: Dp, sw: Float = 2.1f) = Glyph(tint, size = size, strokes = listOf("M15 5l-7 7 7 7"), strokeWidth = sw)

/** 音量喇叭(实体箱体 + 描边声波)。 */
@Composable
fun VolumeIcon(tint: Color, size: Dp) = Glyph(
    tint, size = size,
    fills = listOf("M11 5L6 9H3v6h3l5 4z"),
    strokes = listOf("M16 9a4 4 0 0 1 0 6"),
    strokeWidth = 1.8f,
)

// ——— 含圆的图标:直接画 ———
@Composable
fun SearchIcon(tint: Color, size: Dp) {
    Canvas(Modifier.size(size)) {
        val s = this.size.minDimension / 24f
        val st = Stroke(width = 1.9f * s, cap = StrokeCap.Round)
        drawCircle(tint, radius = 6.5f * s, center = Offset(11f * s, 11f * s), style = st)
        drawLine(tint, Offset(15.6f * s, 15.6f * s), Offset(20f * s, 20f * s), strokeWidth = 1.9f * s, cap = StrokeCap.Round)
    }
}

@Composable
fun MicIcon(tint: Color, size: Dp) = Glyph(
    tint, size = size,
    strokes = listOf(
        "M9 3.5 a3 3 0 0 1 6 0 v5 a3 3 0 0 1 -6 0 z",  // 话筒头(圆角胶囊)
        "M6 8a6 6 0 0 0 12 0",                          // 支架弧
        "M12 14v4M8 18h8",                              // 杆 + 底座
    ),
    strokeWidth = 1.7f,
)

@Composable
fun MusicNoteIcon(tint: Color, size: Dp) {
    Canvas(Modifier.size(size)) {
        val s = this.size.minDimension / 24f
        val st = Stroke(width = 1.8f * s, cap = StrokeCap.Round, join = StrokeJoin.Round)
        // 双八分音符:两根符干 + 顶部符梁(描边),两个符头(实心,否则看着像两个空心 o)
        drawPath(PathParser().parsePathString("M9 18V5l10-2v13").toPath(), tint, style = st)
        drawCircle(tint, radius = 2.7f * s, center = Offset(6f * s, 18f * s))
        drawCircle(tint, radius = 2.7f * s, center = Offset(16f * s, 16f * s))
    }
}

/** 队列行拖动手柄:6 个小点。 */
@Composable
fun DragHandleIcon(tint: Color, size: Dp) {
    Canvas(Modifier.size(size)) {
        val s = this.size.minDimension / 24f
        val r = 1.05f * s
        for (x in listOf(8f, 15f)) for (y in listOf(8f, 12f, 16f))
            drawCircle(tint, radius = r, center = Offset(x * s, y * s))
    }
}

// ——— TabBar 三图标(描边为主,近似原型) ———
@Composable
fun TabSingIcon(tint: Color, size: Dp) = Glyph(
    tint, size = size,
    fills = listOf("M9 2.5 a3 3 0 0 1 6 0 v5 a3 3 0 0 1 -6 0 z"),
    strokes = listOf("M5.5 11a6.5 6.5 0 0 0 13 0", "M12 17.5V21M8.5 21h7"),
    strokeWidth = 1.9f,
)

@Composable
fun TabQueueIcon(tint: Color, size: Dp) = Glyph(
    tint, size = size,
    fills = listOf("M16 13.2v6.3l5-3.15z"),
    strokes = listOf("M4 6h11M4 11h11M4 16h6"),
    strokeWidth = 1.9f,
)

@Composable
fun TabRemoteIcon(tint: Color, size: Dp) {
    Canvas(Modifier.size(size)) {
        val s = this.size.minDimension / 24f
        val st = Stroke(width = 1.9f * s, cap = StrokeCap.Round, join = StrokeJoin.Round)
        drawRoundRectPath(this, 6.5f * s, 2.5f * s, 11f * s, 19f * s, 4f * s, tint, st)
        drawCircle(tint, radius = 1.6f * s, center = Offset(12f * s, 7f * s))
        drawLine(tint, Offset(9.5f * s, 12f * s), Offset(14.5f * s, 12f * s), 1.9f * s, StrokeCap.Round)
        drawLine(tint, Offset(9.5f * s, 15.5f * s), Offset(14.5f * s, 15.5f * s), 1.9f * s, StrokeCap.Round)
    }
}

private fun drawRoundRectPath(
    ds: DrawScope, x: Float, y: Float, w: Float, h: Float, r: Float, tint: Color, st: Stroke,
) {
    ds.drawRoundRect(
        color = tint, topLeft = Offset(x, y), size = Size(w, h),
        cornerRadius = androidx.compose.ui.geometry.CornerRadius(r, r), style = st,
    )
}
