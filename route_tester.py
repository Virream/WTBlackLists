"""跑图线路测试器 —— 独立启动入口。

与黑名单助手(WTBlackList 主程序)相互独立, 可单独运行:
    python route_tester.py           开发环境
    WTRouteTester.exe                打包版(见 build_route_tester.ps1)

依赖: PyQt6(项目既有依赖), 无需任何额外第三方库。
游戏需运行并开启 8111 本地接口(战争雷霆默认开启)。

日志: <data>/route_tester.log —— 打包版是 --windowed(无控制台),
      出错时靠该日志排查。
"""
import logging
import os
import sys
import traceback

from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import QApplication, QMessageBox

from wt81111g.config import data_dir, resource_path
from wt81111g.route_store import RouteStore
from wt81111g.route_window import RouteTesterWindow

log = logging.getLogger("route_tester")


def _setup_logging() -> str:
    """日志同时写文件(便于排查)与控制台(开发环境)。"""
    log_path = os.path.join(data_dir(), "route_tester.log")
    handlers: list[logging.Handler] = []
    try:
        handlers.append(logging.FileHandler(log_path, encoding="utf-8"))
    except OSError:
        pass
    if sys.stderr is not None:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers or None,
    )
    return log_path


def _install_excepthook(log_path: str) -> None:
    """未捕获异常写入日志并提示(打包版是无控制台的 windowed 程序)。"""
    def hook(exc_type, exc, tb) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc, tb)
            return
        detail = "".join(traceback.format_exception(exc_type, exc, tb))
        log.error("未捕获异常:\n%s", detail)
        try:
            QMessageBox.critical(
                None, "跑图线路测试器",
                f"程序遇到错误:\n\n{exc}\n\n详细信息已写入日志:\n{log_path}",
            )
        except Exception:  # noqa: BLE001 - 提示失败也要保证日志已写
            pass
    sys.excepthook = hook


def main() -> int:
    log_path = _setup_logging()
    _install_excepthook(log_path)
    app = QApplication(sys.argv)
    app.setApplicationName("WTRouteTester")
    app.setApplicationDisplayName("跑图线路测试器")
    # 窗口/任务栏图标: 用 icon\ 里的 PNG(Qt 内建解码, 不依赖 ico 读插件);
    # exe 自身的图标在打包时用 icon\WTRouteTester.ico(见 tools\make_app_icon.py)
    icon_path = resource_path(os.path.join("icon", "icon_512X512_WTRouteTester.png"))
    if os.path.exists(icon_path):
        icon = QIcon(icon_path)
        if not icon.isNull():
            app.setWindowIcon(icon)
            log.info("窗口图标已加载: %s", icon_path)
    else:
        log.warning("窗口图标资源缺失: %s", icon_path)

    try:
        win = RouteTesterWindow(RouteStore())
        win.show()
    except Exception:  # noqa: BLE001 - 启动失败必须给出可见提示
        detail = traceback.format_exc()
        log.error("启动失败:\n%s", detail)
        QMessageBox.critical(
            None, "跑图线路测试器",
            "程序启动失败, 详情已写入日志:\n\n"
            f"{log_path}\n\n{detail.strip().splitlines()[-1]}",
        )
        return 1
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
