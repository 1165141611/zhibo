package com.example.liveremote.ui.components

import androidx.compose.foundation.Canvas
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.State
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberUpdatedState
import androidx.compose.runtime.snapshotFlow
import androidx.compose.runtime.withFrameMillis
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.CornerRadius
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp
import com.example.liveremote.ui.theme.C
import kotlin.math.abs

// 服务端进度与本地时钟偏差超过此值 → 硬重锚(seek/换歌/严重失步才会这么大)
private const val HARD_SNAP_MS = 350
// 小偏差的消化窗口:目标在 ~1s 内追平(下一拍回推前基本收敛)
private const val SLEW_WINDOW_MS = 1000.0
// 追赶/放慢的最大变速幅度(±10%),肉眼不可察,保证播放头永不倒退、永不跳变
private const val MAX_RATE_WARP = 0.10

/**
 * 本地插值播放头(毫秒)。电脑播放器是时钟源、每 ~500ms 经 WS 推一次真实进度;
 * 这里用**帧时钟**在推送之间平滑推进,使歌词/音准线/进度条 60fps 顺滑,同时**几乎零开销**。
 *
 * 关键一:返回的 [State] 只应在**绘制作用域**(`Canvas` 的 onDraw、`drawBehind`)或 `derivedStateOf`
 * 里读取。这样每帧只让绘制层失效,**不会**触发整棵 Compose UI 树重组——这正是老版
 * `st.copy(posMs=...)` 每 50ms 重建全局状态导致卡顿的根因,现已避开。
 *
 * 关键二:**只在关键节点硬校正,平时变速微调(clock slewing)**。服务端回推的进度带着
 * 管道+WiFi 传输延迟和音频回调量化抖动,天然比本地时钟"旧且抖"。若每拍回推都硬重锚
 * (老版 `LaunchedEffect(posMs)`),播放头每 500ms 向后跳几十毫秒再前进,歌词高亮一卡一卡。
 * 现改为:播放开始/暂停/seek/换歌(偏差 > [HARD_SNAP_MS])才硬跳;平时把偏差折算成 ±10% 内的
 * 速率微调,在约 1s 内悄悄追平——播放头单调平滑前进,又不会与电脑端漂移(同 NTP slew /
 * 播放器时钟驯化的思路)。
 */
@Composable
fun rememberPlayhead(posMs: Int, playing: Boolean, durMs: Int): State<Int> {
    val out = remember { mutableIntStateOf(posMs) }
    val serverPos = rememberUpdatedState(posMs)   // 最新回推进度,帧循环内读取,不重启 effect
    LaunchedEffect(playing, durMs) {
        if (!playing) {
            // 暂停:直接跟随服务端进度(覆盖暂停中 seek / 换歌归零)
            snapshotFlow { serverPos.value }.collect { out.intValue = it.coerceAtLeast(0) }
        } else {
            var local = serverPos.value.toDouble()   // 本地时钟:开播这一刻硬校正一次
            var lastFrame = -1L
            var lastServer = serverPos.value
            var rate = 1.0
            while (true) {
                withFrameMillis { frame ->
                    if (lastFrame >= 0L) local += (frame - lastFrame) * rate
                    lastFrame = frame
                    val sp = serverPos.value
                    if (sp != lastServer) {          // 新一拍服务端进度:算偏差,不直接采用
                        lastServer = sp
                        val drift = sp - local
                        if (abs(drift) > HARD_SNAP_MS) {
                            local = sp.toDouble()    // seek/换歌/严重失步 → 硬重锚
                            rate = 1.0
                        } else {
                            rate = (1.0 + drift / SLEW_WINDOW_MS)
                                .coerceIn(1.0 - MAX_RATE_WARP, 1.0 + MAX_RATE_WARP)
                        }
                    }
                    val p = local.toInt()
                    out.intValue = if (durMs > 0) p.coerceIn(0, durMs) else p.coerceAtLeast(0)
                }
            }
        }
    }
    return out
}

/**
 * 帧驱动的平滑进度条。[progress] 在**绘制作用域**内被调用(通常读 [rememberPlayhead] 的 State),
 * 因此进度随帧平滑推进却只重绘自身、不引发重组。
 */
@Composable
fun SmoothProgressBar(
    progress: () -> Float,
    modifier: Modifier = Modifier,
    track: Color = C.Stroke,
    bar: Color = C.Accent,
    h: Dp = 4.dp,
) {
    Canvas(modifier.fillMaxWidth().height(h)) {
        val f = progress().coerceIn(0f, 1f)
        val r = CornerRadius(size.height / 2f, size.height / 2f)
        drawRoundRect(color = track, cornerRadius = r)
        if (f > 0f) {
            drawRoundRect(
                color = bar,
                topLeft = Offset(0f, 0f),
                size = Size(size.width * f, size.height),
                cornerRadius = r,
            )
        }
    }
}
