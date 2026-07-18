package com.example.liveremote.ui.screens

import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
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
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import com.example.liveremote.model.AppState
import com.example.liveremote.ui.components.HoldToConfirmButton
import com.example.liveremote.ui.components.SectionLabel
import com.example.liveremote.ui.components.ToggleSwitch
import com.example.liveremote.ui.components.WeightSpacer
import com.example.liveremote.ui.components.connColor
import com.example.liveremote.ui.components.noRippleClick
import com.example.liveremote.ui.theme.C
import com.example.liveremote.ui.theme.F
import kotlin.math.roundToInt

// 声卡场景清单(演唱页 SingScreen 复用)。遥控页不再放场景按钮,场景切换在演唱页。
val SCENES = listOf(1 to "聊天", 2 to "湿唱", 3 to "干唱", 4 to "喇叭", 5 to "闭麦")

@Composable
fun RemoteScreen(
    st: AppState,
    host: String,
    onReset: () -> Unit,
    onDirector: (Boolean) -> Unit,
    onCamZoom: (Int) -> Unit,
    onStudio: () -> Unit,
    onPlayerWin: () -> Unit,
    onPitch: () -> Unit,
    onSetlist: () -> Unit,
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
        // 声卡场景:只留归位(场景切换在演唱页)
        SectionLabel("声卡场景", Modifier.padding(bottom = 12.dp))
        HoldToConfirmButton(label = "归位", hint = "(长按 2 秒归位)", onConfirm = onReset)

        // 自动切镜运镜
        SectionLabel("自动切镜运镜", Modifier.padding(top = 22.dp, bottom = 12.dp))
        Column(
            Modifier.fillMaxWidth().clip(RoundedCornerShape(16.dp)).background(C.Card).border(1.dp, C.Stroke, RoundedCornerShape(16.dp)),
        ) {
            Row(Modifier.fillMaxWidth().padding(15.dp), verticalAlignment = Alignment.CenterVertically) {
                Text("自动切镜运镜", color = C.Text, fontSize = F.body)
                WeightSpacer()
                ToggleSwitch(st.directorOn) { onDirector(!st.directorOn) }
            }
            Box(Modifier.fillMaxWidth().height(1.dp).background(C.Stroke))
            val zoomOn = !st.directorOn   // 自动切镜开启时,主镜放大禁用
            val labelColor = if (zoomOn) C.Text else C.TextDim
            Row(Modifier.fillMaxWidth().padding(start = 15.dp, end = 15.dp, top = 10.dp, bottom = 10.dp),
                verticalAlignment = Alignment.CenterVertically) {
                Text("主镜头放大", color = labelColor, fontSize = F.body)
                Slider(
                    value = st.camZoom.toFloat(), onValueChange = { onCamZoom(it.roundToInt()) },
                    valueRange = 100f..250f, enabled = zoomOn,
                    modifier = Modifier.weight(1f).padding(horizontal = 10.dp),
                    colors = SliderDefaults.colors(thumbColor = C.Accent, activeTrackColor = C.Accent, inactiveTrackColor = C.Stroke2),
                )
                Text(String.format("%.1fx", st.camZoom / 100f), color = labelColor, fontSize = F.pill, fontWeight = FontWeight.Bold)
            }
        }

        SectionLabel("窗口开关", Modifier.padding(top = 22.dp, bottom = 12.dp))
        Column(
            Modifier.fillMaxWidth().clip(RoundedCornerShape(16.dp)).background(C.Card).border(1.dp, C.Stroke, RoundedCornerShape(16.dp)),
        ) {
            WindowRow("Studio One 显示/隐藏", st.studioVisible, onStudio, divider = true)
            WindowRow("滚动歌单 显示/隐藏", st.setlistVisible, onSetlist, divider = true)
            WindowRow("K歌歌词 显示/隐藏", st.playerVisible, onPlayerWin, divider = true)
            WindowRow("音准线 显示/隐藏", st.pitchVisible, onPitch, divider = false)
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
