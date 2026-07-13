package com.example.liveremote.ui.screens

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
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Slider
import androidx.compose.material3.SliderDefaults
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import com.example.liveremote.model.AppState
import com.example.liveremote.ui.components.BgmPrevIcon
import com.example.liveremote.ui.components.CircleButton
import com.example.liveremote.ui.components.HoldToConfirmButton
import com.example.liveremote.ui.components.PauseIcon
import com.example.liveremote.ui.components.PlayIcon
import com.example.liveremote.ui.components.SectionLabel
import com.example.liveremote.ui.components.SkipNextIcon
import com.example.liveremote.ui.components.ToggleSwitch
import com.example.liveremote.ui.components.VolumeIcon
import com.example.liveremote.ui.components.WeightSpacer
import com.example.liveremote.ui.components.connColor
import com.example.liveremote.ui.components.noRippleClick
import com.example.liveremote.ui.theme.C
import com.example.liveremote.ui.theme.F
import kotlin.math.roundToInt

val SCENES = listOf(1 to "聊天", 2 to "湿唱", 3 to "干唱", 4 to "喇叭", 5 to "闭麦")

@Composable
fun RemoteScreen(
    st: AppState,
    host: String,
    onScene: (Int) -> Unit,
    onReset: () -> Unit,
    onBgmPrev: () -> Unit,
    onBgmToggle: () -> Unit,
    onBgmNext: () -> Unit,
    onVol: (Int) -> Unit,
    onStudio: () -> Unit,
    onPlayerWin: () -> Unit,
    onOpenSettings: () -> Unit,
) {
    Column(Modifier.fillMaxSize()) {
        // 吸顶标题行(不随内容滚动)
        Row(Modifier.fillMaxWidth().padding(start = 16.dp, end = 16.dp, top = 14.dp, bottom = 14.dp),
            verticalAlignment = Alignment.CenterVertically) {
            Text("遥控", color = C.Text, fontSize = F.h1, fontWeight = FontWeight.Bold)
            WeightSpacer()
            Row(
                Modifier.clip(RoundedCornerShape(20.dp)).background(C.Card).border(1.dp, C.Stroke2, RoundedCornerShape(20.dp))
                    .noRippleClick { onOpenSettings() }.padding(horizontal = 10.dp, vertical = 5.dp),
                verticalAlignment = Alignment.CenterVertically,
            ) {
                Box(Modifier.size(9.dp).clip(androidx.compose.foundation.shape.CircleShape).background(connColor(st.connected)))
                Text("  ${if (st.connected) "已连 $host" else "未连接"}", color = C.TextDim, fontSize = F.pill)
            }
        }

      Column(Modifier.fillMaxWidth().weight(1f).verticalScroll(rememberScrollState())
          .padding(start = 16.dp, end = 16.dp, bottom = 22.dp)) {
        SectionLabel("声卡场景", Modifier.padding(bottom = 12.dp))
        SceneGrid(current = st.scene, onScene = onScene)
        HoldToConfirmButton(
            label = "归位", hint = "(长按 2 秒归位)", onConfirm = onReset,
            modifier = Modifier.padding(top = 12.dp),
        )

        SectionLabel("背景音乐 · QQ音乐", Modifier.padding(top = 22.dp, bottom = 12.dp))
        Column(
            Modifier.fillMaxWidth().clip(RoundedCornerShape(16.dp)).background(C.Card)
                .border(1.dp, C.Stroke, RoundedCornerShape(16.dp)).padding(vertical = 16.dp, horizontal = 15.dp),
        ) {
            Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.Center, verticalAlignment = Alignment.CenterVertically) {
                CircleButton(46.dp, C.CardAlt, onBgmPrev) { BgmPrevIcon(C.Text, 22.dp) }
                Box(Modifier.width(22.dp))
                CircleButton(58.dp, C.Accent, onBgmToggle) { if (st.bgmPlaying) PauseIcon(C.OnAccent, 24.dp) else PlayIcon(C.OnAccent, 26.dp) }
                Box(Modifier.width(22.dp))
                CircleButton(46.dp, C.CardAlt, onBgmNext) { SkipNextIcon(C.Text, 22.dp) }
            }
            Row(Modifier.fillMaxWidth().padding(top = 16.dp), verticalAlignment = Alignment.CenterVertically) {
                VolumeIcon(C.TextDim, 18.dp)
                Slider(
                    value = st.bgmVol.toFloat(), onValueChange = { onVol(it.roundToInt()) }, valueRange = 0f..100f,
                    modifier = Modifier.weight(1f).padding(horizontal = 12.dp),
                    colors = SliderDefaults.colors(thumbColor = C.Accent, activeTrackColor = C.Accent, inactiveTrackColor = C.Stroke2),
                )
                Text("${st.bgmVol}", color = C.Text, fontSize = F.sub, fontWeight = FontWeight.Bold)
            }
            Text(st.bgmNowLine, color = C.TextFaint, fontSize = F.pill, modifier = Modifier.fillMaxWidth().padding(top = 12.dp),
                textAlign = androidx.compose.ui.text.style.TextAlign.Center)
        }

        SectionLabel("窗口开关", Modifier.padding(top = 22.dp, bottom = 12.dp))
        Column(
            Modifier.fillMaxWidth().clip(RoundedCornerShape(16.dp)).background(C.Card).border(1.dp, C.Stroke, RoundedCornerShape(16.dp)),
        ) {
            WindowRow("Studio One 显示/隐藏", st.studioVisible, onStudio, divider = true)
            WindowRow("K歌歌词 显示/隐藏", st.playerVisible, onPlayerWin, divider = false)
        }
      }
    }
}

@Composable
private fun SceneGrid(current: Int, onScene: (Int) -> Unit) {
    Column(verticalArrangement = Arrangement.spacedBy(10.dp)) {
        SCENES.chunked(3).forEach { row ->
            Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                row.forEach { (id, name) ->
                    val active = id == current
                    Box(
                        Modifier.weight(1f).height(56.dp).clip(RoundedCornerShape(14.dp))
                            .background(if (active) C.Accent else C.Card)
                            .border(1.dp, if (active) Color.Transparent else C.Stroke2, RoundedCornerShape(14.dp))
                            .noRippleClick { onScene(id) },
                        contentAlignment = Alignment.Center,
                    ) { Text(name, color = if (active) C.OnAccent else C.Text, fontSize = F.rowTitle, fontWeight = FontWeight.Bold) }
                }
                repeat(3 - row.size) { Box(Modifier.weight(1f)) }
            }
        }
    }
}

@Composable
private fun WindowRow(label: String, on: Boolean, onToggle: () -> Unit, divider: Boolean) {
    Row(
        Modifier.fillMaxWidth().padding(15.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Text(label, color = C.Text, fontSize = F.body)
        WeightSpacer()
        ToggleSwitch(on, onToggle)
    }
    if (divider) Box(Modifier.fillMaxWidth().height(1.dp).background(C.Stroke))
}
