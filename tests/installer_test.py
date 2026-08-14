"""自解压安装程序核心逻辑验证(解压 + 桌面快捷方式 + 默认安装目录回退)。"""
import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "tools", "installer"))

import installer_main as inst  # noqa: E402


def main() -> int:
    # 载荷存在
    payload = inst.payload_path()
    assert os.path.isfile(payload), f"载荷缺失: {payload}"
    print("payload OK:", payload)

    # 默认安装目录: D:\\Program Files 或 C:\\Program Files
    d = inst.default_install_dir()
    assert d.endswith("WTBlackList"), d
    drive = os.path.splitdrive(d)[0]
    assert drive in ("D:", "C:"), drive
    print("default install dir OK:", d)

    # 安装到临时目录(桌面快捷方式重定向到临时目录)
    tmp = tempfile.mkdtemp()
    inst.desktop_dir = lambda: tmp
    target = os.path.join(tmp, "Inst", "WTBlackList")
    lnk = inst.install(target)
    assert os.path.isfile(os.path.join(target, "WTBlackList.exe")), "exe 未解压"
    assert os.path.isdir(os.path.join(target, "_internal")), "_internal 未解压"
    assert os.path.isfile(os.path.join(tmp, "WTBlackList.lnk")), "快捷方式未创建"
    print("install + shortcut OK:", lnk)

    print("ALL INSTALLER TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
