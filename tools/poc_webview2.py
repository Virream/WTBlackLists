# -*- coding: utf-8 -*-
"""POC: 用 pywebview(Edge WebView2, 非 Chromium 内核自带)加载官网 userinfo 页,
验证能否通过 Cloudflare 人机验证并自动读取昵称 DOM(与现有 CDP 解析同思路)。

用法: python tools/poc_webview2.py [player_id]
窗口可见可交互; 若触发人机验证请手动完成; 抓到昵称或超时后自动关闭。
"""
import sys
import time

import webview

UID = sys.argv[1] if len(sys.argv) > 1 else "179656516"
URL = f"https://warthunder.com/zh/community/userinfo/?uid={UID}/"
TIMEOUT = 180  # 秒
_SELECTOR = "li.user-profile__data-nick"
_JS = (
    "(() => { const el = document.querySelector(%r);"
    " return el ? el.textContent.trim() : ''; })()" % _SELECTOR
)

nickname = None


def poll(window) -> None:
    global nickname
    print(f"[POC] 窗口已打开 uid={UID}, 开始轮询昵称(最多 {TIMEOUT}s)", flush=True)
    start = time.time()
    last_diag = 0.0
    while time.time() - start < TIMEOUT:
        try:
            res = window.evaluate_js(_JS)
        except Exception as exc:  # noqa: BLE001
            print("[POC] evaluate_js 异常:", exc, flush=True)
            res = None
        if res:
            nickname = res
            print(f"[POC] 抓取成功: {res!r}", flush=True)
            window.destroy()
            return
        # 每 15s 打印一次页面诊断, 定位"没抓到"的原因
        if time.time() - last_diag > 15:
            last_diag = time.time()
            try:
                diag = window.evaluate_js(
                    "JSON.stringify({url: location.href, title: document.title,"
                    " nickEls: document.querySelectorAll('li.user-profile__data-nick').length,"
                    " body0: (document.body ? document.body.innerText.slice(0, 100) : '')})"
                )
                print("[POC] 诊断:", diag, flush=True)
            except Exception as exc:  # noqa: BLE001
                print("[POC] 诊断异常:", exc, flush=True)
        time.sleep(1)
    print("[POC] 超时未抓到昵称(可能卡在人机验证/页面结构不同)", flush=True)
    window.destroy()


def main() -> int:
    global nickname
    window = webview.create_window("WT WebView2 POC", URL, width=1100, height=850)
    webview.start(poll, window)
    print("[POC] done nickname =", nickname, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
