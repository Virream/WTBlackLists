"""全局快捷键监听(Windows,零第三方依赖)。

通过 user32.GetAsyncKeyState 定时轮询按键状态实现:
- **可区分左右修饰键**(如"右 Shift"),这是 RegisterHotKey 做不到的;
- 无需消息钩子,不拦截按键,游戏仍能收到该按键;
- 采用"边沿触发"(松开→按下 瞬间才触发一次),避免长按重复。

小键盘兼容:NumLock 开启时小键盘 1/2/3 是 VK_NUMPAD1/2/3,
关闭时退化为 End/Down/PageDown,因此每个键同时监听两种虚拟键码。
"""
from __future__ import annotations

import ctypes
import logging
import sys

from PyQt6.QtCore import QObject, QTimer, pyqtSignal

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
    """全局快捷键轮询器: 命中时发出 triggered(动作名)。"""

    triggered = pyqtSignal(str)

    def __init__(self, bindings: dict[str, tuple[str, str]] | None = None,
                 interval_ms: int = 25, parent=None):
        super().__init__(parent)
        self.bindings = dict(bindings or DEFAULT_BINDINGS)
        self._interval = max(10, int(interval_ms))
        self._state: dict[str, bool] = {a: False for a in self.bindings}
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
        self._timer.timeout.connect(self._poll)
        self._timer.start()
        log.info("全局快捷键已启动: %s", {a: describe_binding(b) for a, b in self.bindings.items()})

    def stop(self) -> None:
        if self._timer is not None:
            self._timer.stop()
            self._timer.deleteLater()
            self._timer = None

    # ------------------------------------------------------------------
    def _down(self, vk: int) -> bool:
        """GetAsyncKeyState 最高位为 1 表示当前按下(SHORT 为负)。"""
        return self._user32.GetAsyncKeyState(vk) < 0

    def _poll(self) -> None:
        for action, (mod_name, key_name) in self.bindings.items():
            mod_vk = VK_MODIFIERS.get(mod_name)
            keys = VK_NUMPADS.get(key_name)
            if mod_vk is None or keys is None:
                continue
            active = self._down(mod_vk) and any(self._down(vk) for vk in keys)
            # 边沿触发: 仅在"上次未按下 → 本次按下"时发出信号
            if active and not self._state.get(action, False):
                log.debug("快捷键触发: %s", action)
                self.triggered.emit(action)
            self._state[action] = active
