"""跑图线路测试器的单实例控制: 只允许运行一份, 重复启动会把已有窗口调到前面。

**与黑名单主程序的单实例互不影响**(各有各的互斥体名字), 两个程序可以同时开。

做法(Windows 优先, 零第三方依赖, **不使用 QtNetwork**):
1. **命名互斥体**(`kernel32.CreateMutexW`)判重 —— 第二个进程拿不到锁就直接退出;
2. 第二个进程再**枚举顶层窗口**, 按标题里含"跑图线路测试器"找到已在运行的那个窗口,
   把它 `ShowWindow(SW_RESTORE)` + `SetForegroundWindow` 调到前面
   (由"刚被点开"的第二个进程去抢前台, 比让后台的老进程自己抢更容易成功;
   实在抢不到就 `FlashWindow` 闪任务栏提醒)。

⚠️ 曾经用 QLocalServer/QLocalSocket 做进程间通知, 结果打包版直接崩:
   `tools/trim_qt.py` 会把没被引用的 Qt 模块 DLL 精简掉, 于是
   `import PyQt6.QtNetwork` 抛 `ImportError: DLL load failed`。
   改动这里的实现时**不要引入新的 Qt 模块** —— 只用 ctypes/Win32。
"""
from __future__ import annotations

import ctypes
import logging
import os
import sys
import tempfile

log = logging.getLogger("route_instance")

MUTEX_NAME = "WTRouteTester_SingleInstance"
# 找"已在运行的窗口"时匹配标题里的这个片段(见 route_window 的 setWindowTitle)
TITLE_HINT = "跑图线路测试器"
_ERROR_ALREADY_EXISTS = 183

# 句柄/锁对象必须有引用一直活着, 否则会被回收 → 锁就没了
_handle: object | None = None
# 枚举窗口时收集结果的临时列表(回调里填, 之后清掉)
_found: list[int] = []



# ------------------------------------------------------------------ 单实例锁
def acquire_mutex(name: str = MUTEX_NAME) -> tuple[bool, object | None]:
    """取一个命名单实例锁 → (本进程是否拿到锁, 句柄)。

    Windows 用命名互斥体; 其它平台用 QLockFile(锁文件)。
    `name` 可传: 测试里用独立名字, 免得影响正在运行的程序。
    """
    if sys.platform.startswith("win"):
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create = kernel32.CreateMutexW
        create.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
        create.restype = wintypes.HANDLE
        handle = create(None, False, name)
        already = ctypes.get_last_error() == _ERROR_ALREADY_EXISTS
        if already:
            return False, handle          # 句柄仍要留着(CloseHandle 由 release_mutex 做)
        return True, handle

    from PyQt6.QtCore import QLockFile

    lock = QLockFile(os.path.join(tempfile.gettempdir(), f"{name}.lock"))
    lock.setStaleLockTime(0)              # 由我们自己的 PID 存活检测判断
    return lock.tryLock(0), lock


def release_mutex(handle: object | None) -> None:
    """释放锁(主要给测试用; 进程退出时系统也会自动释放)。"""
    if handle is None:
        return
    if sys.platform.startswith("win"):
        try:
            ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(handle)
        except (OSError, AttributeError):
            pass
    else:
        try:
            handle.unlock()               # type: ignore[attr-defined]
        except (AttributeError, RuntimeError):
            pass


# ------------------------------------------------------------------ 找窗口 / 置前
def _user32():
    """带好 argtypes 的 user32(只在 Windows 上有意义)。"""
    from ctypes import wintypes

    u = ctypes.WinDLL("user32", use_last_error=True)
    u.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
    u.FindWindowW.restype = wintypes.HWND
    u.EnumWindows.argtypes = [ctypes.c_void_p, wintypes.LPARAM]
    u.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    u.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    u.IsWindowVisible.argtypes = [wintypes.HWND]
    u.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    u.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    u.SetForegroundWindow.argtypes = [wintypes.HWND]
    u.FlashWindow.argtypes = [wintypes.HWND, wintypes.BOOL]
    u.GetForegroundWindow.restype = wintypes.HWND
    return u


def _window_title(hwnd: int) -> str:
    """窗口标题(取不到返回空串)。"""
    u = _user32()
    length = int(u.GetWindowTextLengthW(hwnd))
    if length <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(length + 2)
    u.GetWindowTextW(hwnd, buf, length + 2)
    return buf.value


def _window_pid(hwnd: int) -> int:
    from ctypes import wintypes

    pid = wintypes.DWORD()
    _user32().GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value)


def find_app_window(exclude_pid: int | None = None,
                    title_hint: str = TITLE_HINT) -> int:
    """找一个可见的顶层窗口, 标题里含 `title_hint`, 且不属于 `exclude_pid`。

    (不用 FindWindowW 精确匹配: 程序运行中标题可能带后缀, 用"包含"更稳。)
    找不到返回 0。
    """
    if not sys.platform.startswith("win"):
        return 0
    from ctypes import wintypes

    u = _user32()
    _found.clear()

    def _cb(hwnd, _lparam):
        if not u.IsWindowVisible(hwnd):
            return True
        if exclude_pid is not None and _window_pid(hwnd) == exclude_pid:
            return True
        title = _window_title(hwnd)
        if title_hint in title:
            _found.append(int(hwnd))
            return False            # 找到一个就够了
        return True

    cb = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)(_cb)
    try:
        u.EnumWindows(cb, 0)
    finally:
        del cb                      # 回调对象不能在调用期间被回收
    return _found[0] if _found else 0


def force_foreground(hwnd: int) -> None:
    """尽力把窗口调到最前: 还原 → 置前; 抢不到就闪任务栏图标提醒。"""
    if not sys.platform.startswith("win") or not hwnd:
        return
    from ctypes import wintypes

    u = _user32()
    try:
        u.ShowWindow(wintypes.HWND(hwnd), 9)                # SW_RESTORE
        u.SetForegroundWindow(wintypes.HWND(hwnd))
        if int(u.GetForegroundWindow() or 0) != int(hwnd):
            u.FlashWindow(wintypes.HWND(hwnd), True)        # 抢不到焦点 → 闪任务栏
    except (OSError, AttributeError, TypeError) as exc:
        log.debug("窗口置前失败: %s", exc)


# ------------------------------------------------------------------ 单实例
class SingleInstance:
    """单实例判定。第一个实例拿得到锁; 后来者调用 `focus_existing()` 把那个窗口调到前面。"""

    def __init__(self):
        global _handle
        self._primary, _handle = acquire_mutex()
        if not self._primary:
            log.info("检测到已有实例在运行(本进程将退出)")

    @property
    def is_primary(self) -> bool:
        """本进程是不是唯一实例(False = 已经有窗口在跑)。"""
        return self._primary

    def focus_existing(self) -> bool:
        """(仅第二个实例用)把已经在运行的那个窗口调到前面。成功返回 True。"""
        if self._primary:
            return False
        hwnd = find_app_window(exclude_pid=os.getpid())
        if not hwnd:
            log.info("没找到已在运行的窗口(可能刚启动还没建好窗口)")
            return False
        force_foreground(hwnd)
        log.info("已把已在运行的窗口调到前面(hwnd=%s)", hwnd)
        return True
