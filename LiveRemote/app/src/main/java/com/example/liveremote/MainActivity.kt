package com.example.liveremote

import android.media.AudioManager
import android.os.Bundle
import android.view.KeyEvent
import androidx.activity.ComponentActivity
import androidx.activity.compose.BackHandler
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.activity.viewModels
import androidx.compose.animation.AnimatedVisibility
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.WindowInsets
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.navigationBarsPadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.statusBars
import androidx.compose.foundation.layout.windowInsetsTopHeight
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableLongStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.example.liveremote.ui.components.TabQueueIcon
import com.example.liveremote.ui.components.TabRemoteIcon
import com.example.liveremote.ui.components.TabSingIcon
import com.example.liveremote.ui.components.BgmFabOverlay
import com.example.liveremote.ui.components.noRippleClick
import com.example.liveremote.ui.screens.PickerSheet
import com.example.liveremote.ui.screens.QueueScreen
import com.example.liveremote.ui.screens.RemoteScreen
import com.example.liveremote.ui.screens.SettingsScreen
import com.example.liveremote.ui.screens.SingScreen
import com.example.liveremote.ui.theme.C
import com.example.liveremote.ui.theme.F
import com.example.liveremote.ui.theme.LiveRemoteTheme
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch

class MainActivity : ComponentActivity() {
    private val vm: RemoteViewModel by viewModels()

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()   // 自绘状态栏/导航栏区域,由 Compose 用 WindowInsets 留白
        setContent { LiveRemoteTheme { App(vm) } }
    }

    /**
     * 音量键 → 伴奏音量:唱歌时(已连接且有当前曲)按键先正常调手机媒体音量(弹系统音量条),
     * 再把新的百分比经 WS 同步给电脑播放器,伴奏音量始终 = 手机媒体音量百分比。
     * 空闲/未连接时不拦截,音量键保持系统默认行为。长按连发同样生效(onKeyDown 会重复回调)。
     */
    override fun onKeyDown(keyCode: Int, event: KeyEvent?): Boolean {
        if (keyCode == KeyEvent.KEYCODE_VOLUME_UP || keyCode == KeyEvent.KEYCODE_VOLUME_DOWN) {
            val st = vm.state.value
            if (st.connected && st.hasSong) {
                val am = getSystemService(AUDIO_SERVICE) as AudioManager
                am.adjustStreamVolume(
                    AudioManager.STREAM_MUSIC,
                    if (keyCode == KeyEvent.KEYCODE_VOLUME_UP) AudioManager.ADJUST_RAISE else AudioManager.ADJUST_LOWER,
                    AudioManager.FLAG_SHOW_UI,
                )
                vm.syncKaraokeVolFromPhone()
                return true
            }
        }
        return super.onKeyDown(keyCode, event)
    }
}

