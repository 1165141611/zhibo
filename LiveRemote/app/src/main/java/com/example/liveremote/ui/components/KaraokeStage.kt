package com.example.liveremote.ui.components

import androidx.compose.foundation.Canvas
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.State
import androidx.compose.runtime.derivedStateOf
import androidx.compose.runtime.getValue
import androidx.compose.runtime.remember
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.geometry.CornerRadius
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.buildAnnotatedString
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.withStyle
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.example.liveremote.model.SongLyrics
import com.example.liveremote.ui.theme.C
import com.example.liveremote.ui.theme.F
import kotlin.math.ceil

private const val GAP_FOR_DOTS = 8000     // 间隔≥8s 才显示引导圆点(与 PC 端一致)
private const val LEAD = 3500             // 提前全亮,最后 3.5s 才逐个熄灭
// 音准线几何:全部用 dp(在 DrawScope 里 .dp.toPx()),否则在高 DPI 手机上按设备像素算会又小又挤,
// 音符成一堆散点。DP_PER_SEC=60 → 每秒 60dp,时间密度与电脑端接近(约一屏 5~6 秒),不再散乱。
private const val DP_PER_SEC = 60f        // 横向:每秒像素(dp)
private const val HEAD_RATIO = 0.30f      // 播放头在音准区宽度的 30%(仿 PC,留出已唱历史)
private const val NOTE_H_DP = 10f         // 音符块高(dp)

/**
 * 演唱舞台:音准线区(172dp)+ 逐字歌词区。
 *
 * [playhead] 为帧驱动的本地插值播放头(见 [rememberPlayhead]):音准线在 Canvas 绘制作用域内读取,
 * 每帧只重绘;歌词经 `derivedStateOf` 仅在"当前行/已唱字数/圆点数"变化时重组。全程不引发整树重组。
 *
 * [lyrics] 为 null 时显示占位(后端尚未推送逐字/音高数据)。
 */
@Composable
fun KaraokeStage(
    playhead: State<Int>,
    title: String,
    artist: String,
    lyrics: SongLyrics?,
    modifier: Modifier = Modifier,
) {
    Column(modifier) {
        PitchArea(playhead, title, artist, lyrics)
        Box(Modifier.fillMaxWidth().weight(1f), contentAlignment = Alignment.Center) {
            if (lyrics == null) LyricPlaceholder(title)
            else LyricArea(playhead, lyrics)
        }
    }
}

@Composable
private fun PitchArea(playhead: State<Int>, title: String, artist: String, lyrics: SongLyrics?) {
    val notes = lyrics?.notes ?: emptyList()
    Box(
        Modifier
            .fillMaxWidth()
            .height(172.dp)
            .background(Brush.verticalGradient(listOf(C.StageTop, C.StageBottom)))
    ) {
        Canvas(Modifier.fillMaxSize()) {
            val posMs = playhead.value            // 绘制作用域内读取 → 每帧只重绘本 Canvas
            val w = size.width
            val h = size.height
            val headX = w * HEAD_RATIO
            val pxPerMs = DP_PER_SEC.dp.toPx() / 1000f
            val noteH = NOTE_H_DP.dp.toPx()
            val padV = 16.dp.toPx()
            val usableH = (h - noteH - padV * 2).coerceAtLeast(1f)
            val r = CornerRadius(noteH / 2f, noteH / 2f)
            // 两条参考线
            drawLine(Color.White.copy(alpha = 0.04f), Offset(0f, h * 0.33f), Offset(w, h * 0.33f))
            drawLine(Color.White.copy(alpha = 0.04f), Offset(0f, h * 0.66f), Offset(w, h * 0.66f))
            // 音符块(横轴=时间随 dp/秒 滚动,纵轴=归一化音高;高=顶部)
            for (n in notes) {
                val x = headX + (n.startMs - posMs) * pxPerMs
                val bw = (n.durMs * pxPerMs).coerceAtLeast(3.dp.toPx())
                if (x + bw < 0f || x > w) continue
                val active = posMs >= n.startMs && posMs < n.startMs + n.durMs
                // 归一化音高压进 [0.12,0.88],给上下留白(仿 PC 的 ±2 半音余量),不贴边、趋势更柔和
                val pv = 0.12f + n.pitch * 0.76f
                val top = padV + (1f - pv) * usableH
                drawRoundRect(
                    color = if (active) C.Accent else C.NoteIdle,
                    topLeft = Offset(x, top), size = Size(bw, noteH), cornerRadius = r,
                )
                if (active) {
                    val fill = ((posMs - n.startMs).toFloat() / n.durMs).coerceIn(0f, 1f)
                    drawRoundRect(
                        color = Color.White.copy(alpha = 0.85f),
                        topLeft = Offset(x, top), size = Size(bw * fill, noteH), cornerRadius = r,
                    )
                }
            }
            // 播放头竖线 + 圆点
            drawLine(C.Accent, Offset(headX, 0f), Offset(headX, h), strokeWidth = 2.dp.toPx())
            drawCircle(C.Accent, radius = 4.dp.toPx(), center = Offset(headX, 10.dp.toPx()))
        }
        // 前奏/无音符:居中歌名。nearNote 经 derivedStateOf 计算,仅切换时重组一次(非每帧)。
        val nearNote by remember(lyrics) {
            derivedStateOf {
                val posMs = playhead.value
                notes.any { posMs >= it.startMs - 200 && posMs < it.startMs + it.durMs }
            }
        }
        if (!nearNote && title.isNotBlank()) {
            Text(
                "♪ $title - $artist",
                color = C.TextFaint, fontSize = F.sub,
                modifier = Modifier.align(Alignment.BottomCenter).padding(bottom = 12.dp),
            )
        }
    }
}

