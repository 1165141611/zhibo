package com.example.liveremote.ui.screens

import androidx.compose.animation.AnimatedContent
import androidx.compose.animation.fadeIn
import androidx.compose.animation.fadeOut
import androidx.compose.animation.slideInVertically
import androidx.compose.animation.slideOutVertically
import androidx.compose.animation.togetherWith
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.State
import androidx.compose.runtime.derivedStateOf
import androidx.compose.runtime.getValue
import androidx.compose.runtime.remember
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.alpha
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.example.liveremote.model.AppState
import com.example.liveremote.model.SongLyrics
import com.example.liveremote.model.fmtMs
import com.example.liveremote.ui.components.KaraokeStage
import com.example.liveremote.ui.components.MicIcon
import com.example.liveremote.ui.components.PauseIcon
import com.example.liveremote.ui.components.Pill
import com.example.liveremote.ui.components.PlayIcon
import com.example.liveremote.ui.components.SeekBackIcon
import com.example.liveremote.ui.components.SmoothProgressBar
import com.example.liveremote.ui.components.SeekFwdIcon
import com.example.liveremote.ui.components.connColor
import com.example.liveremote.ui.components.noRippleClick
import com.example.liveremote.ui.theme.C
import com.example.liveremote.ui.theme.F

@Composable
fun SingScreen(
    st: AppState,
    playhead: State<Int>,
    lyrics: SongLyrics?,
    onOpenSettings: () -> Unit,
    onScene: (Int) -> Unit,
    onKeyDelta: (Int) -> Unit,
    onSeek: (Int) -> Unit,
    onPlayPause: () -> Unit,
    onToggleSource: () -> Unit,
    onGoPick: () -> Unit,
) {
    // 无歌(队列唱完/空闲)时**不整页替换**:布局保持,声卡快切与悬浮 BGM 照常可用,
    // 只禁用演唱相关控件、把歌词舞台换成"去点歌"引导。
    val active = st.hasSong

    // 播放时间文本经 derivedStateOf 派生:仅在整秒变化时重组一次,而非每帧。
    val posText by remember(playhead) { derivedStateOf { fmtMs(playhead.value) } }
    val durMs = if (active) st.durMs else 0   // 空闲时播放器还载着上一首,忽略其残留进度/时长

    Column(Modifier.fillMaxSize()) {
        // 顶部信息条
        Column(Modifier.fillMaxWidth().padding(start = 16.dp, end = 16.dp, top = 14.dp, bottom = 12.dp)) {
            Row(verticalAlignment = Alignment.Top) {
                Column(Modifier.weight(1f)) {
                    Text(if (active) st.nowTitle else "未点歌", color = if (active) C.Text else C.TextDim,
                        fontSize = F.songBig, fontWeight = FontWeight.Bold,
                        maxLines = 1, overflow = TextOverflow.Ellipsis)
                    Text(if (active) st.nowArtist else "队列空闲", color = C.TextDim, fontSize = F.sub,
                        modifier = Modifier.padding(top = 3.dp))
                }
                Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    Box(Modifier.size(11.dp).clip(CircleShape).background(connColor(st.connected)).noRippleClick { onOpenSettings() })
                    Pill(st.keyLabel, accent = active, modifier = Modifier.alpha(if (active) 1f else 0.4f))  // 原调:纯标签
                    // 原唱/伴奏:可点切换。青底+青边+切换箭头,凸显它可点,与左侧"原调"标签区分。
                    Row(
                        Modifier.alpha(if (active) 1f else 0.4f)
                            .clip(RoundedCornerShape(9.dp)).background(C.Accent.copy(alpha = 0.14f))
                            .border(1.dp, C.Accent.copy(alpha = 0.55f), RoundedCornerShape(9.dp))
                            .noRippleClick(enabled = active) { onToggleSource() }.padding(horizontal = 10.dp, vertical = 5.dp),
                        verticalAlignment = Alignment.CenterVertically,
                    ) {
                        Text(st.sourceLabel, color = C.Accent, fontSize = F.pill, fontWeight = FontWeight.Bold)
                        Text(" ⇄", color = C.Accent.copy(alpha = 0.85f), fontSize = F.pill, fontWeight = FontWeight.Bold)
                    }
                }
            }
            Row(Modifier.padding(top = 11.dp), verticalAlignment = Alignment.CenterVertically) {
                Text(if (active) posText else "0:00", color = if (active) C.Accent else C.TextDim,
                    fontSize = F.tiny, modifier = Modifier.width(32.dp))
                SmoothProgressBar(
                    progress = { if (active && durMs > 0) playhead.value.toFloat() / durMs else 0f },
                    modifier = Modifier.weight(1f),
                )
                Text(fmtMs(durMs), color = C.TextDim, fontSize = F.tiny,
                    modifier = Modifier.width(34.dp).padding(start = 4.dp), textAlign = androidx.compose.ui.text.style.TextAlign.End)
            }
        }

        // 舞台:有歌 → 音准线+歌词;无歌 → "去点歌"引导(占同一区域)
        if (active) {
            KaraokeStage(playhead, st.nowTitle, st.nowArtist, lyrics, Modifier.weight(1f).fillMaxWidth())
        } else {
            PickGuide(onGoPick, Modifier.weight(1f).fillMaxWidth())
        }

        // 控制条(声卡快切始终可用;演唱控件跟随 active 禁用)
        ControlBar(st, active, onScene, onKeyDelta, onSeek, onPlayPause)
    }
}

