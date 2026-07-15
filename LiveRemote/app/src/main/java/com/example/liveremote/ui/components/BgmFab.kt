package com.example.liveremote.ui.components

import androidx.compose.animation.core.RepeatMode
import androidx.compose.animation.core.animateFloat
import androidx.compose.animation.core.infiniteRepeatable
import androidx.compose.animation.core.rememberInfiniteTransition
import androidx.compose.animation.core.tween
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.offset
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Slider
import androidx.compose.material3.SliderDefaults
import androidx.compose.material3.Switch
import androidx.compose.material3.SwitchDefaults
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.draw.scale
import androidx.compose.ui.graphics.Color
import android.content.Context
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.IntOffset
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.example.liveremote.model.AppState
import com.example.liveremote.ui.theme.C
import com.example.liveremote.ui.theme.F
import androidx.compose.foundation.gestures.detectDragGestures
import androidx.compose.foundation.gestures.detectTapGestures
import kotlin.math.roundToInt

/**
 * 悬浮 QQ音乐 迷你控制台(除遥控页外常驻)。可拖动 + 点击展开毛玻璃面板 + 失焦收起。
 * 位置持久化到 SharedPreferences("cfg" 的 fab_rx/fab_ry,归一化比例):启动时在首次组合的
 * remember 初始化块里**同步读回**(先读后渲染,首帧即在上次位置,无跳动),拖动结束时写盘。
 */
@Composable
fun BgmFabOverlay(
    st: AppState,
    autoFollow: Boolean,
    onPrev: () -> Unit,
    onToggle: () -> Unit,
    onNext: () -> Unit,
    onVol: (Int) -> Unit,
    onAutoFollow: (Boolean) -> Unit,
) {
    androidx.compose.foundation.layout.BoxWithConstraints(Modifier.fillMaxSize()) {
        val density = LocalDensity.current
        val maxWpx = with(density) { maxWidth.toPx() }
        val maxHpx = with(density) { maxHeight.toPx() }
        val ballPx = with(density) { 52.dp.toPx() }
        val maxX = (maxWpx - ballPx).coerceAtLeast(0f)
        val maxY = (maxHpx - ballPx).coerceAtLeast(0f)
        // 位置持久化:存**归一化比例**(适配不同屏幕/旋转),拖动结束写入。
        // 关键:remember 初始化块在**首次组合时同步读** SharedPreferences("cfg" 已被 ViewModel 加载,
        // 内存缓存命中、无磁盘等待)→ 首帧渲染前位置已就绪,启动即在上次的位置,不会先渲染默认位再跳。
        val ctx = LocalContext.current
        val prefs = remember(ctx) { ctx.getSharedPreferences("cfg", Context.MODE_PRIVATE) }
        var pos by remember(maxWpx, maxHpx) {
            val rx = prefs.getFloat("fab_rx", -1f)
            val ry = prefs.getFloat("fab_ry", -1f)
            mutableStateOf(
                if (rx in 0f..1f && ry in 0f..1f) Offset0(rx * maxX, ry * maxY)
                else Offset0(   // 无缓存:默认右侧、声卡控制条上方
                    maxWpx - ballPx - with(density) { 16.dp.toPx() },
                    maxHpx - ballPx - with(density) { 150.dp.toPx() },
                )
            )
        }
        fun savePos() {
            prefs.edit()
                .putFloat("fab_rx", if (maxX > 0f) (pos.x / maxX).coerceIn(0f, 1f) else 0f)
                .putFloat("fab_ry", if (maxY > 0f) (pos.y / maxY).coerceIn(0f, 1f) else 0f)
                .apply()
        }
        var open by remember { mutableStateOf(false) }

        if (open) {
            // 蒙层:点击外部收起
            Box(Modifier.fillMaxSize().pointerInput(Unit) { detectTapGestures { open = false } })
            // 面板跟随悬浮球位置弹出:水平中心对齐球心,竖直优先放球上方(不够则下方),最后夹进屏内
            val panelWpx = with(density) { 260.dp.toPx() }
            val panelHpx = with(density) { 222.dp.toPx() }   // 估计高度(含联动开关行),仅用于夹取与上下方向判断
            val gap = with(density) { 10.dp.toPx() }
            val margin = with(density) { 8.dp.toPx() }
            var px = pos.x + ballPx / 2f - panelWpx / 2f
            var py = pos.y - panelHpx - gap
            if (py < margin) py = pos.y + ballPx + gap
            px = px.coerceIn(margin, (maxWpx - panelWpx - margin).coerceAtLeast(margin))
            py = py.coerceIn(margin, (maxHpx - panelHpx - margin).coerceAtLeast(margin))
            BgmPanel(
                st, autoFollow, onPrev, onToggle, onNext, onVol, onAutoFollow,
                onClose = { open = false },
                modifier = Modifier.offset { IntOffset(px.roundToInt(), py.roundToInt()) },
            )
        } else {
            BgmBall(
                playing = st.bgmPlaying,
                autoFollow = autoFollow,
                modifier = Modifier
                    .offset { IntOffset(pos.x.roundToInt(), pos.y.roundToInt()) }
                    .pointerInput(maxX, maxY) {
                        detectDragGestures(
                            onDragEnd = { savePos() },      // 拖完落盘,下次启动原位出现
                            onDragCancel = { savePos() },
                        ) { _, drag ->
                            val nx = (pos.x + drag.x).coerceIn(0f, maxX)
                            val ny = (pos.y + drag.y).coerceIn(0f, maxY)
                            pos = Offset0(nx, ny)
                        }
                    }
                    .pointerInput(Unit) { detectTapGestures { open = true } },
            )
        }
    }
}

