"""将 dist/WTBlackList 应用目录打包为 build_assets/app_payload.zip (供自解压安装程序内嵌)。"""
import os
import sys
import zipfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(BASE, "dist", "WTBlackList")
OUT = os.path.join(BASE, "build_assets", "app_payload.zip")

# 用户运行时数据目录(位于安装目录下), 严禁打入载荷:
# 若打进安装包, 重新安装解压时会覆盖用户已有数据(设置/黑名单/昵称缓存/证据)。
EXCLUDE_TOP_DIRS = {"data", "evidences"}


def main() -> int:
    if not os.path.isdir(SRC):
        print("未找到应用目录:", SRC)
        return 1
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED) as z:
        for root, dirs, files in os.walk(SRC):
            rel_root = os.path.relpath(root, SRC)
            if rel_root in EXCLUDE_TOP_DIRS:
                dirs[:] = []
                continue
            for f in files:
                full = os.path.join(root, f)
                rel = os.path.relpath(full, SRC)
                z.write(full, rel)
    print("已生成载荷:", OUT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
