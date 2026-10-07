"""全局快捷键监听(Windows,零第三方依赖)。

有**两套通道**同时工作, 任意一套命中就触发(窗口侧会去重):

1. **系统级热键**(`RegisterHotKey`) —— 操作系统直接回调我们, 不依赖定时器,
   也不去读任何按键状态, 最可靠; 缺点是不能区分左右修饰键。
2. **轮询**(`GetAsyncKeyState`) —— 作为补充: 可以区分右 Shift 这类精确组合,
   而且即使游戏把主线程挤得跑不动也不丢按键(看返回值的**最低位**:
   “自上次调用以来按过”。旧写法只看“当前是否按住”, 慢一波轮询就漏了);
   **只看“修饰键 + 目标键”这两个键**, 与 W/鼠标等其它按键无关。

两套都**不拦截按键**: 游戏仍然会收到这个键(所以选的键最好在游戏里没绑功能,
或自己把游戏里那个绑定解掉)。

默认绑定改成了 **F5 / F6 / F7**(用户反馈右Shift+小键盘在游戏里没反应 ——
怀疑是键盘多键冲突): 不带修饰键的单个按键不需要和 W 同时按, 冲突面最小。
绑定可在菜单「快捷键」里改, 存在 QSettings(`hotkey_<动作>` = "修饰键,按键")。
"""
from __future__ import annotations

import ctypes
import logging
import os
import sys

from PyQt6.QtCore import QObject, QSettings, QTimer, Qt, pyqtSignal

log = logging.getLogger("global_hotkey")

# 修饰键虚拟键码(区分左右); 空串 = 不要修饰键
VK_MODIFIERS: dict[str, int] = {
    "lshift": 0xA0,
    "rshift": 0xA1,
    "lctrl": 0xA2,
    "rctrl": 0xA3,
    "lalt": 0xA4,
    "ralt": 0xA5,
}