@Composable
private fun App(vm: RemoteViewModel) {
    val st by vm.state.collectAsStateWithLifecycle()
    val library by vm.library.collectAsStateWithLifecycle()
    val lyrics by vm.lyrics.collectAsStateWithLifecycle()
    val bgmAutoFollow by vm.bgmAutoFollow.collectAsStateWithLifecycle()

    var tab by rememberSaveable { mutableStateOf("sing") }
    var showSettings by remember { mutableStateOf(false) }
    var pickerOpen by remember { mutableStateOf(false) }

    // Toast
    var toastMsg by remember { mutableStateOf<String?>(null) }
    var toastSeq by remember { mutableIntStateOf(0) }
    LaunchedEffectCollectToast(vm) { toastMsg = it; toastSeq++ }
    if (toastMsg != null) {
        androidx.compose.runtime.LaunchedEffect(toastSeq) { delay(1600); toastMsg = null }
    }

    // 返回键:浮层(设置/点歌抽屉)开着先关浮层;否则两次返回才退出(2s 内第二次生效),防误触退到桌面。
    val activity = androidx.compose.ui.platform.LocalContext.current as? ComponentActivity
    var lastBackAt by remember { mutableLongStateOf(0L) }
    BackHandler {
        when {
            showSettings -> showSettings = false
            pickerOpen -> pickerOpen = false
            else -> {
                val now = System.currentTimeMillis()
                if (now - lastBackAt < 2000L) {
                    activity?.finish()
                } else {
                    lastBackAt = now
                    toastMsg = "再按一次退出程序"; toastSeq++
                }
            }
        }
    }

    // 进度本地插值(时钟源=电脑播放器,每 ~500ms WS 推一次真实进度)。帧驱动、只在绘制作用域读取,
    // 不再像老版那样每 50ms 重建全局 st 触发整树重组——那是卡顿主因。见 rememberPlayhead。
    val playhead = com.example.liveremote.ui.components.rememberPlayhead(st.posMs, st.playing, st.durMs)

    val goPick = { tab = "queue"; pickerOpen = true }

    Box(Modifier.fillMaxSize().background(C.Bg)) {
        Column(Modifier.fillMaxSize()) {
            // 状态栏留白(不与系统时间/电量重叠)
            Spacer(Modifier.fillMaxWidth().windowInsetsTopHeight(WindowInsets.statusBars))
            AnimatedVisibility(!st.connected) { DisconnectedBanner() }
            Box(Modifier.weight(1f).fillMaxWidth()) {
                when (tab) {
                    "sing" -> SingScreen(
                        st = st, playhead = playhead, lyrics = lyrics,
                        onOpenSettings = { showSettings = true },
                        onScene = vm::setScene, onKeyDelta = vm::keyDelta, onSeek = vm::seekDelta,
                        onPlayPause = vm::playPause, onToggleSource = vm::toggleVocal, onGoPick = goPick,
                    )
                    "queue" -> QueueScreen(
                        st = st, onOpenPicker = { pickerOpen = true }, onSkipNext = vm::playNext,
                        onClear = vm::clearQueue, onRemove = vm::removeAt, onMove = vm::moveInQueue,
                        onMoveTop = vm::moveTop, onOpenSettings = { showSettings = true },
                    )
                    "remote" -> RemoteScreen(
                        st = st, host = vm.currentHost(),
                        onReset = vm::resetScene,
                        onStudio = vm::toggleStudio, onPlayerWin = vm::togglePlayerWindow,
                        onPitch = vm::togglePitch, onSetlist = vm::toggleSetlist,
                        onGifts = vm::toggleGifts,
                        onOpenSettings = { showSettings = true },
                    )
                }
            }
            TabBar(tab) { tab = it }
        }

        // 悬浮 QQ音乐(除设置/抽屉外常驻,含遥控页——遥控页不再单列 QQ音乐控制区,统一走这枚悬浮球)
        if (!showSettings && !pickerOpen) {
            BgmFabOverlay(st, bgmAutoFollow, vm::bgmPrev, vm::bgmToggle, vm::bgmNext, vm::setVolume, vm::setBgmAutoFollow)
        }

        // Toast
        toastMsg?.let { msg ->
            Box(Modifier.fillMaxSize().padding(bottom = 150.dp), contentAlignment = Alignment.BottomCenter) {
                Box(
                    Modifier.clip(RoundedCornerShape(22.dp)).background(Color(0xFF1C1F27).copy(alpha = 0.96f))
                        .padding(horizontal = 18.dp, vertical = 10.dp),
                ) { Text(msg, color = C.Text, fontSize = F.sub) }
            }
        }

        // 点歌抽屉
        if (pickerOpen) {
            PickerSheet(library = library, onAdd = vm::enqueue, onClose = { pickerOpen = false })
        }

        // 设置(全屏覆盖)
        if (showSettings) {
            SettingsScreen(
                host = vm.currentHost(), connected = st.connected, onClose = { showSettings = false },
                onConnect = { vm.connect(it); showSettings = false }, onDisconnect = vm::disconnect,
            )
        }
    }
}

@Composable
private fun LaunchedEffectCollectToast(vm: RemoteViewModel, onMsg: (String) -> Unit) {
    androidx.compose.runtime.LaunchedEffect(Unit) { vm.toast.collect { onMsg(it) } }
}

@Composable
private fun DisconnectedBanner() {
    Row(
        Modifier.fillMaxWidth().background(C.DisconnBannerBg).padding(vertical = 8.dp, horizontal = 12.dp),
        horizontalArrangement = androidx.compose.foundation.layout.Arrangement.Center,
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Text("与电脑断开,重连中…", color = C.DisconnBannerFg, fontSize = F.sub, fontWeight = FontWeight.Medium)
    }
}

@Composable
private fun TabBar(current: String, onTab: (String) -> Unit) {
    Row(
        Modifier.fillMaxWidth().background(C.TabBar).navigationBarsPadding().padding(top = 7.dp, bottom = 4.dp),
    ) {
        TabItem("sing", "演唱", current, onTab) { c, s -> TabSingIcon(c, s) }
        TabItem("queue", "队列", current, onTab) { c, s -> TabQueueIcon(c, s) }
        TabItem("remote", "遥控", current, onTab) { c, s -> TabRemoteIcon(c, s) }
    }
}

@Composable
private fun androidx.compose.foundation.layout.RowScope.TabItem(
    key: String, label: String, current: String, onTab: (String) -> Unit,
    icon: @Composable (Color, androidx.compose.ui.unit.Dp) -> Unit,
) {
    val active = key == current
    val color = if (active) C.Accent else C.TabIdle
    Column(
        Modifier.weight(1f).noRippleClick { onTab(key) }.padding(vertical = 6.dp),
        horizontalAlignment = Alignment.CenterHorizontally,
    ) {
        icon(color, 25.dp)
        Text(label, color = color, fontSize = F.tiny, fontWeight = if (active) FontWeight.Bold else FontWeight.Normal,
            textAlign = TextAlign.Center, modifier = Modifier.padding(top = 4.dp))
    }
}
