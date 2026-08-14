# 一键打包脚本: 应用(onedir 启动更快) + ZIP 版 + 自解压安装版
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$py = Join-Path $root ".venv\Scripts\python.exe"
Set-Location $root

Write-Host "== 1/5 生成图标(app.ico) =="
& $py "tools\png_to_ico.py"

Write-Host "== 2/5 打包应用 (onedir, 启动更快) =="
# 方案1: patchright 驱动系统真实浏览器(Edge/Chrome), 网络指纹真实, 自动过验证概率最高。
# 无需打包内置 Chromium(体积大减 ~300MB)。若本机无系统浏览器, 软件自动降级到
# CDP 方式(仍用系统浏览器)或提示。
& $py -m PyInstaller `
    --noconfirm `
    --clean `
    --onedir `
    --windowed `
    --name WTBlackList `
    --icon app.ico `
    --add-data "app.ico;." `
    --collect-all playwright `
    --collect-all patchright `
    --collect-all greenlet `
    --version-file version_info.txt `
    main.py

Write-Host "== 2.5/5 精简 Qt 体积(删除用不到的 Qt6 DLL/翻译/图片插件) =="
& $py "tools\trim_qt.py"

Write-Host "== 3/5 生成应用载荷 zip =="
& $py "tools\make_payload.py"

Write-Host "== 4/5 生成 ZIP 版 =="
Copy-Item "build_assets\app_payload.zip" "dist\WTBlackList.zip" -Force

Write-Host "== 5/5 打包自解压安装程序 =="
& $py -m PyInstaller `
    --noconfirm `
    --onefile `
    --windowed `
    --uac-admin `
    --name WTBlackList_Setup `
    --icon app.ico `
    --add-data "app.ico;." `
    --add-data "build_assets\app_payload.zip;." `
    --version-file version_info_installer.txt `
    "tools\installer\installer_main.py"

Write-Host "== 完成 =="
Write-Host "  应用目录: dist\WTBlackList\WTBlackList.exe"
Write-Host "  ZIP 版:   dist\WTBlackList.zip"
Write-Host "  安装版:   dist\WTBlackList_Setup.exe"