@Composable
private fun ControlBar(
    st: AppState,
    active: Boolean,                 // false=无当前曲:演唱控件禁用置灰,声卡快切不受影响
    onScene: (Int) -> Unit,
    onKeyDelta: (Int) -> Unit,
    onSeek: (Int) -> Unit,
    onPlayPause: () -> Unit,
) {
    Column(Modifier.fillMaxWidth().background(C.ControlBarBg).padding(start = 14.dp, end = 14.dp, top = 10.dp, bottom = 14.dp)) {
        // 声卡场景快切行
        Row(verticalAlignment = Alignment.CenterVertically, modifier = Modifier.padding(bottom = 9.dp)) {
            Text("声卡", color = C.TextFaint, fontSize = F.micro, fontWeight = FontWeight.Bold)
            Row(Modifier.padding(start = 6.dp).weight(1f), horizontalArrangement = Arrangement.spacedBy(5.dp)) {
                SCENES.forEach { (id, name) ->
                    val active = id == st.scene
                    Box(
                        Modifier.weight(1f).height(34.dp).clip(RoundedCornerShape(10.dp))
                            .background(if (active) C.Accent else Color(0xFF1A1D25))
                            .border(1.dp, if (active) Color.Transparent else C.Stroke2, RoundedCornerShape(10.dp))
                            .noRippleClick { onScene(id) },
                        contentAlignment = Alignment.Center,
                    ) { Text(name, color = if (active) C.OnAccent else Color(0xFF9AA0B0), fontSize = F.sub, fontWeight = FontWeight.Bold) }
                }
            }
        }
        // 下一首预览
        val next = st.queue.firstOrNull()
        Text(
            if (next != null) "下一首:${next.title} · ${next.artist}" else "下一首:队列已空",
            color = C.TextDim, fontSize = F.pill, modifier = Modifier.fillMaxWidth().padding(bottom = 9.dp),
            textAlign = androidx.compose.ui.text.style.TextAlign.Center,
        )
        // 主控行
        Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(6.dp)) {
            // 升降调胶囊
            Row(
                Modifier.alpha(if (active) 1f else 0.4f)
                    .clip(RoundedCornerShape(13.dp)).background(C.Card).border(1.dp, C.Stroke2, RoundedCornerShape(13.dp)),
                verticalAlignment = Alignment.CenterVertically,
            ) {
                Box(Modifier.size(width = 34.dp, height = 50.dp).noRippleClick(enabled = active) { onKeyDelta(-1) }, contentAlignment = Alignment.Center) {
                    Text("−", color = C.Text, fontSize = 22.sp)
                }
                AnimatedContent(
                    targetState = st.keyLabel,
                    transitionSpec = {
                        (slideInVertically { it / 2 } + fadeIn()) togetherWith
                            (slideOutVertically { -it / 2 } + fadeOut())
                    },
                    label = "key",
                    modifier = Modifier.width(38.dp),
                ) { lbl ->
                    Text(lbl, color = C.Accent, fontSize = F.sub, fontWeight = FontWeight.Bold,
                        modifier = Modifier.fillMaxWidth(), textAlign = androidx.compose.ui.text.style.TextAlign.Center)
                }
                Box(Modifier.size(width = 34.dp, height = 50.dp).noRippleClick(enabled = active) { onKeyDelta(1) }, contentAlignment = Alignment.Center) {
                    Text("+", color = C.Text, fontSize = 20.sp)
                }
            }
            Box(Modifier.weight(1f))
            CircleCtl(48.dp, C.Card, border = C.Stroke2, enabled = active, onClick = { onSeek(-5000) }) { SeekBackIcon(C.Text, 23.dp) }
            CircleCtl(60.dp, C.Accent, enabled = active, onClick = onPlayPause) { if (st.playing) PauseIcon(C.OnAccent, 25.dp) else PlayIcon(C.OnAccent, 27.dp) }
            CircleCtl(48.dp, C.Card, border = C.Stroke2, enabled = active, onClick = { onSeek(5000) }) { SeekFwdIcon(C.Text, 23.dp) }
            Box(Modifier.weight(1f))
        }
    }
}

