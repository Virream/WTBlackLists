"""提示音: 快捷键触发时播放 WAV(默认用 Windows 自带的几个音效)。

已确认的事实(2026-10-07 实测):
- `%SystemRoot%\\Media` 里就有现成的音效文件: `Speech On.wav` / `Speech Off.wav` /
  `Windows Balloon.wav` / `Windows Critical Stop.wav`(都 16 位 44.1kHz 立体声 PCM);
- **音量不能用 MCI 调**: `setaudio <别名> volume to <n>` 在 waveaudio 驱动上直接
  报 261「驱动程序无法识别指定的命令」(`setaudio ... on/off` 才有);
- **也不能用 `waveOutSetVolume`**: 那改的是整个系统的音量, 会影响用户听其它声音。
  所以音量靠**重采样**: 把 16 位 PCM 的样本乘以系数, 写到临时目录里再播
  (按"文件+音量"缓存, 换音量只重算一次)。

播放放在后台守护线程(阻塞式播完即退), 不阻塞 UI 与 8111 轮询。
开关 / 音量 / 自定义文件存在 QSettings(菜单「音效」里改)。
"""
from __future__ import annotations

import array
import hashlib
import logging
import os
import sys
import tempfile
import threading
import wave

from PyQt6.QtCore import QSettings

log = logging.getLogger("sound")

try:
    import winsound
except ImportError:  # 非 Windows
    winsound = None  # type: ignore[assignment]

# 动作 -> Windows 自带音效文件名
DEFAULT_FILES: dict[str, str] = {
    "start": "Speech On.wav",              # 开始录制
    "stop": "Speech Off.wav",              # 结束录制
    "mark": "Windows Balloon.wav",         # 标记当前位置
    "error": "Windows Critical Stop.wav",  # 操作被拒绝时的错误提示
}

# 找不到音效文件时的 MessageBeep 兜底
_FALLBACK_BEEPS: dict[str, str] = {
    "start": "MB_OK",
    "stop": "MB_ICONEXCLAMATION",
    "mark": "MB_ICONASTERISK",
    "error": "MB_ICONHAND",
}

ACTION_NAMES: dict[str, str] = {
    "start": "开始录制轨迹",
    "stop": "结束录制轨迹",
    "mark": "标记当前位置",
    "error": "错误提示",
}

DEFAULT_VOLUME = 70        # 百分比
_CACHE_SUBDIR = "WTRouteTester_sound"


# --------------------------------------------------------------------- 系统音效
def media_dir() -> str:
    """Windows 自带音效目录(通常是 C:\\Windows\\Media)。"""
    root = os.environ.get("SystemRoot") or os.environ.get("WINDIR") or r"C:\Windows"
    return os.path.join(root, "Media")


def find_in_media(name: str) -> str:
    """在系统音效目录里按文件名找(大小写不敏感); 找不到返回空串。"""
    if not name:
        return ""
    direct = os.path.join(media_dir(), name)
    if os.path.isfile(direct):
        return direct
    want = name.lower()
    try:
        for entry in os.listdir(media_dir()):
            if entry.lower() == want:
                return os.path.join(media_dir(), entry)
    except OSError as exc:
        log.debug("读取系统音效目录失败: %s", exc)
    return ""


# --------------------------------------------------------------------- 音量
def _cache_dir() -> str:
    return os.path.join(tempfile.gettempdir(), _CACHE_SUBDIR)


def _scaled_wav(path: str, volume: int) -> str:
    """按音量缩放样本后的临时 WAV 路径; 做不到(非 16 位 PCM 等)返回空串。

    缓存键 = 源文件路径 + 修改时间 + 大小 + 音量: 换文件或改音量才会重算。
    """
    try:
        st = os.stat(path)
    except OSError:
        return ""
    key = hashlib.sha1(
        f"{path}|{st.st_mtime_ns}|{st.st_size}|{volume}".encode("utf-8")
    ).hexdigest()[:16]
    out = os.path.join(_cache_dir(), key + ".wav")
    if os.path.isfile(out):
        return out
    try:
        with wave.open(path, "rb") as src:
            params = src.getparams()
            raw = src.readframes(params.nframes)
        if params.sampwidth != 2:
            return ""                      # 只缩放 16 位 PCM(系统音效都是)
        samples = array.array("h")
        samples.frombytes(raw)
        if sys.byteorder != "little":
            samples.byteswap()
        scale = max(0.0, min(1.0, float(volume) / 100.0))
        for i in range(len(samples)):
            samples[i] = int(samples[i] * scale)
        if sys.byteorder != "little":
            samples.byteswap()
        os.makedirs(_cache_dir(), exist_ok=True)
        tmp = out + ".tmp"
        with wave.open(tmp, "wb") as dst:
            dst.setparams(params)
            dst.writeframes(samples.tobytes())
        os.replace(tmp, out)
        return out
    except (OSError, wave.Error, ValueError, EOFError) as exc:
        log.debug("音量缩放失败(按原音量播放): %s", exc)
        return ""


