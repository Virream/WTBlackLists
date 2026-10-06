"""提示音: 快捷键触发时播放 Windows 系统提示音。

三个动作使用**不同的系统事件音**, 便于游戏中凭听觉区分;
播放放在后台守护线程, 不阻塞 UI 与轮询。
"""
from __future__ import annotations

import logging
import threading

log = logging.getLogger("sound")

try:
    import winsound
except ImportError:  # 非 Windows
    winsound = None  # type: ignore[assignment]

# 动作 -> winsound 事件常量名
_SOUND_FLAGS: dict[str, str] = {
    "start": "MB_OK",               # 开始录制: 默认提示音
    "mark": "MB_ICONASTERISK",      # 标记位置: 信息音(较柔和)
    "stop": "MB_ICONEXCLAMATION",   # 结束录制: 警告音(较醒目)
}


def _beep(flags: int) -> None:
    try:
        winsound.MessageBeep(flags)
    except Exception as exc:  # noqa: BLE001 - 提示音失败不应影响主流程
        log.debug("提示音播放失败: %s", exc)


def play(action: str) -> None:
    """异步播放指定动作的提示音(非 Windows 环境静默忽略)。"""
    if winsound is None:
        return
    name = _SOUND_FLAGS.get(action, "MB_OK")
    flags = getattr(winsound, name, winsound.MB_OK)
    threading.Thread(target=_beep, args=(flags,), daemon=True).start()


def play_error() -> None:
    """播放错误提示音。"""
    if winsound is None:
        return
    threading.Thread(target=_beep, args=(winsound.MB_ICONHAND,), daemon=True).start()