@Composable
private fun CircleCtl(
    size: androidx.compose.ui.unit.Dp, bg: Color, onClick: () -> Unit,
    border: Color? = null, enabled: Boolean = true, content: @Composable () -> Unit,
) {
    Box(
        Modifier.alpha(if (enabled) 1f else 0.4f).size(size).clip(CircleShape).background(bg)
            .then(if (border != null) Modifier.border(1.dp, border, CircleShape) else Modifier)
            .noRippleClick(enabled = enabled) { onClick() },
        contentAlignment = Alignment.Center,
    ) { content() }
}

/** 无歌时占据歌词舞台位置的"去点歌"引导(不整页替换,声卡/BGM 控件保持可用)。 */
@Composable
private fun PickGuide(onGoPick: () -> Unit, modifier: Modifier = Modifier) {
    Column(
        modifier.padding(24.dp), horizontalAlignment = Alignment.CenterHorizontally,
        verticalArrangement = Arrangement.Center,
    ) {
        Box(Modifier.size(64.dp).clip(CircleShape).background(Color(0xFF14161D)).border(1.dp, C.Stroke, CircleShape),
            contentAlignment = Alignment.Center) { MicIcon(C.Accent, 29.dp) }
        Text("还没点歌", color = C.Text, fontSize = 22.sp, fontWeight = FontWeight.Bold, modifier = Modifier.padding(top = 16.dp))
        Text("队列空闲,BGM 顶上了 · 点一首继续唱", color = C.TextDim, fontSize = F.body,
            modifier = Modifier.padding(top = 10.dp), textAlign = androidx.compose.ui.text.style.TextAlign.Center)
        Box(
            Modifier.padding(top = 18.dp).clip(RoundedCornerShape(14.dp)).background(C.Accent).noRippleClick { onGoPick() }
                .padding(horizontal = 26.dp, vertical = 13.dp),
        ) { Text("去点歌 →", color = C.OnAccent, fontSize = 15.sp, fontWeight = FontWeight.Bold) }
    }
}