MODIFIER_LABELS: dict[str, str] = {
    "": "(无)",
    "lshift": "左Shift", "rshift": "右Shift",
    "lctrl": "左Ctrl", "rctrl": "右Ctrl",
    "lalt": "左Alt", "ralt": "右Alt",
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

# 常用单键(F1~F12 与几个编辑键): 名称 -> 虚拟键码
VK_KEYS: dict[str, int] = {
    **{f"f{i}": 0x70 + i - 1 for i in range(1, 13)},     # F1=0x70 … F12=0x7B
    "insert": 0x2D, "delete": 0x2E, "home": 0x24, "end": 0x23,
    "pageup": 0x21, "pagedown": 0x22, "backspace": 0x08, "pause": 0x13,
}

# 下拉框里的可选按键(顺序 = 界面上的顺序)
KEY_CHOICES: tuple[str, ...] = (
    *(f"f{i}" for i in range(1, 13)),
    *(f"num{i}" for i in range(10)),
    "insert", "delete", "home", "end", "pageup", "pagedown", "pause",
)


def key_label(name: str) -> str:
    """按键名 → 界面显示用的文本。"""
    if name.startswith("num"):
        return "小键盘" + name[3:]
    if name.startswith("f") and name[1:].isdigit():
        return name.upper()
    return {"insert": "Insert", "delete": "Delete", "home": "Home", "end": "End",
            "pageup": "PageUp", "pagedown": "PageDown", "backspace": "Backspace",
            "pause": "Pause"}.get(name, name.upper())


# 默认绑定: 动作 -> (修饰键或"", 按键)
DEFAULT_BINDINGS: dict[str, tuple[str, str]] = {
    "start": ("", "f5"),      # 开始录制
    "stop": ("", "f6"),       # 结束录制
    "mark": ("", "f7"),       # 标记位置
}

ACTION_LABELS: dict[str, str] = {
    "start": "开始录制轨迹",
    "stop": "结束录制轨迹",
    "mark": "标记当前位置",
}


def describe_binding(binding: tuple[str, str] | None) -> str:
    """把绑定转成可读文本, 如 '右Shift + 小键盘1' / 'F5'。"""
    if not binding:
        return "(未设置)"
    mod, key = binding
    mod_txt = MODIFIER_LABELS.get(mod, mod or "")
    key_txt = key_label(key)
    return f"{mod_txt} + {key_txt}" if mod_txt and mod_txt != "(无)" else key_txt


def _parse_binding(text: str) -> tuple[str, str] | None:
    """还原 QSettings 里的 "修饰键,按键" 字符串。"""
    parts = [p.strip() for p in str(text or "").split(",")]
    if len(parts) != 2 or not parts[1]:
        return None
    mod, key = parts
    if mod and mod not in VK_MODIFIERS:
        return None
    if key not in VK_KEYS and key not in VK_NUMPADS:
        return None
    return mod, key


def load_bindings(settings: QSettings) -> dict[str, tuple[str, str]]:
    """从 QSettings 读绑定; 缺项/无效项用默认值补齐。"""
    out: dict[str, tuple[str, str]] = {}
    for action, default in DEFAULT_BINDINGS.items():
        saved = _parse_binding(str(settings.value(f"hotkey_{action}", "") or ""))
        out[action] = saved or default
    return out


def save_bindings(settings: QSettings, bindings: dict[str, tuple[str, str]]) -> None:
    """把绑定写回 QSettings。"""
    for action, binding in bindings.items():
        settings.setValue(f"hotkey_{action}", f"{binding[0]},{binding[1]}")


def reset_bindings(settings: QSettings) -> None:
    """清掉自定义绑定(回到默认)。"""
    for action in DEFAULT_BINDINGS:
        settings.remove(f"hotkey_{action}")


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

    def set_bindings(self, bindings: dict[str, tuple[str, str]]) -> None:
        """换一套绑定(用户改了设置时调), 并重置“按住中”的状态。"""
        self.bindings = dict(bindings)
        self._fired = {a: False for a in self.bindings}

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
            codes = _key_codes(key_name)
            if not codes:
                continue
            mod_vk = VK_MODIFIERS.get(mod_name) if mod_name else None
            mod_down = mod_pressed = False
            if mod_vk is not None:
                mod_down, mod_pressed = self._sample(mod_vk)
            key_down = key_pressed = False
            for vk in codes:          # NumLock 开/关两种键码都听
                down, pressed = self._sample(vk)
                key_down = key_down or down
                key_pressed = key_pressed or pressed

            if not key_down and not mod_down:
                self._fired[action] = False      # 完全松开 → 重新武装
            if not (key_pressed or key_down):
                continue                          # 目标键没动
            if mod_vk is not None and not (mod_pressed or mod_down):
                continue                          # 要修饰键但没按
            if self._fired[action] and not key_pressed:
                continue                          # 按住不放只触发一次
            self._fired[action] = True
            log.info("全局快捷键触发(轮询): %s (%s)", action,
                     describe_binding((mod_name, key_name)))
            self.triggered.emit(action)


# ====================================================================== 系统热键
# RegisterHotKey 相关常量
_MOD_ALT = 0x0001
_MOD_CONTROL = 0x0002
_MOD_SHIFT = 0x0004
_MOD_NOREPEAT = 0x4000          # Windows 7+: 按住不放不重复触发
_WM_HOTKEY = 0x0312
_HWND_MESSAGE = -3              # 消息窗口(不显示、不进任务栏)
_MOD_BY_NAME = {"lshift": _MOD_SHIFT, "rshift": _MOD_SHIFT,
                "lctrl": _MOD_CONTROL, "rctrl": _MOD_CONTROL,
                "lalt": _MOD_ALT, "ralt": _MOD_ALT}


def _key_codes(name: str) -> tuple[int, ...]:
    """按键名 → 要监听的虚拟键码表(小键盘键在 NumLock 关时会退化成另一个键)。"""
    if name in VK_NUMPADS:
        return VK_NUMPADS[name]
    if name in VK_KEYS:
        return (VK_KEYS[name],)
    return ()


class GlobalHotkey(QObject):
    """系统级热键(**RegisterHotKey**): 操作系统直接回调, 不靠轮询、不读按键状态。

    两条通道(本类 + `GlobalHotkeyWatcher`)同时工作, 窗口侧会去重。
    与轮询的区别: 不能区分左右 Shift, 但**最不容易受游戏影响** ——
    用户反馈“游戏里按了没反应”时就是靠它兜底。

    实现: 建一个**消息窗口**(HWND_MESSAGE, 不显示、不进任务栏)接收 WM_HOTKEY。
    Qt 的事件循环会把本线程队列里的消息取出来 DispatchMessage, 所以自建窗口的
    WndProc 同样会被调到 —— 不需要自己再跑消息循环。
    """

    triggered = pyqtSignal(str)

    _ID_BASE = 0x5100               # 热键 id 起点

    def __init__(self, bindings: dict[str, tuple[str, str]] | None = None, parent=None):
        super().__init__(parent)
        self.bindings = dict(bindings or DEFAULT_BINDINGS)
        self.failed: dict[str, str] = {}      # 动作 -> 注册失败原因
        self._hwnd = 0
        self._ids: dict[int, str] = {}
        self._user32 = None
        self._proc = None               # WNDPROC 回调必须留引用(否则被回收 → 崩溃)
        if sys.platform == "win32":
            try:
                self._user32 = ctypes.WinDLL("user32", use_last_error=True)
                ctypes.WinDLL("kernel32", use_last_error=True)
            except OSError as exc:  # pragma: no cover
                log.warning("系统热键不可用: %s", exc)

    @staticmethod
    def supported() -> bool:
        return sys.platform == "win32"

    # ------------------------------------------------------------------
    def start(self) -> None:
        """建消息窗口 + 注册全部热键(须在 GUI 线程调用)。"""
        if self._user32 is None or self._hwnd:
            return
        if not self._create_window():
            return
        self._register_all()

    def stop(self) -> None:
        self._unregister_all()
        if self._hwnd:
            try:
                self._user32.DestroyWindow(self._hwnd)
            except (OSError, AttributeError):
                pass
            self._hwnd = 0

    def apply(self, bindings: dict[str, tuple[str, str]]) -> None:
        """换一套绑定(用户改了设置时调): 先注销旧的, 再注册新的。"""
        self.bindings = dict(bindings)
        if not self._hwnd:
            return
        self._unregister_all()
        self._register_all()

    # ------------------------------------------------------------------
    def _create_window(self) -> bool:
        from ctypes import wintypes

        u = self._user32
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, ctypes.c_uint,
                                     ctypes.c_size_t, ctypes.c_ssize_t)

        # ⚠️ 必须写全 argtypes/restype: 不写的话 ctypes 按 32 位处理,
        # 64 位的 HMODULE/HWND 会被截断 —— 实测导致 CreateWindowExW 直接失败(err=1400)
        kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        kernel32.GetModuleHandleW.restype = wintypes.HMODULE
        u.RegisterClassExW.argtypes = [ctypes.c_void_p]
        u.RegisterClassExW.restype = wintypes.ATOM
        u.CreateWindowExW.argtypes = [
            wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
        u.CreateWindowExW.restype = wintypes.HWND
        u.DefWindowProcW.argtypes = [wintypes.HWND, ctypes.c_uint, ctypes.c_size_t,
                                     ctypes.c_ssize_t]
        u.DefWindowProcW.restype = ctypes.c_ssize_t
        u.DestroyWindow.argtypes = [wintypes.HWND]
        u.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_uint,
                                     ctypes.c_uint]
        u.RegisterHotKey.restype = wintypes.BOOL
        u.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
        u.UnregisterHotKey.restype = wintypes.BOOL

        def _wnd_proc(hwnd, msg, wparam, lparam):
            if msg == _WM_HOTKEY:
                action = self._ids.get(int(wparam))
                if action:
                    log.info("全局快捷键触发(系统热键): %s", action)
                    self.triggered.emit(action)
                return 0
            return u.DefWindowProcW(hwnd, msg, wparam, lparam)

        self._proc = WNDPROC(_wnd_proc)
        hinst = kernel32.GetModuleHandleW(None)
        cls_name = f"WTRouteTesterHotkey_{os.getpid()}"

        class WNDCLASSEXW(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_uint), ("style", ctypes.c_uint),
                        ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                        ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE),
                        ("hIcon", wintypes.HICON), ("hCursor", wintypes.HANDLE),
                        ("hbrBackground", wintypes.HBRUSH), ("lpszMenuName", wintypes.LPCWSTR),
                        ("lpszClassName", wintypes.LPCWSTR), ("hIconSm", wintypes.HICON)]

        wc = WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
        wc.lpfnWndProc = self._proc
        wc.hInstance = hinst
        wc.lpszClassName = cls_name
        if not u.RegisterClassExW(ctypes.byref(wc)):
            err = ctypes.get_last_error()
            if err != 1410:                 # 1410 = 类已注册(同进程重启用), 可继续
                log.warning("注册热键窗口类失败(err=%s) —— 仅能靠轮询", err)
                return False
        self._hwnd = int(u.CreateWindowExW(0, cls_name, "WTRouteTesterHotkey", 0,
                                           0, 0, 0, 0, _HWND_MESSAGE, None, hinst, None) or 0)
        if not self._hwnd:
            log.warning("创建热键消息窗口失败(err=%s) —— 仅能靠轮询",
                        ctypes.get_last_error())
            return False
        return True

    def _register_all(self) -> None:
        self.failed.clear()
        hotkey_id = self._ID_BASE
        for action, (mod_name, key_name) in self.bindings.items():
            mods = _MOD_BY_NAME.get(mod_name, 0)
            codes = _key_codes(key_name)
            ok = False
            for vk in codes:                # 小键盘两种键码都试
                hotkey_id += 1
                if self._user32.RegisterHotKey(self._hwnd, hotkey_id, mods | _MOD_NOREPEAT, vk):
                    self._ids[hotkey_id] = action
                    ok = True
                    break
            if not ok:
                err = ctypes.get_last_error()
                self.failed[action] = (f"注册失败(err={err}, 多为该键已被其它程序占用)"
                                       if err else "注册失败")
                log.warning("系统热键注册失败: %s = %s(err=%s)", action,
                            describe_binding((mod_name, key_name)), err)
            else:
                log.info("系统热键已注册: %s = %s", action,
                         describe_binding((mod_name, key_name)))

    def _unregister_all(self) -> None:
        for hotkey_id in list(self._ids):
            try:
                self._user32.UnregisterHotKey(self._hwnd, hotkey_id)
            except (OSError, AttributeError):
                pass
        self._ids.clear()
