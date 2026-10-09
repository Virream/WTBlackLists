"""战争雷霆黑名单助手 —— 程序入口。"""
import logging
import os
import sys
from logging.handlers import RotatingFileHandler

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication, QMessageBox

from wt81111g.config import APP_NAME, APP_VERSION, data_dir
from wt81111g.main_window import MainWindow
from wt81111g.single_instance import ensure_single_instance


def _setup_logging() -> None:
    """日志同时进控制台与文件(打包成 --windowed 后控制台看不到, 排查问题要看文件)。"""
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    try:
        handlers.append(RotatingFileHandler(
            os.path.join(data_dir(), "WTBlackList.log"),
            maxBytes=2 * 1024 * 1024, backupCount=2, encoding="utf-8",
        ))
    except OSError:
        pass   # 目录不可写等 → 只用控制台
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
        force=True,
    )


def main() -> int:
    _setup_logging()
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName(f"{APP_NAME} v{APP_VERSION}")

    # 单实例: 已运行则提示并退出
    if not ensure_single_instance():
        QMessageBox.information(None, "提示", "程序已在运行中")
        return 0

    win = MainWindow()
    win.show()

    # 首次使用软件自动弹出一次关于窗口
    if win.app_settings.first_run:
        win.app_settings.first_run = False
        win.app_settings.save()
        QTimer.singleShot(500, win._show_about)

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
