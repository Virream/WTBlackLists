"""WTBlackList 自解压安装程序 (单文件)。

功能:
- 让用户选择安装位置, 默认 D:\\Program Files\\WTBlackList;
  若没有 D 盘或 D 盘空间不足则退回 C:\\Program Files\\WTBlackList。
- 解压内嵌的应用载荷, 完成后在桌面创建快捷方式。
"""
from __future__ import annotations

import ctypes
import os
import shutil
import sys
import zipfile
from ctypes import byref, wintypes

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QApplication,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

APP_NAME = "WTBlackList"
APP_EXE = "WTBlackList.exe"
PAYLOAD_ZIP = "app_payload.zip"
FREE_MARGIN = 300 * 1024 * 1024  # 300 MB 剩余空间余量

# 深色高对比度界面: 显式指定全部颜色, 避免浅色背景 + 白色文字的可读性问题
_DARK_QSS = """
QDialog {
    background-color: #26282c;
    color: #e8eaed;
}
QLabel {
    color: #e8eaed;
}
QLabel#Title {
    font-size: 16px;
    font-weight: bold;
    color: #ffffff;
}
QLabel#FieldLabel {
    color: #cfd3d9;
}
QLabel#Note {
    color: #b4b8c0;
}
QLineEdit {
    background-color: #34373c;
    color: #ffffff;
    border: 1px solid #565b62;
    border-radius: 4px;
    padding: 5px 7px;
    selection-background-color: #1f6feb;
}
QLineEdit:focus {
    border-color: #3b82f6;
}
QPushButton {
    background-color: #3a3e45;
    color: #ffffff;
    border: 1px solid #565b62;
    border-radius: 4px;
    padding: 6px 18px;
}
QPushButton:hover {
    background-color: #474c54;
}
QPushButton:pressed {
    background-color: #2e3136;
}
QPushButton#Install {
    background-color: #1e8e4e;
    color: #ffffff;
    font-weight: bold;
    border: none;
}
QPushButton#Install:hover {
    background-color: #24a75b;
}
"""

# --- Windows COM 快捷方式所需的 GUID ---
_CLSID_ShellLink = (0x00021401, 0x0000, 0x0000, (0xC0, 0, 0, 0, 0, 0, 0, 0x46))
_IID_IShellLinkW = (0x000214F9, 0x0000, 0x0000, (0xC0, 0, 0, 0, 0, 0, 0, 0x46))
_IID_IPersistFile = (0x0000010B, 0x0000, 0x0000, (0xC0, 0, 0, 0, 0, 0, 0, 0x46))


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]


def _make_guid(spec) -> _GUID:
    d1, d2, d3, b = spec
    g = _GUID()
    g.Data1, g.Data2, g.Data3 = d1, d2, d3
    arr = (ctypes.c_ubyte * 8)()
    for i, v in enumerate(b[:8]):
        arr[i] = v & 0xFF
    g.Data4 = arr
    return g


def _create_shortcut_com(lnk: str, target_exe: str, workdir: str) -> None:
    """用 Windows COM(IShellLinkW + IPersistFile)创建 .lnk 快捷方式。

    与资源管理器"创建快捷方式"走同一系统接口, 不启动任何子进程/脚本解释器,
    避免安全软件对"隐藏执行命令"产生告警。
    """
    ole32 = ctypes.windll.ole32
    ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, wintypes.DWORD]
    ole32.CoCreateInstance.argtypes = [
        ctypes.POINTER(_GUID), ctypes.c_void_p, wintypes.DWORD,
        ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p),
    ]
    ole32.CoCreateInstance.restype = ctypes.c_long
    ole32.CoInitializeEx(None, 0x2)  # COINIT_APARTMENTTHREADED
    try:
        clsid = _make_guid(_CLSID_ShellLink)
        iid_sl = _make_guid(_IID_IShellLinkW)
        iid_pf = _make_guid(_IID_IPersistFile)
        p_sl = ctypes.c_void_p()
        # CLSCTX_ALL = INPROC_SERVER|INPROC_HANDLER|LOCAL_SERVER|REMOTE_SERVER
        if ole32.CoCreateInstance(byref(clsid), None, 0x17,
                                  byref(iid_sl), byref(p_sl)) != 0:
            raise OSError("CoCreateInstance(ShellLink) failed")
        sl_vtbl = ctypes.cast(p_sl, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents

        def _method(idx, argtypes):
            return ctypes.CFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *argtypes)(sl_vtbl[idx])

        _method(20, [wintypes.LPCWSTR])(p_sl, target_exe)              # IShellLink::SetPath
        _method(9, [wintypes.LPCWSTR])(p_sl, workdir)                  # SetWorkingDirectory
        _method(17, [wintypes.LPCWSTR, ctypes.c_int])(p_sl, target_exe, 0)  # SetIconLocation

        p_pf = ctypes.c_void_p()
        _method(0, [ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p)])(
            p_sl, byref(iid_pf), byref(p_pf))
        pf_vtbl = ctypes.cast(p_pf, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents

        def _pf_method(idx, argtypes):
            return ctypes.CFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *argtypes)(pf_vtbl[idx])

        _pf_method(6, [wintypes.LPCWSTR, ctypes.c_int])(p_pf, lnk, 1)  # IPersistFile::Save

        Rel = ctypes.CFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(sl_vtbl[2])
        Rel(p_sl)
        RelP = ctypes.CFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(pf_vtbl[2])
        RelP(p_pf)
    finally:
        ole32.CoUninitialize()


_BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def resource_path(rel: str) -> str:
    """打包后在内嵌解压目录(_MEIPASS), 开发时在 build_assets 下。"""
    if getattr(sys, "frozen", False):
        base = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
        return os.path.join(base, rel)
    return os.path.join(_BASE_DIR, "build_assets", rel)


def payload_path() -> str:
    return resource_path(PAYLOAD_ZIP)


def payload_size() -> int:
    try:
        return os.path.getsize(payload_path())
    except OSError:
        return 0


def default_install_dir() -> str:
    """默认 D:\\Program Files; 无 D 盘或空间不足则退回 C:\\Program Files。"""
    need = FREE_MARGIN + payload_size()
    for drive in ("D:\\", "C:\\"):
        try:
            free = shutil.disk_usage(drive).free
        except OSError:
            continue
        if free >= need:
            return os.path.join(drive + "Program Files", APP_NAME)
    home = os.environ.get("USERPROFILE") or os.path.expanduser("~")
    return os.path.join(home, "Program Files", APP_NAME)


def desktop_dir() -> str:
    """获取桌面路径(直接用系统 API, 不启动 PowerShell)。"""
    if sys.platform.startswith("win"):
        try:
            buf = ctypes.create_unicode_buffer(260)
            shell32 = ctypes.windll.shell32
            shell32.SHGetFolderPathW.argtypes = [
                ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p,
                wintypes.DWORD, wintypes.LPWSTR,
            ]
            shell32.SHGetFolderPathW.restype = ctypes.c_long
            # CSIDL_DESKTOPDIRECTORY = 0x0010
            if shell32.SHGetFolderPathW(None, 0x0010, None, 0, buf) == 0:
                return buf.value
        except Exception:  # noqa: BLE001
            pass
    return os.path.join(os.environ.get("USERPROFILE") or "", "Desktop")


def create_shortcut(target_exe: str, workdir: str) -> str:
    """创建桌面快捷方式(Windows COM 实现, 无子进程/脚本解释器)。"""
    lnk = os.path.join(desktop_dir(), f"{APP_NAME}.lnk")
    if sys.platform.startswith("win"):
        try:
            _create_shortcut_com(lnk, target_exe, workdir)
        except Exception as exc:  # noqa: BLE001
            raise OSError(f"创建快捷方式失败: {exc}") from exc
    return lnk


def install(target: str) -> str:
    os.makedirs(target, exist_ok=True)
    with zipfile.ZipFile(payload_path(), "r") as z:
        z.extractall(target)
    exe = os.path.join(target, APP_EXE)
    return create_shortcut(exe, target)


class InstallDialog(QDialog):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"安装 {APP_NAME}")
        self.setModal(True)
        self.setMinimumWidth(560)

        layout = QVBoxLayout(self)
        title = QLabel(f"欢迎安装 {APP_NAME} (战争雷霆黑名单助手)")
        title.setObjectName("Title")
        layout.addWidget(title)

        label_dir = QLabel("选择安装位置:")
        label_dir.setObjectName("FieldLabel")
        layout.addWidget(label_dir)

        row = QHBoxLayout()
        self.path_edit = QLineEdit(default_install_dir())
        browse = QPushButton("浏览...")
        browse.clicked.connect(self._browse)
        row.addWidget(self.path_edit, 1)
        row.addWidget(browse)
        layout.addLayout(row)

        note = QLabel(
            "默认安装到 D:\\Program Files;若没有 D 盘或空间不足将安装到 C:\\Program Files。\n"
            "安装完成后会在桌面创建快捷方式。"
        )
        note.setObjectName("Note")
        note.setWordWrap(True)
        layout.addWidget(note)

        btns = QHBoxLayout()
        install_btn = QPushButton("安装")
        cancel_btn = QPushButton("取消")
        install_btn.setObjectName("Install")
        install_btn.setDefault(True)
        install_btn.clicked.connect(self._install)
        cancel_btn.clicked.connect(self.reject)
        btns.addStretch(1)
        btns.addWidget(install_btn)
        btns.addWidget(cancel_btn)
        layout.addLayout(btns)

    def _browse(self) -> None:
        d = QFileDialog.getExistingDirectory(
            self, "选择安装文件夹", os.path.dirname(self.path_edit.text()) or "C:\\"
        )
        if d:
            self.path_edit.setText(os.path.join(d, APP_NAME))

    def _install(self) -> None:
        target = os.path.abspath(self.path_edit.text().strip())
        if not target:
            QMessageBox.warning(self, "提示", "请选择安装位置")
            return
        try:
            lnk = install(target)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "安装失败", f"安装失败:\n{exc}")
            return
        QMessageBox.information(
            self, "安装完成",
            f"安装成功!\n安装位置: {target}\n桌面快捷方式: {lnk}\n\n请双击运行 {APP_EXE}",
        )
        self.accept()


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setStyleSheet(_DARK_QSS)
    if not os.path.exists(payload_path()):
        QMessageBox.critical(None, "错误", f"找不到安装数据包:\n{payload_path()}")
        return 1
    dlg = InstallDialog()
    dlg.exec()
    return 0


if __name__ == "__main__":
    sys.exit(main())
