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
import androidx.compose.foundation.layout.statusBarsPadding
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.BasicTextField
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.SolidColor
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.example.liveremote.ui.components.BackIcon
import com.example.liveremote.ui.components.SectionLabel
import com.example.liveremote.ui.components.connColor
import com.example.liveremote.ui.components.noRippleClick
import com.example.liveremote.ui.theme.C
import com.example.liveremote.ui.theme.F

@Composable
fun SettingsScreen(
    host: String,
    connected: Boolean,
    onClose: () -> Unit,
    onConnect: (String) -> Unit,
    onDisconnect: () -> Unit,
) {
    var ip by remember { mutableStateOf(host) }
    Column(Modifier.fillMaxSize().background(C.Bg).statusBarsPadding()) {
        Row(Modifier.fillMaxWidth().padding(16.dp), verticalAlignment = Alignment.CenterVertically) {
            Box(Modifier.size(40.dp).clip(CircleShape).background(C.Card).noRippleClick { onClose() },
                contentAlignment = Alignment.Center) { BackIcon(C.Text, 22.dp) }
            Text("  连接电脑", color = C.Text, fontSize = 20.sp, fontWeight = FontWeight.Bold)
        }
        Column(Modifier.padding(horizontal = 20.dp)) {
            Row(Modifier.padding(top = 8.dp, bottom = 22.dp), verticalAlignment = Alignment.CenterVertically) {
                Box(Modifier.size(11.dp).clip(CircleShape).background(connColor(connected)))
                Text("  ${if (connected) "已连 $host" else "未连接"}", color = C.TextDim, fontSize = F.body)
            }
            SectionLabel("电脑局域网 IP", Modifier.padding(bottom = 10.dp))
            Box(
                Modifier.fillMaxWidth().clip(RoundedCornerShape(14.dp)).background(C.Card)
                    .border(1.dp, C.Stroke2, RoundedCornerShape(14.dp)).padding(15.dp),
            ) {
                if (ip.isEmpty()) Text("192.168.1.6", color = C.TextDim, fontSize = 17.sp)
                BasicTextField(
                    value = ip, onValueChange = { ip = it },
                    textStyle = TextStyle(color = C.Text, fontSize = 17.sp),
                    cursorBrush = SolidColor(C.Accent), singleLine = true, modifier = Modifier.fillMaxWidth(),
                )
            }
            Box(
                Modifier.fillMaxWidth().padding(top = 16.dp).clip(RoundedCornerShape(14.dp)).background(C.Accent)
                    .noRippleClick { if (ip.isNotBlank()) onConnect(ip.trim()) }.padding(15.dp),
                contentAlignment = Alignment.Center,
            ) { Text("连接", color = C.OnAccent, fontSize = 16.sp, fontWeight = FontWeight.Bold) }

            Box(Modifier.fillMaxWidth().height(1.dp).padding(top = 0.dp).background(C.Card))
            if (connected) {
                Box(
                    Modifier.fillMaxWidth().padding(top = 24.dp).clip(RoundedCornerShape(14.dp)).background(C.Card)
                        .border(1.dp, C.Stroke2, RoundedCornerShape(14.dp)).noRippleClick { onDisconnect() }.padding(15.dp),
                    contentAlignment = Alignment.Center,
                ) { Text("断开连接", color = C.Danger, fontSize = F.body) }
            }
            Text("已记住上次连接的电脑", color = C.TextFaint, fontSize = F.pill,
                modifier = Modifier.fillMaxWidth().padding(top = 22.dp), textAlign = androidx.compose.ui.text.style.TextAlign.Center)
        }
    }
}