private data class Offset0(val x: Float, val y: Float)

@Composable
private fun BgmBall(playing: Boolean, autoFollow: Boolean, modifier: Modifier) {
    val pulse by rememberInfiniteTransition(label = "pulse").animateFloat(
        initialValue = 0f, targetValue = 1f,
        animationSpec = infiniteRepeatable(tween(2000), RepeatMode.Restart), label = "p",
    )
    Box(
        modifier
            .size(52.dp)
            .clip(CircleShape)
            .background(Color(0xFF12141B).copy(alpha = 0.85f))
            .border(1.dp, C.Accent.copy(alpha = if (playing) 0.25f + 0.3f * (1f - pulse) else 0.4f), CircleShape),
        contentAlignment = Alignment.Center,
    ) {
        Text("BGM", color = C.Accent, fontSize = 13.sp, fontWeight = FontWeight.Bold)
        // 左下角:演唱联动状态点(绿=已开启,灰=关闭),与右下角播放状态点左右对称
        Box(
            Modifier
                .align(Alignment.BottomStart)
                .padding(6.dp)
                .size(9.dp)
                .clip(CircleShape)
                .background(if (autoFollow) C.Ok else C.TextFaint),
        )
        // 右下角:播放状态点(绿=播放中,灰=未播放)
        Box(
            Modifier
                .align(Alignment.BottomEnd)
                .padding(6.dp)
                .size(9.dp)
                .clip(CircleShape)
                .background(if (playing) C.Ok else C.TextFaint),
        )
    }
}

@Composable
private fun BgmPanel(
    st: AppState,
    autoFollow: Boolean,
    onPrev: () -> Unit,
    onToggle: () -> Unit,
    onNext: () -> Unit,
    onVol: (Int) -> Unit,
    onAutoFollow: (Boolean) -> Unit,
    onClose: () -> Unit,
    modifier: Modifier = Modifier,
) {
    Column(
        modifier
            .width(260.dp)
            .clip(RoundedCornerShape(18.dp))
            .background(Color(0xFF101219).copy(alpha = 0.94f))
            .border(1.dp, C.Accent.copy(alpha = 0.28f), RoundedCornerShape(18.dp))
            // 吃掉面板内的点击,别穿透到背后蒙层导致误收起
            .pointerInput(Unit) { detectTapGestures { } }
            .padding(14.dp),
    ) {
        Row(Modifier.padding(bottom = 9.dp), verticalAlignment = Alignment.CenterVertically) {
            Box(Modifier.size(6.dp).clip(CircleShape).background(C.Accent))
            Text("  QQ音乐 · 背景音乐", color = C.Accent, fontSize = F.pill, fontWeight = FontWeight.Bold)
            WeightSpacer()
            Box(Modifier.size(26.dp).clip(RoundedCornerShape(8.dp)).background(Color.White.copy(alpha = 0.06f))
                .noRippleClick { onClose() }, contentAlignment = Alignment.Center) {
                CloseIcon(C.TextDim, 15.dp)
            }
        }
        Text(st.bgmNowLine, color = Color(0xFFC3C8D2), fontSize = F.pill, maxLines = 1, overflow = TextOverflow.Ellipsis,
            modifier = Modifier.padding(bottom = 12.dp))
        Row(Modifier.padding(bottom = 11.dp).align(Alignment.CenterHorizontally),
            horizontalArrangement = Arrangement.spacedBy(18.dp), verticalAlignment = Alignment.CenterVertically) {
            CircleButton(38.dp, Color.White.copy(alpha = 0.07f), onPrev) { BgmPrevIcon(C.Text, 18.dp) }
            CircleButton(48.dp, C.Accent, onToggle) { if (st.bgmPlaying) PauseIcon(C.OnAccent, 20.dp) else PlayIcon(C.OnAccent, 22.dp) }
            CircleButton(38.dp, Color.White.copy(alpha = 0.07f), onNext) { SkipNextIcon(C.Text, 18.dp) }
        }
        Row(verticalAlignment = Alignment.CenterVertically) {
            VolumeIcon(C.TextDim, 15.dp)
            Slider(
                value = st.bgmVol.toFloat(), onValueChange = { onVol(it.roundToInt()) },
                valueRange = 0f..100f, modifier = Modifier.weight(1f).padding(horizontal = 8.dp),
                colors = SliderDefaults.colors(thumbColor = C.Accent, activeTrackColor = C.Accent, inactiveTrackColor = C.Stroke2),
            )
            Text("${st.bgmVol}", color = C.Text, fontSize = F.pill, fontWeight = FontWeight.Bold)
        }
        // 演唱联动开关:开唱自动暂停 BGM,停唱 2 秒后自动恢复;关掉则互不干涉
        Row(Modifier.padding(top = 4.dp), verticalAlignment = Alignment.CenterVertically) {
            Text("演唱联动", color = if (autoFollow) C.Text else C.TextDim, fontSize = F.pill, fontWeight = FontWeight.Bold)
            Text("  开唱暂停 · 停唱续播", color = C.TextFaint, fontSize = F.pill, maxLines = 1, overflow = TextOverflow.Ellipsis,
                modifier = Modifier.weight(1f))
            Switch(
                checked = autoFollow, onCheckedChange = onAutoFollow,
                modifier = Modifier.scale(0.72f),
                colors = SwitchDefaults.colors(
                    checkedThumbColor = C.OnAccent, checkedTrackColor = C.Accent,
                    uncheckedThumbColor = C.TextDim, uncheckedTrackColor = C.Stroke2,
                ),
            )
        }
    }
}