def _beep(flags_name: str) -> None:
    if winsound is None:
        return
    try:
        winsound.MessageBeep(getattr(winsound, flags_name, winsound.MB_OK))
    except Exception as exc:  # noqa: BLE001 - 提示音失败不应影响主流程
        log.debug("提示音播放失败: %s", exc)


def play_path(path: str, volume: int) -> None:
    """后台线程播放一个 WAV(**阻塞式**播完即退); volume 为 0~100。"""
    if winsound is None or not path:
        return
    threading.Thread(target=_play_blocking, args=(path, int(volume)), daemon=True).start()


def _play_blocking(path: str, volume: int) -> None:
    if winsound is None or volume <= 0:
        return                                  # 音量为 0 = 静音
    target = path
    if volume < 100:
        scaled = _scaled_wav(path, volume)
        if scaled:
            target = scaled
    try:
        winsound.PlaySound(
            target,
            winsound.SND_FILENAME | winsound.SND_SYNC | winsound.SND_NODEFAULT,
        )
    except Exception as exc:  # noqa: BLE001
        log.debug("播放音效失败: %s(%s)", path, exc)


# --------------------------------------------------------------------- 设置
class SoundSettings:
    """提示音设置(开关 / 音量 / 每个动作自定义文件), 持久化到 QSettings。

    自定义文件为空 = 用系统自带音效(见 DEFAULT_FILES); 若那个文件在系统里也找不到,
    就退回 MessageBeep。
    """

    def __init__(self, settings: QSettings | None = None):
        self._s = settings or QSettings("WTBlackList", "RouteTester")

    def _int(self, key: str, default: int) -> int:
        try:
            return int(self._s.value(key, default))
        except (TypeError, ValueError):
            return default

    # ---- 开关
    @property
    def enabled(self) -> bool:
        return self._int("sound_enabled", 1) != 0

    def set_enabled(self, on: bool) -> None:
        self._s.setValue("sound_enabled", 1 if on else 0)

    # ---- 音量(0~100)
    @property
    def volume(self) -> int:
        return max(0, min(100, self._int("sound_volume", DEFAULT_VOLUME)))

    def set_volume(self, volume: int) -> None:
        self._s.setValue("sound_volume", max(0, min(100, int(volume))))

    # ---- 每个动作的音效文件
    def custom_file(self, action: str) -> str:
        """用户自定义的文件路径(未设置 / 文件已被删掉 → 空串)。"""
        path = str(self._s.value(f"sound_file_{action}", "") or "")
        return path if path and os.path.isfile(path) else ""

    def set_file(self, action: str, path: str) -> None:
        self._s.setValue(f"sound_file_{action}", path or "")

    def path_for(self, action: str) -> str:
        """实际要播的文件: 自定义优先, 其次系统自带; 都没有 → 空串(用 MessageBeep)。"""
        return self.custom_file(action) or find_in_media(DEFAULT_FILES.get(action, ""))

    def label_for(self, action: str) -> str:
        """界面上显示的一行说明。"""
        custom = self.custom_file(action)
        if custom:
            return f"自定义: {os.path.basename(custom)}"
        name = DEFAULT_FILES.get(action, "")
        return f"默认: {name}" if name and find_in_media(name) else "默认(系统提示音)"


_settings: SoundSettings | None = None


def settings() -> SoundSettings:
    """进程内共享的设置对象。"""
    global _settings
    if _settings is None:
        _settings = SoundSettings()
    return _settings


def play(action: str, force: bool = False) -> None:
    """播放某个动作的提示音(force=True 用于「试听」: 忽略启用开关)。"""
    snd = settings()
    if not (force or snd.enabled):
        return
    path = snd.path_for(action)
    if path:
        play_path(path, snd.volume)
    else:
        threading.Thread(target=_beep,
                         args=(_FALLBACK_BEEPS.get(action, "MB_OK"),),
                         daemon=True).start()


def play_error(force: bool = False) -> None:
    """播放错误提示音(操作被拒绝时)。"""
    play("error", force=force)
