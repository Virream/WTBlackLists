"""单实例锁: 保证程序只启动一份, 重复点击 exe 只弹"程序已在运行中"。"""
from __future__ import annotations

import os
import sys
import tempfile

_handle = None
_MUTEX_NAME = "WTBlackList_SingleInstance"


def ensure_single_instance() -> bool:
    """尝试获取单实例锁。返回 True 表示本进程是唯一实例。"""
    global _handle
    if sys.platform.startswith("win"):
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_mutex = kernel32.CreateMutexW
        create_mutex.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
        create_mutex.restype = wintypes.HANDLE
        _handle = create_mutex(None, False, _MUTEX_NAME)
        already_exists = ctypes.get_last_error() == 183  # ERROR_ALREADY_EXISTS
        return not already_exists

    # 非 Windows 兜底: 锁文件 + PID 存活检测
    path = os.path.join(tempfile.gettempdir(), _MUTEX_NAME + ".lock")
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        _handle = path
        return True
    except FileExistsError:
        try:
            with open(path, encoding="utf-8") as f:
                pid = int(f.read().strip())
            os.kill(pid, 0)
            return False
        except (ValueError, ProcessLookupError):
            try:
                os.remove(path)
            except OSError:
                pass
            return ensure_single_instance()
