"""将 dist/WTBlackList 应用目录打包为 build_assets/app_payload.zip (供自解压安装程序内嵌)。"""
import os
import sys
import zipfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(BASE, "dist", "WTBlackList")
OUT = os.path.join(BASE, "build_assets", "app_payload.zip")


def main() -> int:
    if not os.path.isdir(SRC):
        print("未找到应用目录:", SRC)
        return 1
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED) as z:
        for root, _dirs, files in os.walk(SRC):
            for f in files:
                full = os.path.join(root, f)
                rel = os.path.relpath(full, SRC)
                z.write(full, rel)
    print("已生成载荷:", OUT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
