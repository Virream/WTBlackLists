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

    # 数据保护: 即使载荷意外包含 data/evidences, 也不得覆盖用户已有数据
    tmp2 = tempfile.mkdtemp()
    inst.desktop_dir = lambda: tmp2
    import zipfile
    fake_zip = os.path.join(tmp2, "fake_payload.zip")
    with zipfile.ZipFile(fake_zip, "w") as z:
        z.writestr("WTBlackList.exe", "fake-exe")
        z.writestr("data/user.json", '{"overwrite": "bad"}')
        z.writestr("evidences/1/2.png", "fake")
    orig_payload = inst.payload_path
    inst.payload_path = lambda: fake_zip
    try:
        target2 = os.path.join(tmp2, "T2", "WTBlackList")
        os.makedirs(os.path.join(target2, "data"))
        with open(os.path.join(target2, "data", "user.json"), "w",
                  encoding="utf-8") as fh:
            fh.write('{"keep": "user-data"}')
        inst.install(target2)
        # 用户 data 未被覆盖
        with open(os.path.join(target2, "data", "user.json"),
                  encoding="utf-8") as fh:
            assert fh.read() == '{"keep": "user-data"}', "用户数据被安装覆盖!"
        # 应用文件正常解压, 数据目录未解压
        assert os.path.isfile(os.path.join(target2, "WTBlackList.exe")), \
            "应用文件未解压"
        assert not os.path.exists(os.path.join(target2, "evidences")), \
            "evidences 不应被解压"
    finally:
        inst.payload_path = orig_payload
    print("install 数据保护 OK")

    print("ALL INSTALLER TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