@Composable
private fun LyricPlaceholder(title: String) {
    Column(horizontalAlignment = Alignment.CenterHorizontally, verticalArrangement = Arrangement.spacedBy(10.dp)) {
        Text(
            if (title.isBlank()) "🎤 卡拉OK字幕" else "🎤 $title",
            color = C.Text, fontSize = 22.sp, fontWeight = FontWeight.Bold,
        )
        Text("逐字歌词 / 音准线待电脑端推送", color = C.TextDim, fontSize = F.sub)
    }
}

/** 从播放头派生出的离散渲染量:仅当这些值变化时歌词区才重组(每秒寥寥数次)。 */
private data class LyricSnap(
    val curIdx: Int,
    val showDots: Boolean,
    val lit: Int,
    val sungChars: Int,
)

@Composable
private fun LyricArea(playhead: State<Int>, lyrics: SongLyrics) {
    val lines = lyrics.lines
    if (lines.isEmpty()) { LyricPlaceholder(""); return }

    val snap by remember(lyrics) {
        derivedStateOf {
            val posMs = playhead.value
            var curIdx = lines.indexOfFirst { posMs < it.endMs }
            if (curIdx < 0) curIdx = lines.lastIndex
            val cur = lines[curIdx]
            val prevEnd = if (curIdx == 0) 0 else lines[curIdx - 1].endMs
            val gap = cur.startMs - prevEnd
            val inPre = posMs < cur.startMs
            val showDots = inPre && gap >= GAP_FOR_DOTS
            val remain = cur.startMs - posMs
            val lit = when {
                !showDots -> 0
                remain >= LEAD -> 4
                else -> ceil(remain.toFloat() / LEAD * 4).toInt().coerceIn(0, 4)
            }
            val sungChars = if (inPre) 0 else cur.chars.count { posMs >= it.startMs }
            LyricSnap(curIdx, showDots, lit, sungChars)
        }
    }

    val cur = lines[snap.curIdx]
    val next = lines.getOrNull(snap.curIdx + 1)

    Column(
        horizontalAlignment = Alignment.CenterHorizontally,
        verticalArrangement = Arrangement.spacedBy(16.dp),
        modifier = Modifier.fillMaxWidth().padding(horizontal = 20.dp),
    ) {
        if (snap.showDots) {
            Row(horizontalArrangement = Arrangement.spacedBy(12.dp), verticalAlignment = Alignment.CenterVertically) {
                repeat(4) { i ->
                    Box(Modifier.size(11.dp).clip(CircleShape).background(if (i < snap.lit) C.Accent else C.Stroke2))
                }
            }
        }
        // 当前行逐字(整字高亮,已唱=青色)
        Text(
            buildAnnotatedString {
                cur.chars.forEachIndexed { idx, ch ->
                    withStyle(androidx.compose.ui.text.SpanStyle(color = if (idx < snap.sungChars) C.Accent else C.LyricWhite)) {
                        append(ch.text)
                    }
                }
                if (cur.chars.isEmpty()) append(cur.text)
            },
            style = TextStyle(fontSize = F.lyricBig, fontWeight = FontWeight.Bold),
            textAlign = androidx.compose.ui.text.style.TextAlign.Center,
        )
        // 下一行
        Text(next?.text ?: "", color = C.TextDim, fontSize = F.lyricNext,
            textAlign = androidx.compose.ui.text.style.TextAlign.Center)
    }
}
