"""全局快捷键监听(Windows,零第三方依赖)。

通过 user32.GetAsyncKeyState 定时轮询按键状态实现:
- **可区分左右修饰键**(如"右 Shift"),这是 RegisterHotKey 做不到的;
- 无需消息钩子,不拦截按键,游戏仍能收到该按键;
- **不要求组合键同时被按住**: 游戏里常是先按住 W 再按右Shift+小键盘 ——
  判定只看"修饰键 + 小键盘键",与其它按键(W/鼠标…)无关。

可靠性: GetAsyncKeyState 返回值的**最低位**表示"自上次调用以来该键被按过"。
主线程被游戏挤占、定时器被拖到几百毫秒才跑一次时,这一位也不会丢 ——
所以即使按下的一瞬间没被采样到,下一次轮询仍能发现"刚按过"并触发;
修饰键同样看这一位(右Shift 与小键盘键"在同一个被拖长的间隔里都按过"也算命中)。
反过来只用"当前是否按住"判定会漏按 —— 这是以前的写法, 游戏里经常按了没反应。

小键盘兼容:NumLock 开启时小键盘 1/2/3 是 VK_NUMPAD1/2/3,
关闭时退化为 End/Down/PageDown,因此每个键同时监听两种虚拟键码。
"""
from __future__ import annotations

import ctypes
import logging
import sys

from PyQt6.QtCore import QObject, QTimer, Qt, pyqtSignal

log = logging.getLogger("global_hotkey")

# 修饰键虚拟键码(区分左右)
VK_MODIFIERS: dict[str, int] = {
    "lshift": 0xA0,
    "rshift": 0xA1,
    "lctrl": 0xA2,
    "rctrl": 0xA3,
    "lalt": 0xA4,
    "ralt": 0xA5,
}

# 小键盘主键: 名称 -> (NumLock 开, NumLock 关) 两种虚拟键码
VK_NUMPADS: dict[str, tuple[int, int]] = {
    "num0": (0x60, 0x2D),  # Numpad0 / Insert
    "num1": (0x61, 0x23),  # Numpad1 / End
    "num2": (0x62, 0x28),  # Numpad2 / Down
    "num3": (0x63, 0x22),  # Numpad3 / PageDown
    "num4": (0x64, 0x25),  # Numpad4 / Left
    "num5": (0x65, 0x0C),  # Numpad5 / Clear
    "num6": (0x66, 0x27),  # Numpad6 / Right
    "num7": (0x67, 0x24),  # Numpad7 / Home
    "num8": (0x68, 0x26),  # Numpad8 / Up
    "num9": (0x69, 0x21),  # Numpad9 / PageUp
}

# 默认绑定: 动作 -> (修饰键, 小键盘键)
DEFAULT_BINDINGS: dict[str, tuple[str, str]] = {
    "start": ("rshift", "num1"),
    "stop": ("rshift", "num2"),
    "mark": ("rshift", "num3"),
}

ACTION_LABELS: dict[str, str] = {
    "start": "开始录制轨迹",
    "stop": "结束录制轨迹",
    "mark": "标记当前位置",
}


def describe_binding(binding: tuple[str, str]) -> str:
    """把绑定转成可读文本, 如 '右Shift + 小键盘1'。"""
    mod, key = binding
    mod_txt = {
        "lshift": "左Shift", "rshift": "右Shift",
        "lctrl": "左Ctrl", "rctrl": "右Ctrl",
        "lalt": "左Alt", "ralt": "右Alt",
    }.get(mod, mod)
    key_txt = "小键盘" + key[3:] if key.startswith("num") else key
    return f"{mod_txt} + {key_txt}"


class GlobalHotkeyWatcher(QObject):
    """全局快捷键轮询器: 命中时发出 triggered(动作名)。

    触发条件 = 小键盘键"刚按下或正按住" **且** 其修饰键"刚按下或正按住"。
    同理, 与其它按键无关 —— 一直按着 W 前进时照样能触发。
    按住不放只触发一次, 松开(或再按一次)后才会再触发。
    """

    triggered = pyqtSignal(str)

    def __init__(self, bindings: dict[str, tuple[str, str]] | None = None,
                 interval_ms: int = 20, parent=None):
        super().__init__(parent)
        self.bindings = dict(bindings or DEFAULT_BINDINGS)
        # 20ms 足够跟手(人按一下也有几十毫秒), 配合低位锁存不会漏按;
        # 不取更小是为了少打扰系统(这个轮询在游戏运行时一直在跑)
        self._interval = max(5, int(interval_ms))
        self._fired: dict[str, bool] = {a: False for a in self.bindings}
        self._user32 = None
        self._timer: QTimer | None = None
        if sys.platform == "win32":
            try:
                u32 = ctypes.windll.user32
                u32.GetAsyncKeyState.argtypes = [ctypes.c_int]
                u32.GetAsyncKeyState.restype = ctypes.c_short
                self._user32 = u32
            except (AttributeError, OSError) as exc:  # pragma: no cover
                log.warning("全局快捷键不可用: %s", exc)

    @staticmethod
    def supported() -> bool:
        return sys.platform == "win32"

    # ------------------------------------------------------------------
    def start(self) -> None:
        """启动轮询(须在 GUI 线程调用)。"""
        if self._user32 is None or self._timer is not None:
            return
        self._timer = QTimer(self)
        self._timer.setInterval(self._interval)
        self._timer.setTimerType(Qt.TimerType.CoarseTimer)
        self._timer.timeout.connect(self._poll)
        self._timer.start()
        log.info("全局快捷键已启动(每 %d ms 轮询): %s",
                 self._interval,
                 {a: describe_binding(b) for a, b in self.bindings.items()})

    def stop(self) -> None:
        if self._timer is not None:
            self._timer.stop()
            self._timer.deleteLater()
            self._timer = None

    # ------------------------------------------------------------------
    def _sample(self, vk: int) -> tuple[bool, bool]:
        """读一个键的状态 → (现在是否按住, 自上次调用以来是否被按过)。

        GetAsyncKeyState 返回值: 最高位 = 当前按下, 最低位 = 自上回查询以来按过。
        取 `& 0xFFFF` 是因为 ctypes 把它读成了有符号 short(高位为 1 时是负数)。
        """
        raw = int(self._user32.GetAsyncKeyState(vk)) & 0xFFFF
        return bool(raw & 0x8000), bool(raw & 0x0001)

    def _poll(self) -> None:
        for action, (mod_name, key_name) in self.bindings.items():
            mod_vk = VK_MODIFIERS.get(mod_name)
            keys = VK_NUMPADS.get(key_name)
            if mod_vk is None or keys is None:
                continue
            mod_down, mod_pressed = self._sample(mod_vk)
            key_down = key_pressed = False
            for vk in keys:          # NumLock 开/关两种键码都听
                down, pressed = self._sample(vk)
                key_down = key_down or down
                key_pressed = key_pressed or pressed

            if not key_down and not mod_down:
                self._fired[action] = False      # 组合完全松开 → 重新武装
            if not (key_pressed or key_down):
                continue
            if not (mod_pressed or mod_down):
                continue
            if self._fired[action] and not key_pressed:
                continue                          # 按住不放只触发一次
            self._fired[action] = True
            log.info("全局快捷键触发: %s (%s)", action, describe_binding((mod_name, key_name)))
            self.triggered.emit(action)
